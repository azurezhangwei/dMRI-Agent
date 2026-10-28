"""Connect the Slicer Agent panel to the managed local service."""
from __future__ import annotations

import os
import logging
import threading
from pathlib import Path

import qt
import slicer

from .runtime_bootstrap import get_runtime_manager
from .scene_server import SceneServerController
from .scene_assets import agent_output_paths, capture_slice_views, restore_slice_views
from .service_client import AgentServiceClient, ServiceError
from .session_lifecycle import detach_session, stop_session


class ServiceSlicerLogic:
    def __init__(self):
        super().__init__()
        self.client = None
        self.session = None
        self.runtime_manager = get_runtime_manager()
        self.scene_server = None

    def service_client(self, api_base=None):
        return AgentServiceClient(
            api_base or self.runtime_manager.base_url,
            token=self.runtime_manager.token,
        )

    def health(self, api_base=None):
        return self.service_client(api_base).health()

    def session_status(self):
        self._require_session()
        return self.client.session_status(self.session.session_id)

    def start_session(self, api_base, on_event, on_error, llm_mode="", **kwargs):
        workspace = str(self.runtime_manager.config.workspace or "")
        if not workspace:
            raise ServiceError("Choose a workspace in Settings before starting a task.")
        self.client = self.service_client(api_base)
        self.session = self.client.create_session(
            workspace=workspace,
            profile_id=kwargs.get("profile_id") or llm_mode or None,
            auto_approve_tools=bool(kwargs.get("auto_approve_tools", False)),
        )
        try:
            self.scene_server = SceneServerController(
                port=self.runtime_manager.config.scene_port
            )
            status = self.scene_server.start(
                auth_token=self.runtime_manager.scene_token_for_session(self.session.session_id)
            )
            os.environ["MCP_SLICER_WEB_SERVER_URL"] = f"http://127.0.0.1:{status.port}/slicer"
            self.client.start_stream(self.session.session_id, on_event, on_error)
        except Exception as exc:
            logging.exception("Could not start the local Slicer scene interface")
            if self.session.reused:
                self.detach()
            else:
                self.shutdown()
            raise ServiceError("Local Slicer scene access could not be started. Check the scene port and retry.") from exc
        return self.session

    def reconnect_stream(self, on_event, on_error):
        self._require_session()
        self.client.start_stream(self.session.session_id, on_event, on_error)

    def send_message(self, text, mode="execute", protocol_ids=None):
        self._require_session()
        return self.client.send_message(self.session.session_id, text, mode=mode, protocol_ids=protocol_ids)

    def cancel_turn(self):
        self._require_session()
        return self.client.send_command(self.session.session_id, "cancel")

    def set_auto_approve(self, enabled):
        self._require_session()
        return self.client.send_command(
            self.session.session_id, "auto_approve_on" if enabled else "auto_approve_off"
        )

    def respond_to_approval(self, pending, approved):
        self._require_session()
        method = self.client.approve_plan if pending["type"] == "plan" else self.client.approve_tool
        return method(self.session.session_id, pending["id"], approved)

    def shutdown(self):
        if self.client is not None and self.session is not None:
            stop_session(self.client, self.session.session_id)
        self._clear()

    def detach(self):
        detach_session(self.client)
        self._clear()

    def _clear(self):
        if self.scene_server is not None:
            self.scene_server.stop()
        self.scene_server = None
        self.client = None
        self.session = None

    def _require_session(self):
        if self.client is None or self.session is None:
            raise ServiceError("No active session.")


class ServiceWidgetMixin:
    def setup(self):
        super().setup()
        self.runtime_manager = get_runtime_manager(Path(__file__))
        saved = str(slicer.app.userSettings().value("DMRIAgent/Workspace", "") or "")
        if saved and self.runtime_manager.config.workspace is None:
            try:
                self.runtime_manager.set_workspace(saved)
            except (OSError, ValueError):
                pass
        self._profiles_loaded = False
        self._service_ready = False
        self._settings_result = None
        self._settings_test_profile = None
        self._settings_test_label = ""
        self._settings_test_model = ""
        self._settings_catalog_job = None
        self._model_profiles = {}
        self.runtime_manager.start_async()
        qt.QTimer.singleShot(0, self._finish_service_setup)

    def _finish_service_setup(self):
        self.logic.runtime_manager = self.runtime_manager
        self._cli_view_snapshots = {}
        workspace = self.runtime_manager.config.workspace
        if workspace is not None:
            os.environ["MRI_AGENT_WORKSPACE"] = str(workspace)
            os.environ["MRI_AGENT_FILE_ROOTS"] = str(workspace)
            self.refresh_workspace_roots()
            self.refresh_workspace_tree()
        slicer.app.connect("aboutToQuit()", self.logic.detach)
        self.api_base_edit.text = self.runtime_manager.base_url
        self.settings_button = qt.QToolButton()
        self.settings_button.setIcon(slicer.util.mainWindow().style().standardIcon(qt.QStyle.SP_FileDialogDetailedView))
        self.settings_button.setToolTip("Workspace and model settings")
        self.settings_button.setAccessibleName("Agent settings")
        self.settings_button.connect("clicked(bool)", self.on_service_settings)
        self.session_controls.layout().addWidget(self.settings_button)
        self._startup_timer = qt.QTimer()
        self._startup_timer.setInterval(300)
        self._startup_timer.connect("timeout()", self._refresh_runtime_status)
        self._startup_timer.start()
        self._refresh_runtime_status()

    def _set_connection_state(self, reachable):
        super()._set_connection_state(reachable)
        self.start_button.setEnabled(reachable and getattr(self, "_model_ready", False) and self.logic.session is None)

    def _update_composer_action(self):
        super()._update_composer_action()
        if not getattr(self, "_model_ready", False) and self.logic.session is None:
            self.send_button.setEnabled(False)

    def _selected_profile_ready(self, *_args):
        selected = str(self.llm_mode_combo.itemData(self.llm_mode_combo.currentIndex) or "")
        session = self.logic.session
        if selected and session is not None and selected != session.profile_id:
            if self._awaiting_response or self.pending_approval is not None or self._cancel_pending:
                # Programmatic selection changes must obey the same task lock.
                blocked = self.llm_mode_combo.blockSignals(True)
                self.llm_mode_combo.setCurrentIndex(self.llm_mode_combo.findData(session.profile_id))
                self.llm_mode_combo.blockSignals(blocked)
                return
            self._reset_chat()
        self._model_ready = bool(getattr(self, "_profile_readiness", {}).get(selected, False))
        if getattr(self, "_settings_dialog", None) is not None:
            self.settings_profile_label.text = self.llm_mode_combo.currentText
            if getattr(self, "_settings_model_profile", None) != selected:
                self._fill_settings_model(selected)
        self._update_composer_action()
        if not self._model_ready:
            self.activity_text_label.text = "Configure this model in Settings"

    def cleanup(self):
        if hasattr(self, "_startup_timer"):
            self._startup_timer.stop()
        self.logic.detach()
        super().cleanup()

    def _fit_active_volume_views(self, node_id):
        selection = slicer.app.applicationLogic().GetSelectionNode()
        if str(selection.GetActiveVolumeID() or "") != str(node_id):
            return
        node = slicer.mrmlScene.GetNodeByID(node_id)
        if node is None or node.GetImageData() is None:
            return
        count = slicer.mrmlScene.GetNumberOfNodesByClass("vtkMRMLSliceCompositeNode")
        for index in range(count):
            composite = slicer.mrmlScene.GetNthNodeByClass(index, "vtkMRMLSliceCompositeNode")
            if str(composite.GetBackgroundVolumeID() or "") != str(node_id):
                return
            if composite.GetForegroundVolumeID() or composite.GetLabelVolumeID():
                return
        manager = slicer.app.layoutManager()
        if manager is None:
            return
        for name in ("Red", "Yellow", "Green"):
            widget = manager.sliceWidget(name)
            if widget is None:
                continue
            logic = widget.sliceLogic()
            bounds = [0.0] * 6
            logic.GetVolumeSliceBounds(node, bounds)
            logic.SetSliceOffset((bounds[4] + bounds[5]) / 2.0)
            view = widget.sliceView()
            logic.FitSliceToBackground(view.width, view.height)

    def _refresh_runtime_status(self):
        if not hasattr(self, "api_base_edit"):
            return
        self.api_base_edit.text = self.runtime_manager.base_url
        snapshot = self.runtime_manager.snapshot()
        self._service_ready = snapshot.state == "ready"
        if not self._service_ready:
            self._set_connection_state(False)
            self.activity_text_label.text = snapshot.message
            self.activity_text_label.setToolTip(snapshot.message)
            return
        if hasattr(self, "_startup_timer"):
            self._startup_timer.stop()
        try:
            if not self._profiles_loaded:
                models = self.logic.service_client().list_models()
                self._model_profiles = {profile["id"]: dict(profile) for profile in models.get("profiles", [])}
                self._profile_readiness = {
                    profile["id"]: bool(profile.get("api_key_configured"))
                    for profile in models.get("profiles", [])
                }
                self.llm_mode_combo.clear()
                active = str(models.get("active_profile") or "")
                for profile in models.get("profiles", []):
                    if profile.get("provider") not in {"deepseek", "openai", "anthropic", "dashscope"}:
                        continue
                    self.llm_mode_combo.addItem(
                        f"{profile.get('label', profile['id'])} · {profile.get('model', '')}",
                        profile["id"],
                    )
                    if profile["id"] == active:
                        self.llm_mode_combo.setCurrentIndex(self.llm_mode_combo.count - 1)
                self._profiles_loaded = True
                self.llm_mode_combo.connect("currentIndexChanged(int)", self._selected_profile_ready)
                self._selected_profile_ready()
                if getattr(self, "_settings_dialog", None) is not None:
                    self.settings_profile_label.text = self.llm_mode_combo.currentText
                    selected = str(self.llm_mode_combo.itemData(self.llm_mode_combo.currentIndex) or "")
                    self._fill_settings_model(selected)
            super()._refresh_runtime_status()
        except ServiceError as exc:
            self._set_connection_state(False)
            self.activity_text_label.text = str(exc)

    def on_start_session(self):
        if not self._service_ready or not getattr(self, "_model_ready", False):
            self.on_service_settings()
            return False
        return super().on_start_session()

    def handle_event(self, payload):
        kind = payload.get("type")
        value = payload.get("value")
        value = value if isinstance(value, dict) else {}
        call_id = value.get("callId") or value.get("call_id") or ""
        if kind == "tool_call_start":
            args = value.get("args") or {}
            if (
                value.get("name") == "run_slicer_module"
                and args.get("action", "run") == "run"
                and args.get("mode", "auto") != "logic"
            ):
                self._cli_view_snapshots[call_id] = capture_slice_views(
                    slicer.mrmlScene, slicer.app.applicationLogic().GetSelectionNode()
                )
        if kind in {"tool_call_response", "tool_response"}:
            snapshot = self._cli_view_snapshots.pop(call_id, None)
            if snapshot is not None:
                restore_slice_views(slicer.mrmlScene, slicer.app.applicationLogic().GetSelectionNode(), snapshot)
            paths = list(value.get("output_paths") or [])
            for path in agent_output_paths(value.get("output"), self.runtime_manager.config.workspace):
                if path not in paths:
                    paths.append(path)
            if paths:
                payload = {**payload, "value": {**value, "output_paths": paths}}
        if kind in {"finished", "error"}:
            for snapshot in self._cli_view_snapshots.values():
                restore_slice_views(slicer.mrmlScene, slicer.app.applicationLogic().GetSelectionNode(), snapshot)
            self._cli_view_snapshots.clear()
        if kind == "error":
            self.pending_approval = None
            self._set_approval_ui("No pending approval.", enabled=False)
        if kind == "finished" and (payload.get("value") or {}).get("reason") == "plan_ready":
            # Planning is complete, not the pending review/approval.
            self.current_stream_index = None
            self._awaiting_response = False
            self._cancel_pending = False
            self._render_runtime_status()
            return
        if kind == "plan_cleared":
            self.pending_approval = None
            self._set_approval_ui("No pending approval.", enabled=False)
        return super().handle_event(payload)

    def on_service_settings(self, *_args):
        if self._awaiting_response or self.pending_approval is not None:
            slicer.util.infoDisplay("Stop the current task before changing workspace or model settings.")
            return
        if getattr(self, "_settings_dialog", None) is not None:
            self._settings_dialog.raise_()
            return
        dialog = qt.QDialog(slicer.util.mainWindow())
        self._settings_dialog = dialog
        dialog.setWindowTitle("dMRI-Agent Settings")
        dialog.setMinimumWidth(520)
        layout = qt.QFormLayout(dialog)
        self.settings_workspace_edit = qt.QLineEdit(str(self.runtime_manager.config.workspace or ""))
        self.settings_workspace_choose_button = qt.QPushButton("Choose...")
        row = qt.QHBoxLayout()
        row.addWidget(self.settings_workspace_edit)
        row.addWidget(self.settings_workspace_choose_button)
        layout.addRow("Workspace", row)
        def choose_workspace(*_args):
            path = qt.QFileDialog.getExistingDirectory(dialog, "Choose workspace", str(Path.home()))
            if path:
                try:
                    self._apply_workspace_folder(path)
                except (OSError, ValueError) as exc:
                    self.settings_status_label.text = str(exc)
        self.settings_workspace_choose_button.connect("clicked(bool)", choose_workspace)
        self.settings_profile_label = qt.QLabel(self.llm_mode_combo.currentText)
        layout.addRow("Provider", self.settings_profile_label)
        self.settings_model_edit = qt.QComboBox()
        self.settings_model_edit.setEditable(True)
        self.settings_model_edit.setInsertPolicy(qt.QComboBox.NoInsert)
        self.settings_model_edit.setSizeAdjustPolicy(qt.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.settings_model_edit.setMinimumContentsLength(18)
        self.settings_model_edit.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Fixed)
        self.settings_model_edit.setMaxVisibleItems(12)
        self.settings_model_edit.setObjectName("DMRIModelNameEdit")
        self.settings_model_edit.setAccessibleName("Model name")
        self.settings_fetch_models_button = qt.QPushButton("Get model list")
        self.settings_fetch_models_button.setObjectName("DMRIFetchModelsButton")
        self.settings_fetch_models_button.setToolTip("Fetch models using the entered API key, or the saved key when left blank.")
        self.settings_fetch_models_button.connect("clicked(bool)", self._fetch_settings_models)
        row = qt.QHBoxLayout()
        row.addWidget(self.settings_model_edit)
        row.addWidget(self.settings_fetch_models_button)
        layout.addRow("Model name", row)
        self.settings_api_key_edit = qt.QLineEdit()
        self.settings_api_key_edit.setEchoMode(qt.QLineEdit.Password)
        self.settings_api_key_edit.setPlaceholderText("Leave blank to keep the saved key")
        layout.addRow("API key", self.settings_api_key_edit)
        self.settings_status_label = qt.QLabel("")
        self.settings_status_label.setWordWrap(True)
        self.settings_status_label.setMinimumHeight(self.settings_status_label.fontMetrics().lineSpacing() * 2)
        layout.addRow(self.settings_status_label)
        self.settings_save_button = qt.QPushButton("Save && Test")
        self.settings_save_button.connect("clicked(bool)", self._save_service_settings)
        selected = str(self.llm_mode_combo.itemData(self.llm_mode_combo.currentIndex) or "")
        self._fill_settings_model(selected)
        if self._settings_test_profile is not None:
            self.settings_save_button.setEnabled(False)
            self.settings_model_edit.setEnabled(False)
            self.settings_fetch_models_button.setEnabled(False)
            self.settings_status_label.text = f"Testing {self._settings_test_label} connection…"
        layout.addRow(self.settings_save_button)
        dialog.connect("finished(int)", self._close_service_settings)
        dialog.show()

    def _fill_settings_model(self, profile_id):
        self._settings_catalog_job = None
        profile = getattr(self, "_model_profiles", {}).get(profile_id, {})
        model = str(profile.get("model") or "")
        if not model and " · " in self.llm_mode_combo.currentText:
            model = self.llm_mode_combo.currentText.rsplit(" · ", 1)[-1]
        self.settings_model_edit.clear()
        self.settings_model_edit.setEditText(model)
        self.settings_model_edit.lineEdit().setPlaceholderText("Enter or select a model name")
        default = str(profile.get("default_model") or model)
        self.settings_model_edit.setToolTip(f"Default: {default}. You can enter another model available to this provider.")
        if getattr(self, "_settings_model_profile", None) != profile_id and hasattr(self, "settings_api_key_edit"):
            self.settings_api_key_edit.clear()
            if self._settings_test_profile is None:
                self.settings_status_label.text = ""
        if hasattr(self, "settings_save_button"):
            self._update_settings_controls()
        self._settings_model_profile = profile_id

    def _update_settings_controls(self):
        testing = self._settings_test_profile is not None
        fetching = self._settings_catalog_job is not None
        self.settings_save_button.setEnabled(not testing and not fetching)
        self.settings_fetch_models_button.setEnabled(not testing and not fetching)
        self.settings_model_edit.setEnabled(not testing)
        self.settings_api_key_edit.setEnabled(not fetching)

    def _fetch_settings_models(self, *_args):
        if self._settings_catalog_job is not None or self._settings_test_profile is not None:
            return
        if not self._service_ready or not self._profiles_loaded:
            self.settings_status_label.text = "Wait for the service, then fetch the model list."
            return
        profile_id = str(self.llm_mode_combo.itemData(self.llm_mode_combo.currentIndex) or "")
        key = self.settings_api_key_edit.text.strip()
        if not key and not self._profile_readiness.get(profile_id):
            self.settings_status_label.text = "Enter an API key before fetching models."
            return
        client = self.logic.service_client()
        job = {"dialog": self._settings_dialog, "profile_id": profile_id, "result": None}
        self._settings_catalog_job = job
        self._update_settings_controls()
        self.settings_status_label.text = "Fetching the provider's model list…"

        def fetch():
            try:
                payload = client.fetch_model_catalog(profile_id, api_key=key)
                job["result"] = (True, payload["models"])
            except ServiceError as exc:
                job["result"] = (False, str(exc))
            except Exception:
                job["result"] = (False, "Could not fetch models. Check the key and network, or enter a model name manually.")

        threading.Thread(target=fetch, daemon=True, name="dmri-model-catalog").start()
        qt.QTimer.singleShot(100, lambda: self._poll_settings_models(job))

    def _poll_settings_models(self, job):
        if self._settings_catalog_job is not job or self._settings_dialog is not job["dialog"]:
            return
        if job["result"] is None:
            qt.QTimer.singleShot(100, lambda: self._poll_settings_models(job))
            return
        self._settings_catalog_job = None
        self._update_settings_controls()
        profile_id = str(self.llm_mode_combo.itemData(self.llm_mode_combo.currentIndex) or "")
        if profile_id != job["profile_id"]:
            return
        passed, result = job["result"]
        if not passed:
            self.settings_status_label.text = result
            return
        current = self.settings_model_edit.currentText
        self.settings_model_edit.clear()
        for model in result:
            self.settings_model_edit.addItem(model["id"])
            self.settings_model_edit.setItemData(
                self.settings_model_edit.count - 1, model.get("name") or model["id"], qt.Qt.ToolTipRole,
            )
        self.settings_model_edit.setEditText(current)
        self.settings_status_label.text = (
            f"Fetched {len(result)} models. Select one or enter a name, then Save & Test."
        )

    def _apply_workspace_folder(self, path):
        workspace = Path(path).expanduser().resolve(strict=True)
        if not workspace.is_dir() or workspace == Path(workspace.anchor):
            raise ValueError("Choose a workspace folder other than the filesystem root.")
        changed = workspace != self.runtime_manager.config.workspace
        if changed and (self._awaiting_response or self.pending_approval is not None):
            raise ValueError("Stop the current task before changing workspace.")
        self.runtime_manager.set_workspace(workspace)
        if changed and self.logic.session is not None:
            self._reset_chat()
        workspace = super()._apply_workspace_folder(workspace)
        self.runtime_manager.environ.update({
            "MRI_AGENT_WORKSPACE": str(workspace),
            "MRI_AGENT_FILE_ROOTS": str(workspace),
            "DMRI_DEFAULT_WORKSPACE": str(workspace),
        })
        slicer.app.userSettings().setValue("DMRIAgent/Workspace", str(workspace))
        if getattr(self, "_settings_dialog", None) is not None:
            self.settings_workspace_edit.text = str(workspace)
        self.runtime_manager.retry_async()
        self._startup_timer.start()
        return workspace

    def _close_service_settings(self, *_args):
        self._settings_catalog_job = None
        self.settings_api_key_edit.clear()
        self._settings_dialog = None

    def _save_service_settings(self, *_args):
        if self._settings_catalog_job is not None:
            self.settings_status_label.text = "Wait for the model list before saving settings."
            return
        if self._settings_test_profile is not None:
            self.settings_status_label.text = f"Testing {self._settings_test_label} connection…"
            return
        if self._awaiting_response or self.pending_approval is not None:
            self.settings_status_label.text = "Stop the current task before changing settings."
            return
        try:
            self._apply_workspace_folder(self.settings_workspace_edit.text.strip())
        except (OSError, ValueError) as exc:
            self.settings_status_label.text = str(exc)
            return
        profile_id = str(self.llm_mode_combo.itemData(self.llm_mode_combo.currentIndex) or "") if self._profiles_loaded else ""
        if not self._service_ready or not profile_id:
            self.settings_status_label.text = "Workspace saved. Wait for the service, then Save & Test."
            return
        model = self.settings_model_edit.currentText.strip()
        if not model or len(model) > 256 or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in model):
            self.settings_status_label.text = "Enter a model name without spaces or control characters (up to 256 characters)."
            return
        if self.logic.session is not None:
            self._reset_chat()
        key = self.settings_api_key_edit.text.strip()
        self.settings_api_key_edit.clear()
        client = self.logic.service_client()
        profile = getattr(self, "_model_profiles", {}).get(profile_id, {})
        profile_label = f"{profile.get('label') or self.llm_mode_combo.currentText} · {model}"
        self._settings_test_profile = profile_id
        self._settings_test_label = profile_label
        self._settings_test_model = model
        self._settings_result = None
        self._update_settings_controls()
        self.settings_status_label.text = f"Testing {profile_label} connection…"
        def test():
            try:
                result = client.test_model(profile_id, api_key=key, model=model, save_on_success=True)
                if not result.get("ok"):
                    raise ServiceError("Model test did not report success")
                client.set_active_model(profile_id)
                self._settings_result = (profile_id, True, f"{profile_label}: Connection successful. Settings saved locally.")
            except Exception:
                self._settings_result = (profile_id, False, f"{profile_label}: Connection failed. Check the key, model access and network.")
        threading.Thread(target=test, daemon=True, name="dmri-model-setup").start()
        qt.QTimer.singleShot(100, self._poll_service_settings)

    def _poll_service_settings(self):
        if self._settings_test_profile is None:
            return
        if self._settings_result is None:
            qt.QTimer.singleShot(100, self._poll_service_settings)
            return
        profile_id, passed, message = self._settings_result
        tested_model = self._settings_test_model
        self._settings_result = None
        self._settings_test_profile = None
        self._settings_test_label = ""
        self._settings_test_model = ""
        if passed:
            profile = self._model_profiles.setdefault(profile_id, {})
            profile["model"] = tested_model
            for index in range(self.llm_mode_combo.count):
                if str(self.llm_mode_combo.itemData(index) or "") == profile_id:
                    label = profile.get("label") or self.llm_mode_combo.itemText(index).rsplit(" · ", 1)[0]
                    self.llm_mode_combo.setItemText(index, f"{label} · {tested_model}")
                    break
            self._profile_readiness[profile_id] = True
            self._selected_profile_ready()
        if self._settings_dialog is not None:
            self._update_settings_controls()
            self.settings_status_label.text = message
