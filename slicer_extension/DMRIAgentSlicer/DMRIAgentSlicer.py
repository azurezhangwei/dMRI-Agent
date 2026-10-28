import html
import json
import os
import queue
import time
import textwrap
from functools import partial
from pathlib import Path
from typing import Any, Optional

import ctk
import qt
import slicer
from slicer.ScriptedLoadableModule import (
    ScriptedLoadableModule,
    ScriptedLoadableModuleLogic,
    ScriptedLoadableModuleWidget,
)

from DMRIAgentLib.service_client import ServiceError
from DMRIAgentLib.service_integration import ServiceWidgetMixin, ServiceSlicerLogic
from DMRIAgentLib.runtime_bootstrap import peek_runtime_manager
from DMRIAgentLib.activity_summary import plan_review, tool_activity, tool_outcome, tool_review, review_html
from DMRIAgentLib.markdown_render import render_markdown_fragment
from DMRIAgentLib.chat_render import render_conversation
from DMRIAgentLib.scene_assets import (
    SceneAssetRegistry,
    collect_file_metadata,
    default_workspace_roots,
    detect_file_kind,
    infer_space,
)


UI_FONT_FAMILY = "Noto Sans CJK SC"
UI_FONT_STACK = (
    "'Noto Sans CJK SC','Noto Sans CJK JP','WenQuanYi Micro Hei',"
    "'Microsoft YaHei','DejaVu Sans',sans-serif"
)
MONO_FONT_STACK = (
    "'Noto Sans Mono CJK SC','Noto Sans Mono CJK JP',"
    "'DejaVu Sans Mono',monospace"
)


SCENE_NODE_CLASSES = [
    "vtkMRMLDiffusionWeightedVolumeNode",
    "vtkMRMLScalarVolumeNode",
    "vtkMRMLLabelMapVolumeNode",
    "vtkMRMLSegmentationNode",
    "vtkMRMLFiberBundleNode",
    "vtkMRMLModelNode",
    "vtkMRMLMarkupsFiducialNode",
    "vtkMRMLMarkupsCurveNode",
    "vtkMRMLMarkupsLineNode",
]

SUPPORTED_FILE_KINDS = {
    "scalar_volume",
    "diffusion_volume",
    "labelmap",
    "segmentation",
    "fiber_bundle",
    "model",
    "markups",
    "scene",
}

KIND_LABELS = {
    "scalar_volume": "Volume",
    "diffusion_volume": "DWI",
    "labelmap": "Label Map",
    "segmentation": "Segmentation",
    "fiber_bundle": "Fiber Bundle",
    "model": "3D Model",
    "markups": "Markups",
    "scene": "Scene",
    "folder": "Folder",
    "file": "File",
}

MAX_TREE_DEPTH = 5
MAX_TREE_ITEMS_PER_DIR = 150
CHAT_MINIMUM_HEIGHT = 300
CHAT_DEFAULT_HEIGHT = 600

class DMRIAgentSlicer(ScriptedLoadableModule):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent.title = "dMRI-Agent"
        self.parent.categories = ["Diffusion MRI"]
        self.parent.dependencies = []
        self.parent.contributors = ["Fan’s Lab", "Wei Zhang", "Chengjin Li", "Guanlin Huang"]
        self.parent.helpText = (
            "A guided Slicer front-end for dMRI-Agent. Browse data, load files into the scene, "
            "review approvals, and run protocol-driven diffusion MRI workflows."
        )
        self.parent.acknowledgementText = (
            "Developed by Fan’s Lab. "
            "Main contributors: Wei Zhang, Chengjin Li, and Guanlin Huang."
        )
        slicer.app.connect("aboutToQuit()", self._release_runtime)


    def _release_runtime(self):
        # The runtime manager only releases resources acquired by this Slicer
        # process; a compatible backend owned by another frontend is untouched.
        # Merely loading the module from the user's startup hook must not create
        # runtime state during Slicer shutdown if its widget was never opened.
        manager = peek_runtime_manager()
        if manager is not None:
            manager.stop()

class ChatWheelFilter(qt.QObject):
    def __init__(self, transcript):
        super().__init__()
        self.transcript = transcript

    def eventFilter(self, watched, event):
        if event.type() != qt.QEvent.Wheel:
            return False
        scroll_bar = self.transcript.verticalScrollBar()
        angle = event.angleDelta().y()
        pixels = event.pixelDelta().y()
        step = scroll_bar.singleStep
        if callable(step):
            step = step()
        distance = pixels if pixels else angle * max(step * 3, 48) / 120
        value = scroll_bar.value
        if callable(value):
            value = value()
        scroll_bar.setValue(value - round(distance))
        event.accept()
        return True


class ChatResizeFilter(qt.QObject):
    """Resize Chat independently of the collapsed Files section's fixed height."""

    def __init__(self, chat, resize):
        super().__init__()
        self.chat = chat
        self.resize = resize
        self._start_y = None
        self._start_height = 0

    def eventFilter(self, handle, event):
        kind = event.type()
        if kind == qt.QEvent.MouseButtonPress and event.button() == qt.Qt.LeftButton:
            self._start_y = event.globalPos().y()
            self._start_height = int(self.chat.height)
            handle.setFocus()
            handle.grabMouse()
        elif kind == qt.QEvent.MouseMove and self._start_y is not None:
            self.resize(self._start_height + event.globalPos().y() - self._start_y)
        elif kind == qt.QEvent.MouseButtonRelease and self._start_y is not None:
            self.resize(self._start_height + event.globalPos().y() - self._start_y)
            self._start_y = None
            handle.releaseMouse()
        elif kind == qt.QEvent.UngrabMouse:
            self._start_y = None
            return False
        elif kind == qt.QEvent.KeyPress and event.key() in (qt.Qt.Key_Up, qt.Qt.Key_Down):
            self.resize(int(self.chat.height) + (20 if event.key() == qt.Qt.Key_Down else -20))
        else:
            return False
        event.accept()
        return True


class BaseAgentPanelWidget(ScriptedLoadableModuleWidget):
    def setup(self):
        super().setup()
        self.logic = DMRIAgentSlicerLogic()
        self.asset_registry = SceneAssetRegistry()
        self.recent_outputs: dict[str, dict[str, Any]] = {}
        self._pending_events = queue.Queue()
        self._session_generation = 0
        self._event_timer = qt.QTimer()
        self._event_timer.setInterval(100)
        self._event_timer.connect("timeout()", self._drain_pending_events)
        self._event_timer.start()
        self.pending_approval: Optional[dict[str, str]] = None
        self.transcript_entries: list[dict[str, str]] = []
        self._tool_activity_by_call: dict[str, int] = {}
        self._reasoning_activity_index: Optional[int] = None
        self._tool_activity_labels: dict[str, str] = {}
        self._expanded_entries: set[int | str] = set()
        self._transcript_render_timer = qt.QTimer()
        self._transcript_render_timer.setSingleShot(True)
        self._transcript_render_timer.setInterval(100)
        self._transcript_render_timer.connect("timeout()", self.render_transcript)
        self._transcript_scroll_pending = False
        self._transcript_render_revision = 0
        self.current_stream_index: Optional[int] = None
        self._last_event_at: float = 0.0
        self._last_event_kind = "No events yet"
        self._last_error_text = ""
        self._awaiting_response = False
        self._cancel_pending = False
        self._chat_requested_height = CHAT_DEFAULT_HEIGHT
        self._chat_height_dragged = False
        self._apply_styles()

        self.hero_card = self._build_hero()
        self.chat_launcher_card = self._build_chat_launcher_card()
        self.connection_card = self._build_connection_card()
        self.session_controls = self._build_session_controls()
        self.section_nav = self._build_section_nav()
        self.chat_card = self._build_chat_card()
        self._chat_wheel_filter = ChatWheelFilter(self.transcript)
        for widget in (self.transcript, self.transcript.viewport()):
            widget.installEventFilter(self._chat_wheel_filter)
        self.workspace_card = self._build_workspace_card()
        self.chat_resize_handle = self._build_chat_resize_handle()

        self.layout.addWidget(self.session_controls, 0)
        self.layout.addWidget(self.section_nav, 0)
        self.layout.addWidget(self.chat_card, 0)
        self.layout.addWidget(self.chat_resize_handle, 0)
        self.layout.addWidget(self.workspace_card, 0)
        self.layout.addStretch(1)
        self.layout.setSizeConstraint(qt.QLayout.SetMinimumSize)

        self._status_timer = qt.QTimer()
        self._status_timer.setInterval(3000)
        self._status_timer.connect("timeout()", self._refresh_runtime_status)
        self._status_timer.start()
        self._activity_frame = 0
        self._activity_timer = qt.QTimer()
        self._activity_timer.setInterval(350)
        self._activity_timer.connect("timeout()", self._tick_activity_indicator)
        self._activity_timer.start()

        self.start_button.connect("clicked(bool)", self.on_start_session)
        self.stop_button.connect("clicked(bool)", self.on_stop_session)
        self.reconnect_button.connect("clicked(bool)", self.on_reconnect_stream)
        self.send_button.connect("clicked(bool)", self.on_send_or_cancel)
        # self.one_click_prompt_button.connect("clicked(bool)", self.on_send_one_click_prompt)
        self.open_chat_button.connect("clicked(bool)", self.show_chat_window)
        self.approve_button.connect("clicked(bool)", partial(self.on_approval, True))
        self.reject_button.connect("clicked(bool)", partial(self.on_approval, False))
        self.auto_approve_box.connect("toggled(bool)", self._sync_auto_approve_from_toolbar)

        self.workspace_root_combo.connect("currentIndexChanged(int)", self.refresh_workspace_tree)
        self.choose_workspace_button.connect("clicked(bool)", self.on_choose_workspace)
        self.refresh_tree_button.connect("clicked(bool)", self.refresh_workspace_tree)
        self.workspace_search_edit.connect("textChanged(QString)", self._apply_workspace_filters)
        self.workspace_tree.connect(
            "itemSelectionChanged()", self.on_workspace_selection_changed
        )
        self.workspace_tree.connect(
            "itemDoubleClicked(QTreeWidgetItem*,int)", self.on_workspace_item_activated
        )
        self.load_file_button.connect("clicked(bool)", self.on_load_selected_file)
        self.insert_file_context_button.connect(
            "clicked(bool)", self.on_insert_selected_file_into_prompt
        )

        self.refresh_workspace_roots()
        self.refresh_workspace_tree()
        self.sync_scene_assets()
        self._set_connection_state(False)
        self._set_session_status("No active session", "#F2F4F7", "#344054", "#D0D5DD")
        self._set_stream_status("Idle", "#FEF3C7", "#92400E", "#FCD34D")
        self._render_runtime_status()
        self._set_approval_ui("No pending approval.", enabled=False)
        self.render_transcript()
        qt.QTimer.singleShot(150, self._refresh_runtime_status)
        qt.QTimer.singleShot(0, self._fit_initial_chat_height)

    def cleanup(self):
        if hasattr(self, "_transcript_render_timer"):
            self._transcript_render_timer.stop()
        if hasattr(self, "_event_timer") and self._event_timer is not None:
            self._event_timer.stop()
        if hasattr(self, "_status_timer") and self._status_timer is not None:
            self._status_timer.stop()
        if hasattr(self, "_activity_timer") and self._activity_timer is not None:
            self._activity_timer.stop()
        self.logic.shutdown()

    def _apply_styles(self):
        stylesheet = """
            QWidget {
              font-family: "Noto Sans CJK SC", "Noto Sans CJK JP", "DejaVu Sans", sans-serif;
            }
            QGroupBox {
              font-weight: 600;
              border: 1px solid #D0D5DD;
              border-radius: 12px;
              margin-top: 12px;
              padding: 10px;
              background: #FFFFFF;
            }
            QGroupBox::title {
              subcontrol-origin: margin;
              left: 12px;
              padding: 0 6px 0 6px;
              color: #111827;
            }
            QPushButton {
              background: #0F172A;
              color: white;
              border: none;
              border-radius: 10px;
              padding: 8px 14px;
              font-weight: 600;
            }
            QPushButton:disabled {
              background: #CBD5E1;
              color: #475467;
            }
            QPushButton#secondaryButton {
              background: #F8FAFC;
              color: #111827;
              border: 1px solid #E2E8F0;
            }
            QLineEdit, QPlainTextEdit, QTextBrowser, QTreeWidget {
              border: 1px solid #D0D5DD;
              border-radius: 12px;
              background: #FFFFFF;
              padding: 8px;
            }
            QLabel#HeroTitle {
              font-size: 22px;
              font-weight: 700;
              color: #111827;
            }
            QLabel#HeroSubtitle {
              color: #667085;
              font-size: 12px;
            }
            QLabel#MutedLabel {
              color: #9CA3AF;
              font-size: 12px;
            }
            QFrame#SectionNavFrame {
              background: transparent;
              border: none;
            }
            QFrame#ChatHeightHandle {
              background: #CBD5E1;
              margin: 3px 24px;
              border-radius: 2px;
            }
            QFrame#ChatHeightHandle:hover {
              background: #94A3B8;
            }
            QPushButton#sectionNavButton {
              background: #161B22;
              color: #D1D5DB;
              border: 1px solid #374151;
              border-radius: 10px;
              padding: 6px 12px;
              font-weight: 600;
              font-size: 11px;
            }
            QPushButton#sectionNavButton:hover {
              color: #F9FAFB;
              border-color: #D97706;
            }
            QLabel#StatusBadge {
              border-radius: 12px;
              padding: 5px 10px;
              font-weight: 600;
            }
            QLabel#ApprovalBanner {
              border-radius: 12px;
              padding: 10px 12px;
              background: #FFF8E8;
              color: #7C5A00;
              border: 1px solid #F8CC73;
            }
            QLabel#InputBadge {
              border-radius: 12px;
              padding: 8px 12px;
              background: #F8FAFC;
              color: #344054;
              border: 1px solid #E2E8F0;
              font-weight: 600;
            }
            QFrame#ChatTranscriptFrame,
            QFrame#ChatComposerFrame,
            QFrame#ChatApprovalFrame,
            QFrame#ChatCardFrame {
              background: #111827;
            }
            QFrame#ChatTranscriptFrame QLabel,
            QFrame#ChatComposerFrame QLabel,
            QFrame#ChatApprovalFrame QLabel,
            QFrame#ChatCardFrame QLabel {
              color: #E5E7EB;
            }
            QFrame#ChatCardFrame {
              border: 1px solid #374151;
              border-radius: 18px;
              padding: 0;
            }
            QFrame#ChatTranscriptFrame QLabel#InputBadge,
            QFrame#ChatComposerFrame QLabel#InputBadge,
            QFrame#ChatApprovalFrame QLabel#InputBadge {
              border-radius: 12px;
              padding: 8px 12px;
              background: #161B22;
              color: #D1D5DB;
              border: 1px solid #374151;
              font-weight: 600;
            }
            QFrame#ChatHeaderFrame {
              background: #F8FAFC;
              border: 1px solid #E2E8F0;
              border-radius: 16px;
            }
            QFrame#ChatToolbarFrame {
              background: #111827;
              border: 1px solid #374151;
              border-radius: 16px;
            }
            QFrame#ChatTranscriptFrame {
              background: transparent;
              border: none;
              border-radius: 0;
            }
            QFrame#ChatComposerFrame {
              background: transparent;
              border: none;
              border-radius: 0;
            }
            QFrame#ChatApprovalFrame {
              background: #1E293B;
              border: 1px solid #475569;
              border-radius: 12px;
            }
            QFrame#ChatApprovalFrame QLabel#ApprovalBanner {
              padding: 0;
              background: transparent;
              color: #F8FAFC;
              border: none;
              font-size: 15px;
              font-weight: 700;
            }
            QTextBrowser#ApprovalSummary {
              background: transparent;
              color: #F1F5F9;
              border: none;
              padding: 0;
              font-size: 13px;
            }
            QPlainTextEdit#ApprovalDetails {
              background: #0F172A;
              color: #E5E7EB;
              border: 1px solid #374151;
              border-radius: 8px;
              font-family: "Noto Sans Mono CJK SC", "DejaVu Sans Mono", monospace;
            }
            QTextBrowser#ChatTranscriptView {
              border: none;
              border-radius: 0;
              background: #111827;
              color: #F3F4F6;
              padding: 0;
              font-family: "Noto Sans CJK SC", "Noto Sans CJK JP", "DejaVu Sans", sans-serif;
            }
            QPlainTextEdit#ChatPromptEdit {
              border: none;
              border-radius: 0;
              background: transparent;
              color: #F3F4F6;
              padding: 10px 8px 10px 10px;
              font-family: "Noto Sans CJK SC", "Noto Sans CJK JP", "DejaVu Sans", sans-serif;
            }
            QPlainTextEdit#ChatPromptEdit[plainText=\"\"] {
              color: #9CA3AF;
            }
            QFrame#ChatComposerFrame QPushButton,
            QFrame#ChatApprovalFrame QPushButton {
              background: #D97706;
              color: #111827;
              border: none;
              border-radius: 10px;
              padding: 8px 14px;
              font-weight: 700;
            }
            QFrame#ChatComposerFrame QPushButton#secondaryButton,
            QFrame#ChatApprovalFrame QPushButton#secondaryButton {
              background: #111827;
              color: #E5E7EB;
              border: 1px solid #4B5563;
            }
            QFrame#ChatComposerFrame QCheckBox,
            QFrame#ChatApprovalFrame QCheckBox {
              color: #D1D5DB;
            }
            QFrame#ChatDivider {
              background: #D1D5DB;
              min-height: 1px;
              max-height: 1px;
              border: none;
            }
            QFrame#ChatComposerShell {
              background: #111827;
              border: 1px solid #D1D5DB;
              border-radius: 12px;
            }
            QFrame#ChatComposerShell QPushButton {
              background: #D97706;
              color: #111827;
              border: none;
              border-radius: 18px;
              padding: 0;
              font-weight: 700;
            }
            QFrame#ChatComposerShell QPushButton#secondaryButton {
              background: transparent;
              color: #D1D5DB;
              border: 1px solid #4B5563;
              border-radius: 10px;
              padding: 0 10px;
              min-height: 28px;
              font-weight: 600;
            }
            QFrame#ChatComposerShell QPushButton:hover {
              background: #F59E0B;
            }
            QFrame#ChatComposerShell QPushButton#secondaryButton:hover {
              background: #1F2937;
              border-color: #6B7280;
              color: #F3F4F6;
            }
            QFrame#ChatActivityFrame {
              background: transparent;
              border: none;
            }
            QLabel#ChatActivityLive {
              color: #86EFAC;
              font-size: 12px;
              font-weight: 600;
            }
            QLabel#ChatActivityText {
              color: #D1D5DB;
              font-size: 12px;
            }
            QGroupBox#WorkspaceCard {
              font-weight: 700;
              border: 1px solid #D0D5DD;
              border-radius: 18px;
              margin-top: 18px;
              padding: 16px;
              background: #FCFCFD;
              color: #101828;
            }
            QGroupBox#WorkspaceCard::title {
              subcontrol-origin: margin;
              left: 14px;
              padding: 0 8px 0 8px;
              color: #D97706;
              font-size: 17px;
              font-weight: 900;
            }
            QGroupBox#WorkspaceCard QLabel {
              color: #101828;
            }
            QGroupBox#WorkspaceCard QLineEdit,
            QGroupBox#WorkspaceCard QTextBrowser,
            QGroupBox#WorkspaceCard QTreeWidget,
            QGroupBox#WorkspaceCard QComboBox {
              border: 1px solid #D0D5DD;
              border-radius: 12px;
              background: #FFFFFF;
              color: #101828;
              padding: 8px;
            }
            QGroupBox#WorkspaceCard QTreeWidget::item {
              padding: 4px 0;
            }
            QGroupBox#WorkspaceCard QHeaderView::section {
              background: #F8FAFC;
              color: #344054;
              border: none;
              border-bottom: 1px solid #EAECF0;
              padding: 6px 8px;
              font-weight: 600;
            }
            QGroupBox#WorkspaceCard QPushButton {
              background: #FFFFFF;
              color: #101828;
              border: 1px solid #D0D5DD;
              border-radius: 10px;
              padding: 8px 14px;
              font-weight: 600;
            }
            QGroupBox#WorkspaceCard QPushButton:hover {
              border-color: #D97706;
              color: #101828;
              background: #FFF7ED;
            }
            QGroupBox#WorkspaceCard QCheckBox {
              color: #344054;
            }
            QComboBox#ModelProfileCombo::drop-down:disabled,
            QComboBox#WorkspaceRootCombo::drop-down:disabled {
              border: none;
              width: 0px;
            }
            QComboBox#ModelProfileCombo::down-arrow:disabled,
            QComboBox#WorkspaceRootCombo::down-arrow:disabled {
              image: none;
              width: 0px;
              height: 0px;
            }
            QComboBox#WorkspaceRootCombo:disabled {
              color: #101828;
            }
            """
        self.parent.setStyleSheet(stylesheet)

    def _build_hero(self):
        frame = qt.QFrame()
        layout = qt.QVBoxLayout(frame)
        title = qt.QLabel("dMRI-Agent")
        title.setObjectName("HeroTitle")
        subtitle = qt.QLabel("Files, scene, chat.")
        subtitle.setWordWrap(True)
        subtitle.setObjectName("HeroSubtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        return frame

    def _build_section_nav(self):
        frame = qt.QFrame()
        frame.setObjectName("SectionNavFrame")
        layout = qt.QHBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 6)
        layout.setSpacing(8)
        self.nav_chat_button = qt.QPushButton("Chat")
        self.nav_files_button = qt.QPushButton("Files")
        for button in (
            self.nav_chat_button,
            self.nav_files_button,
        ):
            button.setObjectName("sectionNavButton")
            button.setCursor(qt.Qt.PointingHandCursor)
            layout.addWidget(button, 0)
        layout.addStretch(1)
        self.nav_chat_button.connect(
            "clicked(bool)", lambda _=False: self._scroll_to_widget(self.chat_card)
        )
        self.nav_files_button.connect(
            "clicked(bool)", self._show_files
        )
        return frame

    def _build_session_controls(self):
        frame = qt.QFrame()
        frame.setObjectName("SessionControlsFrame")
        layout = qt.QHBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 6)
        layout.setSpacing(10)
        layout.addWidget(qt.QLabel("Model"))
        self.llm_mode_combo = qt.QComboBox()
        self.llm_mode_combo.setObjectName("ModelProfileCombo")
        self.llm_mode_combo.setToolTip(
            "Switch models while idle to start a new chat. Model selection is locked while a task is running."
        )
        self.llm_mode_combo.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Fixed)
        self.llm_mode_combo.setSizeAdjustPolicy(qt.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.llm_mode_combo.setMinimumContentsLength(12)
        layout.addWidget(self.llm_mode_combo, 1)
        layout.addWidget(self.auto_approve_box)
        return frame

    def _build_chat_resize_handle(self):
        handle = qt.QFrame()
        handle.setObjectName("ChatHeightHandle")
        handle.setFixedHeight(12)
        handle.setCursor(qt.Qt.SizeVerCursor)
        handle.setFocusPolicy(qt.Qt.StrongFocus)
        handle.setAccessibleName("Resize Chat height")
        handle.setToolTip("Drag up/down to resize Chat, or focus here and use the arrow keys")
        self._chat_resize_filter = ChatResizeFilter(self.chat_card, self._set_chat_height)
        handle.installEventFilter(self._chat_resize_filter)
        return handle

    def _set_chat_height(self, height, user=True):
        self._chat_requested_height = max(CHAT_MINIMUM_HEIGHT, int(height))
        self._chat_height_dragged = self._chat_height_dragged or user
        self._apply_chat_height()

    def _apply_chat_height(self):
        layout = self.chat_card.layout()
        layout.invalidate()
        layout.activate()
        minimum = max(CHAT_MINIMUM_HEIGHT, int(layout.minimumSize().height()))
        self.chat_card.setFixedHeight(max(minimum, self._chat_requested_height))
        self.chat_card.updateGeometry()
        self.layout.invalidate()
        self.layout.activate()

    def _fit_initial_chat_height(self):
        if self._chat_height_dragged:
            return
        parent = self.chat_card.parentWidget()
        while parent is not None:
            if isinstance(parent, qt.QScrollArea):
                viewport = parent.viewport()
                top = self.chat_card.mapTo(viewport, qt.QPoint(0, 0)).y()
                available = int(viewport.height) - max(0, top) - int(self.workspace_card.height) - 32
                self._set_chat_height(min(CHAT_DEFAULT_HEIGHT, available), user=False)
                return
            parent = parent.parentWidget()

    def _show_files(self, *_args):
        self.workspace_card.collapsed = False
        self._scroll_to_widget(self.workspace_card)

    def _build_chat_launcher_card(self):
        box = qt.QGroupBox("Chat")
        layout = qt.QGridLayout(box)

        self.open_chat_button = qt.QPushButton("Open")
        self.open_chat_button.setObjectName("secondaryButton")
        layout.addWidget(self.open_chat_button, 0, 0)

        self.chat_launcher_summary_label = qt.QLabel("offline · no session · idle")
        self.chat_launcher_summary_label.setWordWrap(True)
        self.chat_launcher_summary_label.setObjectName("HeroSubtitle")
        layout.addWidget(self.chat_launcher_summary_label, 0, 1, 1, 2)

        return box

    def _build_chat_card(self):
        box = qt.QFrame()
        box.setObjectName("ChatCardFrame")
        box.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Fixed)
        box.setFixedHeight(CHAT_DEFAULT_HEIGHT)
        layout = qt.QVBoxLayout(box)
        # _apply_chat_height owns the frame's bounds. SetMinimumSize would
        # overwrite its minimum during a later label/layout update and snap it
        # back to the content hint after the drag finishes.
        layout.setSizeConstraint(qt.QLayout.SetNoConstraint)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)
        self.conversation_card = self._build_conversation_card()
        self.activity_card = self._build_activity_card()
        self.approval_card = self._build_approval_card()
        self.prompt_card = self._build_prompt_card()
        layout.addWidget(self.conversation_card)
        layout.addWidget(self.activity_card)
        layout.addWidget(self.approval_card)
        divider = qt.QFrame()
        divider.setObjectName("ChatDivider")
        layout.addWidget(divider)
        layout.addWidget(self.prompt_card)
        layout.setStretch(0, 1)
        layout.setStretch(4, 0)
        return box

    def _build_connection_card(self):
        box = qt.QFrame()
        box.setObjectName("ChatToolbarFrame")
        layout = qt.QGridLayout(box)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(8)
        self.api_base_edit = qt.QLineEdit(
            os.environ.get("MRI_AGENT_API_BASE", "http://localhost:18080")
        )
        self.api_base_edit.setVisible(False)
        layout.addWidget(self.api_base_edit, 0, 0, 1, 1)

        self.start_button = qt.QPushButton("Start")
        self.stop_button = qt.QPushButton("Stop")
        self.reconnect_button = qt.QPushButton("Reconnect")
        self.stop_button.setObjectName("secondaryButton")
        self.reconnect_button.setObjectName("secondaryButton")
        self.auto_approve_box = qt.QCheckBox("Auto approve")
        self.stop_button.setEnabled(False)
        self.reconnect_button.setEnabled(False)
        layout.addWidget(self.start_button, 0, 0)
        layout.addWidget(self.stop_button, 0, 1)
        layout.addWidget(self.reconnect_button, 0, 2)

        self.connection_badge = qt.QLabel("Disconnected")
        self.connection_badge.setObjectName("StatusBadge")
        self.session_badge = qt.QLabel("No active session")
        self.session_badge.setObjectName("StatusBadge")
        self.stream_badge = qt.QLabel("Idle")
        self.stream_badge.setObjectName("StatusBadge")
        self.session_label = qt.QLabel("No session")
        self.session_label.setWordWrap(True)
        self.runtime_label = qt.QLabel("Bridge unavailable.")
        self.runtime_label.setWordWrap(True)
        layout.addWidget(self.connection_badge, 1, 0)
        layout.addWidget(self.session_badge, 1, 1)
        layout.addWidget(self.stream_badge, 1, 2)
        layout.addWidget(self.session_label, 2, 0, 1, 5)
        layout.addWidget(self.runtime_label, 3, 0, 1, 5)
        return box

    def _scroll_to_widget(self, target):
        if target is None:
            return
        parent = target.parentWidget()
        while parent is not None:
            if isinstance(parent, qt.QScrollArea):
                parent.ensureWidgetVisible(target, 0, 24)
                return
            parent = parent.parentWidget()
        try:
            target.setFocus()
        except Exception:
            pass

    def _build_workspace_card(self):
        box = ctk.ctkCollapsibleButton()
        box.setText("Files")
        box.setObjectName("WorkspaceCard")
        layout = qt.QGridLayout(box)
        layout.setContentsMargins(16, 24, 16, 16)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(12)

        self.workspace_root_combo = qt.QComboBox()
        self.workspace_root_combo.setObjectName("WorkspaceRootCombo")
        self.workspace_root_combo.setToolTip("Use Choose... to change the workspace folder.")
        self.workspace_root_combo.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Fixed)
        self.workspace_root_combo.setSizeAdjustPolicy(qt.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.workspace_root_combo.setMinimumContentsLength(10)
        self.choose_workspace_button = qt.QPushButton("Choose...")
        self.choose_workspace_button.setObjectName("secondaryButton")
        self.choose_workspace_button.setToolTip(
            "Change the workspace used by Files and Settings. Changing it starts a new chat and keeps the scene and draft."
        )
        self.refresh_tree_button = qt.QPushButton("Refresh Tree")
        self.refresh_tree_button.setObjectName("secondaryButton")
        layout.addWidget(qt.QLabel("Workspace"), 0, 0)
        root_row = qt.QHBoxLayout()
        root_row.addWidget(self.workspace_root_combo, 1)
        root_row.addWidget(self.choose_workspace_button)
        layout.addLayout(root_row, 0, 1, 1, 2)
        layout.addWidget(self.refresh_tree_button, 0, 3)

        self.workspace_search_edit = qt.QLineEdit()
        self.workspace_search_edit.setPlaceholderText(
            "Filter files"
        )
        layout.addWidget(qt.QLabel("Filter"), 1, 0)
        layout.addWidget(self.workspace_search_edit, 1, 1, 1, 3)

        self.workspace_summary_label = qt.QLabel()
        self.workspace_summary_label.setWordWrap(True)
        self.workspace_summary_label.setObjectName("MutedLabel")
        layout.addWidget(self.workspace_summary_label, 2, 0, 1, 4)

        self.workspace_tree = qt.QTreeWidget()
        self.workspace_tree.setColumnCount(5)
        self.workspace_tree.setHeaderLabels(["Name", "Type", "Subject", "Space", "Status"])
        self.workspace_tree.setMinimumHeight(220)
        self.workspace_tree.header().setStretchLastSection(False)
        self.workspace_tree.header().setSectionResizeMode(0, qt.QHeaderView.Stretch)
        for column in (1, 2, 3, 4):
            self.workspace_tree.header().setSectionResizeMode(
                column, qt.QHeaderView.ResizeToContents
            )
        layout.addWidget(self.workspace_tree, 3, 0, 1, 4)

        self.file_metadata = qt.QTextBrowser()
        self.file_metadata.setMaximumHeight(140)
        self.file_details_button = qt.QToolButton()
        self.file_details_button.setText("Selected file details")
        self.file_details_button.setCheckable(True)
        self.file_details_button.setChecked(False)
        self.file_details_button.setArrowType(qt.Qt.RightArrow)
        self.file_details_button.setToolButtonStyle(qt.Qt.ToolButtonTextBesideIcon)
        self.file_details_button.connect("toggled(bool)", self._toggle_file_details)
        layout.addWidget(self.file_details_button, 4, 0, 1, 4)
        layout.addWidget(self.file_metadata, 5, 0, 1, 4)
        self.file_metadata.setVisible(False)

        file_button_row = qt.QHBoxLayout()
        self.load_file_button = qt.QPushButton("Load")
        self.insert_file_context_button = qt.QPushButton("Insert into Prompt")
        self.insert_file_context_button.setObjectName("secondaryButton")
        file_button_row.addWidget(self.load_file_button)
        file_button_row.addWidget(self.insert_file_context_button)
        layout.addLayout(file_button_row, 6, 0, 1, 4)
        box.collapsed = True
        return box

    def _build_conversation_card(self):
        box = qt.QFrame()
        box.setObjectName("ChatTranscriptFrame")
        box.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Expanding)
        layout = qt.QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        self.transcript = qt.QTextBrowser()
        self.transcript.setObjectName("ChatTranscriptView")
        # Keep file paths and code inside the viewport during streamed reflow.
        # QTextBrowser renders Qt rich text; browser-only table CSS is insufficient.
        self.transcript.setLineWrapMode(qt.QTextEdit.WidgetWidth)
        self.transcript.setWordWrapMode(qt.QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.transcript.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        transcript_font = qt.QFont()
        transcript_font.setFamily(UI_FONT_FAMILY)
        transcript_font.setPointSize(10)
        self.transcript.setFont(transcript_font)
        self.transcript.document.setDocumentMargin(10)
        self.transcript.setOpenExternalLinks(False)
        self.transcript.setOpenLinks(False)
        self.transcript.connect("anchorClicked(QUrl)", self._on_transcript_link_clicked)
        self.transcript.setPlaceholderText(
            "Conversation will appear here."
        )
        self.transcript.setMinimumHeight(160)
        self.transcript.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Expanding)
        layout.addWidget(self.transcript)
        return box

    def _build_activity_card(self):
        box = qt.QFrame()
        box.setObjectName("ChatActivityFrame")
        box.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Fixed)
        layout = qt.QHBoxLayout(box)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(8)
        self.activity_live_label = qt.QLabel("● live")
        self.activity_live_label.setObjectName("ChatActivityLive")
        self.activity_text_label = qt.QLabel("ready")
        self.activity_text_label.setObjectName("ChatActivityText")
        self.activity_text_label.setAlignment(qt.Qt.AlignRight | qt.Qt.AlignVCenter)
        layout.addWidget(self.activity_live_label, 0)
        layout.addStretch(1)
        layout.addWidget(self.activity_text_label, 0)
        return box

    def _build_approval_card(self):
        box = qt.QFrame()
        box.setObjectName("ChatApprovalFrame")
        box.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Minimum)
        layout = qt.QVBoxLayout(box)
        layout.setSizeConstraint(qt.QLayout.SetMinimumSize)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        self.approval_label = qt.QLabel()
        self.approval_label.setObjectName("ApprovalBanner")
        self.approval_label.setWordWrap(True)
        layout.addWidget(self.approval_label)
        self.approval_summary_label = qt.QTextBrowser()
        self.approval_summary_label.setObjectName("ApprovalSummary")
        self.approval_summary_label.setMinimumHeight(200)
        self.approval_summary_label.setMaximumHeight(280)
        self.approval_summary_label.setOpenLinks(False)
        self.approval_summary_label.setLineWrapMode(qt.QTextEdit.WidgetWidth)
        self.approval_summary_label.setWordWrapMode(qt.QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.approval_summary_label.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        layout.addWidget(self.approval_summary_label)
        self.approval_details_button = qt.QPushButton("View technical details")
        self.approval_details_button.setObjectName("secondaryButton")
        self.approval_details_button.setCheckable(True)
        self.approval_details_button.connect("toggled(bool)", self._toggle_approval_details)
        layout.addWidget(self.approval_details_button)
        self.approval_details_edit = qt.QPlainTextEdit()
        self.approval_details_edit.setObjectName("ApprovalDetails")
        self.approval_details_edit.setReadOnly(True)
        self.approval_details_edit.setFixedHeight(180)
        self.approval_details_edit.setVisible(False)
        layout.addWidget(self.approval_details_edit)
        note = qt.QLabel("Allow applies only to this operation. Auto approve above controls future requests.")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = qt.QHBoxLayout()
        self.approve_button = qt.QPushButton("Allow this operation")
        self.reject_button = qt.QPushButton("Decline")
        self.reject_button.setObjectName("secondaryButton")
        buttons.addWidget(self.reject_button)
        buttons.addStretch(1)
        buttons.addWidget(self.approve_button)
        layout.addLayout(buttons)
        return box

    def _build_prompt_card(self):
        box = qt.QFrame()
        box.setObjectName("ChatComposerFrame")
        layout = qt.QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        shell = qt.QFrame()
        shell.setObjectName("ChatComposerShell")
        shell_layout = qt.QHBoxLayout(shell)
        shell_layout.setContentsMargins(2, 2, 8, 2)
        shell_layout.setSpacing(6)
        self.prompt_edit = qt.QPlainTextEdit()
        self.prompt_edit.setObjectName("ChatPromptEdit")
        prompt_font = qt.QFont()
        prompt_font.setFamily(UI_FONT_FAMILY)
        prompt_font.setPointSize(10)
        self.prompt_edit.setFont(prompt_font)
        self.prompt_edit.setFixedHeight(76)
        self.prompt_edit.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Fixed)
        self.prompt_edit.setPlaceholderText(
            "Ask dMRI-Agent"
        )
        shell_layout.addWidget(self.prompt_edit, 1)
        # self.one_click_prompt_button = qt.QPushButton("Inject & Send")
        # self.one_click_prompt_button.setObjectName("secondaryButton")
        # self.one_click_prompt_button.setCursor(qt.Qt.PointingHandCursor)
        # self.one_click_prompt_button.setFixedHeight(30)
        # self.one_click_prompt_button.setToolTip(
        #     "Send a pre-configured test prompt. "
        # )
        # shell_layout.addWidget(self.one_click_prompt_button, 0, qt.Qt.AlignVCenter)
        self.send_button = qt.QPushButton("")
        self.send_button.setEnabled(True)
        self.send_button.setCursor(qt.Qt.PointingHandCursor)
        self.send_button.setFixedSize(40, 40)
        self.send_button.setToolTip("Send")
        self.send_button.setIcon(self._create_send_icon())
        self.send_button.setIconSize(qt.QSize(22, 22))
        self.send_button.setAccessibleName("Send message")
        shell_layout.addWidget(self.send_button, 0, qt.Qt.AlignRight | qt.Qt.AlignVCenter)
        layout.addWidget(shell)
        return box

    def _create_send_icon(self):
        size = 24
        pixmap = qt.QPixmap(size, size)
        pixmap.fill(qt.Qt.transparent)
        painter = qt.QPainter(pixmap)
        painter.setRenderHint(qt.QPainter.Antialiasing, True)
        pen = qt.QPen(qt.QColor("#111827"))
        pen.setWidthF(1.8)
        pen.setCapStyle(qt.Qt.RoundCap)
        pen.setJoinStyle(qt.Qt.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(qt.QPointF(6.0, 4.0), qt.QPointF(14.0, 12.0))
        plane = qt.QPolygonF(
            [
                qt.QPointF(6.0, 4.0),
                qt.QPointF(11.0, 18.0),
                qt.QPointF(14.0, 12.0),
                qt.QPointF(20.0, 9.0),
                qt.QPointF(6.0, 4.0),
            ]
        )
        painter.drawPolyline(plane)
        painter.end()
        return qt.QIcon(pixmap)

    def _create_stop_icon(self):
        pixmap = qt.QPixmap(24, 24)
        pixmap.fill(qt.Qt.transparent)
        painter = qt.QPainter(pixmap)
        painter.setRenderHint(qt.QPainter.Antialiasing, True)
        painter.setPen(qt.Qt.NoPen)
        painter.setBrush(qt.QColor("#111827"))
        painter.drawRoundedRect(qt.QRectF(5.0, 5.0, 14.0, 14.0), 2.0, 2.0)
        painter.end()
        return qt.QIcon(pixmap)

    def _update_composer_action(self):
        busy = self.logic.session is not None and (
            self._awaiting_response or self.pending_approval is not None
        )
        self.llm_mode_combo.setEnabled(not busy and not self._cancel_pending)
        action = "stop" if busy else "send"
        if getattr(self, "_composer_action", None) != action:
            self._composer_action = action
            self.send_button.setIcon(
                self._create_stop_icon() if busy else self._create_send_icon()
            )
            self.send_button.setToolTip("Stop current task" if busy else "Send")
            self.send_button.setAccessibleName(
                "Stop current task" if busy else "Send message"
            )
        self.send_button.setEnabled(
            self.connection_badge.text == "Bridge reachable" and not self._cancel_pending
        )


    def show_chat_window(self, _checked: bool = False, focus_prompt: bool = False):
        if focus_prompt:
            self.prompt_edit.setFocus()

    def _update_chat_launcher_summary(self):
        bridge = self.connection_badge.text.lower() if hasattr(self, "connection_badge") else "bridge unreachable"
        session = self.session_badge.text if hasattr(self, "session_badge") else "No active session"
        stream = self.stream_badge.text if hasattr(self, "stream_badge") else "Idle"
        bridge_label = "online" if "reachable" in bridge and "unreachable" not in bridge else "offline"
        if hasattr(self, "chat_launcher_summary_label"):
            self.chat_launcher_summary_label.text = f"{bridge_label} · {session.lower()} · {stream.lower()}"

    def _set_connection_state(self, bridge_reachable: bool):
        session_active = self.logic.session is not None
        if bridge_reachable:
            self.connection_badge.text = "Bridge reachable"
            self.connection_badge.setStyleSheet(
                "background:#ECFDF3;color:#065F46;border:1px solid #86EFAC;"
            )
        else:
            self.connection_badge.text = "Bridge unreachable"
            self.connection_badge.setStyleSheet(
                "background:#FEF3F2;color:#7A271A;border:1px solid #FECACA;"
            )
        self.start_button.setEnabled(bridge_reachable and not session_active)
        self.stop_button.setEnabled(session_active)
        self.reconnect_button.setEnabled(bridge_reachable and session_active)
        self._update_composer_action()
        self._update_chat_launcher_summary()

    def _set_session_status(self, text: str, bg: str, fg: str, border: str):
        self.session_badge.text = text
        self.session_badge.setStyleSheet(
            f"background:{bg};color:{fg};border:1px solid {border};"
        )
        self._update_chat_launcher_summary()

    def _set_stream_status(self, text: str, bg: str, fg: str, border: str):
        self.stream_badge.text = text
        self.stream_badge.setStyleSheet(
            f"background:{bg};color:{fg};border:1px solid {border};"
        )
        self._update_chat_launcher_summary()

    def _format_last_event_age(self) -> str:
        if self._last_event_at <= 0:
            return "never"
        return f"{max(0, int(time.time() - self._last_event_at))}s ago"

    def _render_runtime_status(self):
        parts = [
            f"Last event: {self._last_event_kind}",
            f"Last event {self._format_last_event_age()}",
        ]
        if self._last_error_text:
            parts.append(f"Last error: {self._last_error_text}")
        self.runtime_label.text = " | ".join(parts)
        self._render_chat_activity()

    def _tick_activity_indicator(self):
        self._activity_frame = (self._activity_frame + 1) % 4
        self._render_chat_activity()

    def _render_chat_activity(self):
        if not hasattr(self, "activity_live_label"):
            return
        self._update_composer_action()
        dots = "." * self._activity_frame
        if self.logic.session is None:
            live_text = "○ offline"
            live_color = "#9CA3AF"
            status_text = "idle"
            status_color = "#9CA3AF"
        elif self._cancel_pending:
            live_text = "● live"
            live_color = "#86EFAC"
            status_text = "stopping"
            status_color = "#D97706"
        elif self.pending_approval is not None:
            live_text = "● live"
            live_color = "#86EFAC"
            status_text = f"approval pending{dots}"
            status_color = "#D97706"
        elif self.current_stream_index is not None:
            live_text = "● live"
            live_color = "#86EFAC"
            status_text = f"streaming{dots}"
            status_color = "#D97706"
        elif self._awaiting_response:
            live_text = "● live"
            live_color = "#86EFAC"
            status_text = f"thinking{dots}"
            status_color = "#D97706"
        elif self._last_error_text:
            live_text = "● live"
            live_color = "#FCA5A5"
            status_text = "error"
            status_color = "#FCA5A5"
        else:
            live_text = "● live"
            live_color = "#86EFAC"
            status_text = "ready"
            status_color = "#D1D5DB"
        if self.auto_approve_box.checked:
            status_text = f"{status_text} · auto"
        self.activity_live_label.text = live_text
        self.activity_live_label.setStyleSheet(
            f"color:{live_color};font-size:12px;font-weight:600;"
        )
        self.activity_text_label.text = status_text
        self.activity_text_label.setStyleSheet(
            "color:#D97706;font-size:12px;font-weight:600;"
        )

    def _refresh_runtime_status(self):
        bridge_ok = False
        bridge_error = ""
        try:
            health = self.logic.health(self.api_base_edit.text.strip())
            bridge_ok = bool(health.get("ok"))
        except ServiceError as exc:
            bridge_error = str(exc)

        self._set_connection_state(bridge_ok)
        if not bridge_ok:
            self._set_session_status("Unknown", "#FEF3F2", "#7A271A", "#FECACA")
            self._set_stream_status("Disconnected", "#FEF3F2", "#7A271A", "#FECACA")
            self.runtime_label.text = (
                f"Offline · last event {self._format_last_event_age()} · "
                f"{bridge_error or 'no response'}"
            )
            return

        if self.logic.session is None:
            self._set_session_status("No active session", "#FEF3C7", "#92400E", "#FCD34D")
            self._set_stream_status("Idle", "#FEF3C7", "#92400E", "#FCD34D")
            self.session_label.text = "No session"
            self.runtime_label.text = (
                f"Ready · last event {self._format_last_event_age()}"
            )
            return

        try:
            status = self.logic.session_status()
        except ServiceError as exc:
            self._set_session_status("Session not found", "#FEF3F2", "#7A271A", "#FECACA")
            self._set_stream_status("Disconnected", "#FEF3F2", "#7A271A", "#FECACA")
            self.session_label.text = (
                f"Session {self.logic.session.session_id} · stale"
            )
            self.runtime_label.text = (
                f"Session missing · last event {self._format_last_event_age()} · reconnect or restart"
            )
            return

        alive = bool(status.get("alive"))
        subscribers = status.get("subscriberCount", 0)
        pid = status.get("pid", "—")
        workspace = status.get("workspace", self.logic.session.workspace)
        age_text = self._format_last_event_age()
        if alive:
            self._set_session_status("Active", "#ECFDF3", "#065F46", "#86EFAC")
        else:
            self._set_session_status("Exited", "#FEF3F2", "#7A271A", "#FECACA")
        self.session_label.text = (
            f"Session {status.get('sessionId', self.logic.session.session_id)} · "
            f"PID {pid} · sub {subscribers}"
        )
        if not alive:
            self._set_stream_status("Stopped", "#FEF3F2", "#7A271A", "#FECACA")
            self.runtime_label.text = (
                f"Backend exited · last event {age_text}"
            )
            self._awaiting_response = False
            return

        if self.pending_approval is not None:
            self._set_stream_status("Waiting", "#FEF3C7", "#92400E", "#FCD34D")
            self.runtime_label.text = (
                f"Waiting for {self.pending_approval['type']} approval · {age_text}"
            )
            return

        if subscribers <= 0:
            self._set_stream_status("Stale", "#FEF3C7", "#92400E", "#FCD34D")
            self.runtime_label.text = (
                f"Alive but stream stale · {age_text}"
            )
            return

        if self._awaiting_response:
            if self._last_event_at > 0 and (time.time() - self._last_event_at) > 15:
                self._set_stream_status("Stale", "#FEF3C7", "#92400E", "#FCD34D")
                self.runtime_label.text = (
                    f"Alive but stream stale · {age_text}"
                )
            else:
                self._set_stream_status("Streaming", "#ECFDF3", "#065F46", "#86EFAC")
                self.runtime_label.text = (
                    f"Streaming · {age_text}"
                )
            return

        self._set_stream_status("Ready", "#ECFDF3", "#065F46", "#86EFAC")
        self.runtime_label.text = f"Ready · {age_text}"

    def on_start_session(self):
        self._session_generation += 1
        generation = self._session_generation
        try:
            session_info = self.logic.start_session(
                self.api_base_edit.text.strip(),
                lambda payload: self._dispatch_event(payload, generation),
                lambda exc: self._dispatch_error(exc, generation),
                llm_mode=str(self.llm_mode_combo.itemData(self.llm_mode_combo.currentIndex) or "remote"),
            )
            self.logic.set_auto_approve(bool(self.auto_approve_box.checked))
        except ServiceError as exc:
            slicer.util.errorDisplay(str(exc))
            return False
        self._awaiting_response = False
        self._cancel_pending = False
        self._set_connection_state(True)
        self.session_label.text = f"Session {session_info.session_id} · {session_info.mode}"
        self._set_session_status("Starting", "#EEF2FF", "#312E81", "#C7D2FE")
        self._set_stream_status("Connecting", "#EEF2FF", "#312E81", "#C7D2FE")
        self._last_error_text = ""
        self._last_event_kind = "Session created"
        self._last_event_at = time.time()
        self._render_runtime_status()
        self.render_transcript()
        return True

    def on_stop_session(self):
        self._session_generation += 1
        self.logic.shutdown()
        self.pending_approval = None
        self.current_stream_index = None
        self._awaiting_response = False
        self._cancel_pending = False
        self._set_connection_state(True)
        self.session_label.text = "No session"
        self._set_session_status("Stopped", "#F2F4F7", "#344054", "#D0D5DD")
        self._set_stream_status("Idle", "#FEF3C7", "#92400E", "#FCD34D")
        self._last_event_kind = "Session stopped"
        self._last_event_at = time.time()
        self._render_runtime_status()
        self._set_approval_ui("No pending approval.", enabled=False)
        self.render_transcript()

    def _reset_chat(self):
        self.on_stop_session()
        self._transcript_render_timer.stop()
        self.transcript_entries.clear()
        self._tool_activity_by_call.clear()
        self._tool_activity_labels.clear()
        self._expanded_entries.clear()
        self._reasoning_activity_index = None
        self._transcript_scroll_pending = False
        self._transcript_render_revision += 1
        self._last_error_text = ""
        self._last_event_kind = "New chat"
        self._last_event_at = time.time()
        while not self._pending_events.empty():
            try:
                self._pending_events.get_nowait()
            except queue.Empty:
                break
        self.render_transcript()
        self._refresh_runtime_status()
        self.prompt_edit.setFocus()

    def on_reconnect_stream(self):
        try:
            generation = self._session_generation
            self.logic.reconnect_stream(
                lambda payload: self._dispatch_event(payload, generation),
                lambda exc: self._dispatch_error(exc, generation),
            )
        except ServiceError as exc:
            slicer.util.errorDisplay(str(exc))
            self._last_error_text = str(exc)
            self._render_runtime_status()
            return
        self._last_event_kind = "Stream reconnected"
        self._last_event_at = time.time()
        self._last_error_text = ""
        self._set_stream_status("Reconnected", "#EEF2FF", "#312E81", "#C7D2FE")
        self._render_runtime_status()
        self.render_transcript()

    # def on_send_one_click_prompt(self):
    #     prompt = (self.one_click_prompt or "").strip()
    #     if not prompt:
    #         slicer.util.infoDisplay("No one-click test prompt configured.")
    #         return
    #     self.prompt_edit.setPlainText(prompt)
    #     self.on_send_message()

    def on_send_or_cancel(self):
        if self._awaiting_response or self.pending_approval is not None:
            self.on_cancel_turn()
        else:
            self.on_send_message()

    def on_cancel_turn(self):
        if self._cancel_pending or self.logic.session is None:
            return
        try:
            self.logic.cancel_turn()
        except ServiceError as exc:
            slicer.util.errorDisplay(str(exc))
            return
        self._cancel_pending = True
        if self.pending_approval is not None:
            self._set_approval_ui("Stopping current task...", enabled=False)
        self._last_event_kind = "cancel requested"
        self._last_event_at = time.time()
        self._render_runtime_status()

    def on_send_message(self):
        if self._cancel_pending or self._awaiting_response or self.pending_approval is not None:
            return
        text = self.prompt_edit.toPlainText().strip()
        if not text:
            return
        if self._try_handle_text_approval(text):
            self.prompt_edit.setPlainText("")
            return
        if self.logic.session is None:
            if not self.on_start_session():
                return
        try:
            self.logic.send_message(text)
        except ServiceError as exc:
            slicer.util.errorDisplay(str(exc))
            return
        self._awaiting_response = True
        self._last_event_kind = "message sent"
        self._last_event_at = time.time()
        self._set_stream_status("Streaming", "#ECFDF3", "#065F46", "#86EFAC")
        self._render_runtime_status()
        self.add_entry("ME", text)
        self.prompt_edit.setPlainText("")

    def on_approval(self, approved: bool, *_signal_args):
        if not self.pending_approval:
            return
        try:
            self.logic.respond_to_approval(self.pending_approval, approved)
        except ServiceError as exc:
            slicer.util.errorDisplay(str(exc))
            return
        decision = "approved" if approved else "rejected"
        self.add_entry(
            "SYSTEM",
            f"{self.pending_approval['type'].capitalize()} '{self.pending_approval['label']}' {decision}.",
        )
        self.pending_approval = None
        self._awaiting_response = bool(approved)
        self._last_event_kind = f"approval {decision}"
        self._last_event_at = time.time()
        if approved:
            self._set_stream_status("Streaming", "#ECFDF3", "#065F46", "#86EFAC")
        else:
            self._set_stream_status("Ready", "#ECFDF3", "#065F46", "#86EFAC")
        self._render_runtime_status()
        self._set_approval_ui("No pending approval.", enabled=False)

    def on_approve_with_auto(self):
        if not self.pending_approval:
            return
        self.auto_approve_box.setChecked(True)
        self.on_approval(True)

    def _try_handle_text_approval(self, text: str) -> bool:
        if not self.pending_approval:
            return False
        command = text.strip().lower()
        if command in {"y", "yes", "approve"}:
            self.on_approval(True)
            return True
        if command in {"n", "no", "reject"}:
            self.on_approval(False)
            return True
        if command in {"a", "auto", "yes-auto", "approve-auto", "auto-approve"}:
            self.on_approve_with_auto()
            return True
        return False

    def _sync_auto_approve_from_toolbar(self, checked: bool):
        self.add_entry("SYSTEM", "Auto approve enabled for this session." if checked else "Auto approve disabled.")
        if self.logic.session is not None:
            try:
                self.logic.set_auto_approve(checked)
            except ServiceError as exc:
                self._last_error_text = str(exc)
                self.add_entry("ERROR", str(exc))
                return
        if checked and self.pending_approval is not None:
            self.on_approval(True)
        self._render_chat_activity()

    def on_choose_workspace(self, *_args):
        if self._awaiting_response or self.pending_approval is not None:
            slicer.util.infoDisplay("Stop the current task before changing workspace.")
            return
        path = qt.QFileDialog.getExistingDirectory(
            slicer.util.mainWindow(), "Choose workspace", self._selected_workspace_root()
        )
        if path:
            try:
                self._apply_workspace_folder(path)
            except (OSError, ValueError) as exc:
                slicer.util.errorDisplay(str(exc))

    def _apply_workspace_folder(self, path):
        workspace = Path(path).expanduser().resolve(strict=True)
        if not workspace.is_dir() or workspace == Path(workspace.anchor):
            raise ValueError("Choose a workspace folder other than the filesystem root.")
        os.environ["MRI_AGENT_WORKSPACE"] = str(workspace)
        os.environ["MRI_AGENT_FILE_ROOTS"] = str(workspace)
        os.environ["DMRI_DEFAULT_WORKSPACE"] = str(workspace)
        self.refresh_workspace_roots()
        self.refresh_workspace_tree()
        return workspace

    def _toggle_file_details(self, expanded):
        self.file_metadata.setVisible(expanded)
        self.file_details_button.setArrowType(qt.Qt.DownArrow if expanded else qt.Qt.RightArrow)

    def refresh_workspace_roots(self):
        current_path = self._selected_workspace_root()
        self.workspace_root_combo.blockSignals(True)
        self.workspace_root_combo.clear()
        roots = default_workspace_roots()
        if not roots:
            self.workspace_root_combo.addItem("No valid workspace roots found", "")
            self.workspace_root_combo.setEnabled(False)
            self.workspace_root_combo.blockSignals(False)
            return
        selected_index = 0
        for index, (label, path) in enumerate(roots):
            self.workspace_root_combo.addItem(f"{label} — {path}", path)
            if path == current_path:
                selected_index = index
        self.workspace_root_combo.setCurrentIndex(selected_index)
        self.workspace_root_combo.setEnabled(len(roots) > 1)
        self.workspace_root_combo.blockSignals(False)

    def refresh_workspace_tree(self):
        selected = self._selected_workspace_item()
        selected_path = selected["path"] if selected else ""
        self.workspace_tree.clear()
        root_path = self._selected_workspace_root()
        if not root_path:
            self.file_metadata.setHtml(
                "<p style='color:#667085;'>No workspace root.</p>"
            )
            self._set_workspace_buttons_enabled(False)
            self.workspace_summary_label.text = "No workspace root."
            return
        root_dir = Path(root_path)
        current_text = self.workspace_root_combo.currentText
        root_label = current_text.split(" — ", 1)[0] if current_text else root_dir.name
        root_metadata = self._workspace_metadata(str(root_dir), root_label, kind_override="folder")
        root_item = qt.QTreeWidgetItem(
            [
                root_dir.name or root_path,
                KIND_LABELS["folder"],
                root_metadata["subjectId"] or "—",
                root_metadata["space"] or "unknown",
                self._status_for_path(str(root_dir), "folder"),
            ]
        )
        self._set_tree_item_data(root_item, root_metadata, True)
        self.workspace_tree.addTopLevelItem(root_item)
        self._populate_tree(root_item, root_dir, root_label, 0)
        root_item.setExpanded(True)
        self._refresh_workspace_statuses()
        restored_selection = self._select_workspace_path(selected_path) if selected_path else False
        if not restored_selection:
            self.file_metadata.setHtml(
                "<p style='color:#667085;'>Select a file.</p>"
            )
            self._set_workspace_buttons_enabled(False)

    def _populate_tree(self, parent_item, directory: Path, root_label: str, depth: int):
        if depth >= MAX_TREE_DEPTH:
            parent_item.addChild(qt.QTreeWidgetItem(["… deeper levels hidden", "Truncated"]))
            return
        try:
            children = sorted(
                [child for child in directory.iterdir() if not child.name.startswith(".")],
                key=lambda entry: (entry.is_file(), entry.name.lower()),
            )
        except OSError as exc:
            parent_item.addChild(qt.QTreeWidgetItem([f"[unreadable] {exc}", "Error"]))
            return

        for index, child in enumerate(children):
            if index >= MAX_TREE_ITEMS_PER_DIR:
                parent_item.addChild(
                    qt.QTreeWidgetItem(
                        [f"… {len(children) - MAX_TREE_ITEMS_PER_DIR} more entries hidden", "Truncated", "—", "—", "—"]
                    )
                )
                break
            if child.is_dir():
                metadata = self._workspace_metadata(str(child), root_label, kind_override="folder")
                item = qt.QTreeWidgetItem(
                    [
                        child.name,
                        KIND_LABELS["folder"],
                        metadata["subjectId"] or "—",
                        metadata["space"] or "unknown",
                        self._status_for_path(str(child), "folder"),
                    ]
                )
                self._set_tree_item_data(item, metadata, True)
                parent_item.addChild(item)
                self._populate_tree(item, child, root_label, depth + 1)
            else:
                metadata = self._workspace_metadata(str(child), root_label)
                kind = metadata["kind"]
                item = qt.QTreeWidgetItem(
                    [
                        child.name,
                        KIND_LABELS.get(kind, kind),
                        metadata["subjectId"] or "—",
                        metadata["space"] or "unknown",
                        self._status_for_path(str(child), kind),
                    ]
                )
                self._set_tree_item_data(item, metadata, False)
                parent_item.addChild(item)

    def _set_tree_item_data(self, item, metadata: dict[str, Any], is_dir: bool):
        item.setData(0, qt.Qt.UserRole, metadata.get("path", ""))
        item.setData(0, qt.Qt.UserRole + 1, metadata.get("rootLabel", ""))
        item.setData(0, qt.Qt.UserRole + 2, int(is_dir))
        item.setData(0, qt.Qt.UserRole + 3, metadata.get("kind", ""))
        item.setData(0, qt.Qt.UserRole + 4, metadata.get("subjectId", ""))
        item.setData(0, qt.Qt.UserRole + 5, metadata.get("space", ""))

    def _selected_workspace_item(self):
        item = self.workspace_tree.currentItem()
        if item is None:
            return None
        path = item.data(0, qt.Qt.UserRole)
        root_label = item.data(0, qt.Qt.UserRole + 1) or ""
        is_dir = bool(item.data(0, qt.Qt.UserRole + 2))
        kind = item.data(0, qt.Qt.UserRole + 3) or ""
        subject_id = item.data(0, qt.Qt.UserRole + 4) or ""
        space = item.data(0, qt.Qt.UserRole + 5) or ""
        if not path:
            return None
        return {
            "path": str(path),
            "rootLabel": str(root_label),
            "isDir": is_dir,
            "kind": str(kind),
            "subjectId": str(subject_id),
            "space": str(space),
        }

    def _selected_workspace_root(self) -> str:
        index = self.workspace_root_combo.currentIndex
        if index < 0:
            return ""
        value = self.workspace_root_combo.itemData(index)
        return str(value or "")

    def on_workspace_selection_changed(self):
        self.file_details_button.setChecked(False)
        item = self._selected_workspace_item()
        if item is None:
            self._set_workspace_buttons_enabled(False)
            return
        if item["isDir"]:
            directory = Path(item["path"])
            summary = self._directory_summary_html(directory, item["rootLabel"])
            self.file_metadata.setHtml(summary)
            self._set_workspace_buttons_enabled(False)
            return
        metadata = collect_file_metadata(item["path"], item["rootLabel"])
        self.file_metadata.setHtml(self._metadata_to_html(metadata, title="Selected file"))
        self._set_workspace_buttons_enabled(True)
        self.load_file_button.setEnabled(item["kind"] in SUPPORTED_FILE_KINDS)

    def on_workspace_item_activated(self, _item=None, _column=0):
        selected = self._selected_workspace_item()
        if selected and not selected["isDir"] and selected["kind"] in SUPPORTED_FILE_KINDS:
            self.on_load_selected_file()

    def on_load_selected_file(self):
        selected = self._selected_workspace_item()
        if selected is None or selected["isDir"]:
            slicer.util.infoDisplay("Choose a file from the workspace tree first.")
            return
        try:
            asset = self._load_path_into_scene(selected["path"], selected["rootLabel"])
        except Exception as exc:  # noqa: BLE001
            slicer.util.errorDisplay(str(exc))
            return
        if asset is not None:
            self.add_entry(
                "SYSTEM",
                f"Loaded {Path(selected['path']).name} into the scene as {asset.kind}.",
            )
            self._refresh_workspace_statuses()
        else:
            self.add_entry("SYSTEM", f"Loaded scene file {Path(selected['path']).name}.")

    def sync_scene_assets(self):
        for class_name in SCENE_NODE_CLASSES:
            try:
                nodes = slicer.util.getNodesByClass(class_name)
            except Exception:  # noqa: BLE001
                continue
            for node in nodes:
                self.asset_registry.register_node(node)
        self._refresh_workspace_statuses()

    def on_insert_selected_file_into_prompt(self):
        selected = self._selected_workspace_item()
        if selected is None or selected["isDir"]:
            slicer.util.infoDisplay("Choose a file from the workspace tree first.")
            return
        cursor = self.prompt_edit.textCursor()
        current = self.prompt_edit.toPlainText()
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        prefix = " " if start and not current[start - 1].isspace() else ""
        suffix = " " if end < len(current) and not current[end].isspace() else ""
        cursor.insertText(prefix + selected["path"] + suffix)
        self.prompt_edit.setTextCursor(cursor)
        self.show_chat_window(focus_prompt=True)

    def _load_path_into_scene(self, path: str, root_label: str):
        existing = self.asset_registry.get_by_path(path)
        if existing is not None:
            node = slicer.mrmlScene.GetNodeByID(existing.mrml_node_id)
            if node is not None:
                self._apply_default_visualization(node, existing.kind)
            return existing

        kind = detect_file_kind(path)
        if kind == "scene":
            if not slicer.util.loadScene(path):
                raise RuntimeError(f"Failed to load Slicer scene: {path}")
            self.sync_scene_assets()
            return None

        file_type = self._file_type_for_kind(kind)
        if file_type is None:
            raise RuntimeError(f"Unsupported file type for loading: {path}")
        properties = {"labelmap": True} if kind == "labelmap" else {}
        loaded = slicer.util.loadNodeFromFile(path, file_type, properties, returnNode=True)
        node = self._normalize_loaded_node(loaded)
        if node is None:
            raise RuntimeError(f"Slicer did not return a loaded node for: {path}")
        if kind == "labelmap" and not node.IsA("vtkMRMLLabelMapVolumeNode"):
            slicer.mrmlScene.RemoveNode(node)
            raise RuntimeError(f"Slicer did not load the mask as a labelmap: {path}")
        asset = self.asset_registry.register_node(
            node,
            workspace_path=path,
            source_root=root_label,
            kind=kind,
        )
        self._apply_default_visualization(node, asset.kind)
        self._refresh_workspace_statuses()
        return asset

    def _file_type_for_kind(self, kind: str) -> Optional[str]:
        mapping = {
            "scalar_volume": "VolumeFile",
            "diffusion_volume": "VolumeFile",
            "labelmap": "VolumeFile",
            "segmentation": "SegmentationFile",
            "fiber_bundle": "FiberBundleFile",
            "model": "ModelFile",
            "markups": "MarkupsFile",
        }
        return mapping.get(kind)

    def _normalize_loaded_node(self, loaded):
        if hasattr(loaded, "GetID"):
            return loaded
        if isinstance(loaded, tuple) and len(loaded) == 2:
            success, node = loaded
            if success and hasattr(node, "GetID"):
                return node
        return None

    def _apply_default_visualization(self, node, kind: str):
        app_logic = slicer.app.applicationLogic()
        selection_node = app_logic.GetSelectionNode()
        if kind in {"scalar_volume", "diffusion_volume"}:
            try:
                node.CreateDefaultDisplayNodes()
            except AttributeError:
                pass
            display_node = node.GetDisplayNode() if hasattr(node, "GetDisplayNode") else None
            if display_node is not None:
                if hasattr(display_node, "AutoWindowLevelOn"):
                    display_node.AutoWindowLevelOn()
                if hasattr(display_node, "SetVisibility"):
                    display_node.SetVisibility(True)
            selection_node.SetReferenceActiveVolumeID(node.GetID())
            selection_node.SetReferenceSecondaryVolumeID("")
            selection_node.SetReferenceActiveLabelVolumeID("")
            app_logic.PropagateVolumeSelection()
            slicer.util.setSliceViewerLayers(
                background=node,
                foreground=None,
                label=None,
                fit=True,
                rotateToVolumePlane=True,
            )
            qt.QTimer.singleShot(
                120,
                lambda node_id=node.GetID(): self._fit_active_volume_views(node_id),
            )
            return
        if kind == "labelmap":
            try:
                selection_node.SetReferenceActiveLabelVolumeID(node.GetID())
                app_logic.PropagateVolumeSelection()
            except AttributeError:
                pass
            slicer.util.resetSliceViews()
            return
        try:
            node.CreateDefaultDisplayNodes()
        except AttributeError:
            pass
        display_node = node.GetDisplayNode() if hasattr(node, "GetDisplayNode") else None
        if display_node is None:
            return
        try:
            display_node.SetVisibility(True)
        except AttributeError:
            pass
        if kind == "segmentation":
            for method_name in ("SetVisibility2D", "SetVisibility3D"):
                if hasattr(display_node, method_name):
                    getattr(display_node, method_name)(True)
        if kind in {"fiber_bundle", "model", "markups"} and hasattr(display_node, "SetVisibility3D"):
            display_node.SetVisibility3D(True)
        if kind in {"fiber_bundle", "model"}:
            try:
                slicer.util.resetThreeDViews()
            except Exception:
                pass

    def _set_workspace_buttons_enabled(self, enabled: bool):
        self.load_file_button.setEnabled(enabled)
        self.insert_file_context_button.setEnabled(enabled)

    def _metadata_to_html(self, metadata: dict[str, Any], title: str = "Metadata") -> str:
        rows = []
        for key in sorted(metadata.keys()):
            value = metadata[key]
            if value in ("", None):
                continue
            rows.append(
                f"<tr><td style='padding:2px 8px 2px 0;color:#667085;vertical-align:top;'>{html.escape(str(key))}</td>"
                f"<td style='padding:2px 0;color:#101828;'>{html.escape(str(value))}</td></tr>"
            )
        if not rows:
            rows.append(
                "<tr><td colspan='2' style='color:#667085;'>No structured metadata available.</td></tr>"
            )
        return (
            f"<div style=\"font-family:{UI_FONT_STACK};color:#101828;\">"
            f"<b style='color:#D97706;'>{html.escape(title)}</b><br><table style='margin-top:6px;'>"
            + "".join(rows)
            + "</table></div>"
        )

    def _markdown_to_html_fragment(self, text: str, color: str) -> str:
        clean = textwrap.dedent(text or "").strip("\n")
        if not clean:
            return ""
        body = render_markdown_fragment(clean)
        return (
            f"<div lang='zh-CN' style=\"font-family:{UI_FONT_STACK};color:{color};"
            "font-size:13px;line-height:150%;\">"
            "<style>"
            "p { margin:0 0 9px 0; }"
            "li { margin:0 0 5px 0; }"
            "ul, ol { margin:0 0 10px 22px; padding-left:8px; }"
            "blockquote { margin:8px 0; padding:7px 10px; border-left:3px solid #D97706; color:#D1D5DB; }"
            "hr { color:#4B5563; background:#4B5563; height:1px; border:0; margin:10px 0; }"
            f"pre, code {{ font-family:{MONO_FONT_STACK}; }}"
            "pre { white-space:pre-wrap; background:#0F172A; color:#F3F4F6; padding:10px; border:1px solid #374151; }"
            "code { background:#0F172A; color:#FDE68A; padding:1px 4px; }"
            "h1, h2, h3, h4, h5, h6 { color:#F1F5F9; margin:15px 0 7px 0; font-weight:700; }"
            "h1 { font-size:17px; } h2 { font-size:15px; } h3 { font-size:14px; }"
            "h4, h5, h6 { font-size:14px; }"
            "a { color:#93C5FD; } strong { color:#F9FAFB; }"
            "</style>"
            f"{body}</div>"
        )

    def _directory_summary_html(self, directory: Path, root_label: str) -> str:
        try:
            children = [child for child in directory.iterdir() if not child.name.startswith(".")]
        except OSError as exc:
            return f"<p style='color:#B42318;'>Failed to read directory: {html.escape(str(exc))}</p>"
        subdirs = sum(1 for child in children if child.is_dir())
        supported_files = sum(
            1
            for child in children
            if child.is_file() and detect_file_kind(child) in SUPPORTED_FILE_KINDS
        )
        data = {
            "path": str(directory),
            "rootLabel": root_label,
            "subjectId": "",
            "space": "",
            "subdirectories": subdirs,
            "supportedFiles": supported_files,
        }
        return self._metadata_to_html(data, title="Selected directory")

    def _normalize_agent_output_path(self, raw_path: str) -> str:
        path = str(raw_path or "").strip()
        if not path:
            return path
        aliases: list[tuple[str, str]] = []
        aliases_env = os.environ.get("MRI_AGENT_PATH_ALIASES", "").strip()
        if aliases_env:
            for item in aliases_env.split(","):
                token = item.strip()
                if not token or "=" not in token:
                    continue
                legacy_prefix, canonical_prefix = token.split("=", 1)
                legacy_prefix = legacy_prefix.strip().rstrip("/\\")
                canonical_prefix = canonical_prefix.strip().rstrip("/\\")
                if legacy_prefix and canonical_prefix:
                    aliases.append((legacy_prefix, canonical_prefix))
        for legacy_prefix, canonical_prefix in aliases:
            if path == legacy_prefix or path.startswith(legacy_prefix + "/"):
                suffix = path[len(legacy_prefix):]
                normalized = canonical_prefix + suffix
                normalized_path = Path(normalized)
                # Prefer canonical processed root whenever available.
                if normalized_path.exists() or not Path(path).exists():
                    return normalized
        if "/data_processed/" in path:
            normalized = path.replace("/data_processed/", "/processed/")
            if Path(normalized).exists() or not Path(path).exists():
                return normalized
        if path.endswith("/data_processed"):
            normalized = path[: -len("/data_processed")] + "/processed"
            if Path(normalized).exists() or not Path(path).exists():
                return normalized
        return path

    def _prioritize_primary_tract_outputs(self, paths: list[str]) -> list[str]:
        if not paths:
            return paths
        seen: set[str] = set()
        selected: list[str] = []
        second_tensor_candidates: list[str] = []
        for path in paths:
            lower = path.lower()
            if "tractswithsecondtensor" in lower:
                second_tensor_candidates.append(path)
                continue
            if path not in seen:
                selected.append(path)
                seen.add(path)
        if selected:
            return selected
        # Fallback: keep second-tensor outputs only if no primary output exists.
        for path in second_tensor_candidates:
            if path not in seen:
                selected.append(path)
                seen.add(path)
        return selected

    def _register_output_paths(self, paths: list[str]):
        changed = False
        normalized_paths = [
            self._normalize_agent_output_path(str(raw_path or "").strip())
            for raw_path in paths
        ]
        for path in self._prioritize_primary_tract_outputs(normalized_paths):
            if not path:
                continue
            output_path = Path(path)
            metadata = collect_file_metadata(path, "Agent output") if output_path.exists() else {
                "path": path,
                "name": output_path.name or path,
                "kind": "folder" if str(path).endswith("/") else detect_file_kind(path),
                "subjectId": "",
                "space": infer_space(path),
                "suffix": "".join(output_path.suffixes) or output_path.suffix,
                "rootLabel": "Agent output",
                "missing": True,
            }
            self.recent_outputs[path] = metadata
            changed = True
        if changed:
            self.refresh_workspace_roots()
            self.refresh_workspace_tree()
            self.sync_scene_assets()

    def _dispatch_event(self, payload, generation=None):
        generation = self._session_generation if generation is None else generation
        self._pending_events.put((generation, "event", payload))

    def _dispatch_error(self, exc, generation=None):
        generation = self._session_generation if generation is None else generation
        self._pending_events.put((generation, "error", str(exc)))

    def _drain_pending_events(self):
        while True:
            try:
                record = self._pending_events.get_nowait()
            except queue.Empty:
                break
            if len(record) != 3:
                continue
            generation, kind, payload = record
            if generation != self._session_generation:
                continue
            self._last_event_at = time.time()
            if kind == "event":
                self._last_event_kind = payload.get("type") or payload.get("_sse_event") or "event"
                finish_reason = (payload.get("value") or {}).get("reason") if self._last_event_kind == "finished" else None
                if self._last_event_kind != "error" and finish_reason != "length":
                    self._last_error_text = ""
                self.handle_event(payload)
            else:
                self._last_error_text = str(payload)
                self._last_event_kind = "stream error"
                self._awaiting_response = False
                self.add_entry("ERROR", str(payload))

    def handle_event(self, payload):
        kind = payload.get("type")
        if payload.get("_sse_event") == "ready":
            self._set_session_status("Streaming", "#ECFDF3", "#065F46", "#86EFAC")
            self._set_stream_status("Ready", "#ECFDF3", "#065F46", "#86EFAC")
            return
        if kind == "content":
            text = payload.get("value")
            if isinstance(text, str) and text:
                self.append_stream("AGENT", text)
            return
        if kind == "thought":
            value = payload.get("value") or {}
            if isinstance(value, dict):
                description = str(value.get("description") or "")
                if description:
                    index = self._reasoning_activity_index
                    if index is None or index >= len(self.transcript_entries):
                        self._reasoning_activity_index = self.add_entry("THINKING", description)
                    else:
                        self.transcript_entries[index]["text"] = description
                        self._transcript_render_timer.stop()
                        self.render_transcript()
            return
        if kind == "finished":
            reply_index = self.current_stream_index
            scrollbar = self.transcript.verticalScrollBar()
            follow_reply = scrollbar.maximum - scrollbar.value <= 32 or self._transcript_scroll_pending
            self.current_stream_index = None
            self._reasoning_activity_index = None
            self._awaiting_response = False
            self._cancel_pending = False
            if self.pending_approval is not None:
                self.pending_approval = None
                self._set_approval_ui("No pending approval.", enabled=False)
            finish_reason = (payload.get("value") or {}).get("reason")
            if finish_reason == "cancelled":
                self.add_entry("STATUS", "Task stopped by user.")
            elif finish_reason == "length":
                self._last_error_text = "Model output was truncated; task incomplete."
                self.add_entry("STATUS", "Model output limit reached. Task incomplete; you can ask the agent to continue.")
            self._render_runtime_status()
            self._transcript_render_timer.stop()
            self.render_transcript()
            if follow_reply and reply_index is not None and finish_reason not in {"cancelled", "length"}:
                qt.QTimer.singleShot(0, lambda: self.transcript.scrollToAnchor(f"message-{reply_index}"))
            return
        if kind == "info":
            self.add_entry("INFO", str(payload.get("value", "")))
            return
        if kind == "error":
            value = payload.get("value", {})
            if isinstance(value, dict):
                message = value.get("error", {}).get("message", value)
            else:
                message = value
            self._awaiting_response = False
            self._cancel_pending = False
            self._last_error_text = str(message)
            self.add_entry("ERROR", str(message))
            return
        if kind == "usage":
            return
        if kind == "plan_ready":
            value = payload.get("value", {})
            label = value.get("current") or "Plan"
            preview, details = plan_review(value)
            self.pending_approval = {
                "type": "plan",
                "id": value.get("planId", ""),
                "label": label,
            }
            self._awaiting_response = False
            self._set_approval_ui(
                "Review plan before execution",
                enabled=True,
                summary=preview,
                details=details,
            )
            self.add_entry("PLAN", details, summary=f"Plan · {preview}")
            if self.auto_approve_box.checked:
                self.on_approval(True)
            return
        if kind in {"tool_call_request", "tool_call"}:
            value = payload.get("value", {})
            call_id = value.get("callId") or value.get("call_id") or ""
            name = value.get("name", "tool")
            args = value.get("args") or {}
            action = tool_activity(name, args)
            is_stage = name in {"execute_stage", "execute_slicer_stage"}
            preview, details = tool_review(name, args)
            self.pending_approval = {"type": "tool", "id": call_id, "label": action}
            self._awaiting_response = False
            self._set_approval_ui(
                "Allow this processing stage?" if is_stage else "Allow this operation?",
                enabled=True,
                summary=preview,
                details=details,
            )
            if is_stage and details:
                self.add_entry("PLAN", details, summary=f"Plan · {preview}")
            self._upsert_tool_activity(call_id, f"Tool: {name}\n\n{details}", f"Awaiting approval · {action}")
            if self.auto_approve_box.checked:
                self.on_approval(True)
            return
        if kind == "tool_call_start":
            value = payload.get("value", {})
            call_id = value.get("callId") or value.get("call_id") or ""
            name = value.get("name", "tool")
            args = value.get("args") or {}
            action = tool_activity(name, args)
            self._tool_activity_labels[call_id] = action
            details = f"Tool: {name}\n\nArguments:\n{self._compact_json(args)}"
            self._upsert_tool_activity(call_id, details, f"Running · {action}")
            return
        if kind in {"tool_call_response", "tool_response"}:
            value = payload.get("value", {})
            output = value.get("output", "")
            name = value.get("name", "tool")
            call_id = value.get("callId") or value.get("call_id") or ""
            action = self._tool_activity_labels.pop(call_id, tool_activity(name, {}))
            output_paths = [str(path) for path in value.get("output_paths") or []]
            extra = ""
            if output_paths:
                extra = "\n\nOutputs:\n" + "\n".join(output_paths)
                self._register_output_paths(output_paths)
            body = output.strip() if isinstance(output, str) else self._compact_json(output)
            if len(body) > 6000:
                body = body[:6000] + "\n[Output preview truncated in UI]"
            outcome = tool_outcome(output)
            self._upsert_tool_activity(
                call_id, f"Tool: {name}\n\n{body or 'completed'}{extra}",
                f"{outcome} · {action}",
            )
            return
        if kind in {"stage_start", "stage_done"}:
            value = payload.get("value", {})
            stage = str(value.get("stage") or "processing stage")
            if kind == "stage_start":
                self.add_entry("STATUS", f"Started · {stage}")
            else:
                status = str(value.get("status") or "incomplete").lower()
                elapsed = value.get("elapsed")
                timing = f" · {round(float(elapsed))}s" if isinstance(elapsed, (int, float)) else ""
                prefix = {"done": "Completed", "failed": "Failed", "error": "Failed", "cancelled": "Cancelled"}.get(status, "Incomplete")
                self.add_entry("STATUS", f"{prefix} · {stage}{timing}")
            return
        if kind == "shutdown_complete":
            self._awaiting_response = False
            self._set_session_status("Stopped", "#F2F4F7", "#344054", "#D0D5DD")
            self._set_stream_status("Idle", "#FEF3C7", "#92400E", "#FCD34D")

    def add_entry(self, role: str, text: str, summary: str = ""):
        if not text:
            return
        if role in {"INFO", "SYSTEM", "USAGE"}:
            return
        self.transcript_entries.append({"role": role, "text": text, "summary": summary})
        self._transcript_render_timer.stop()
        self.render_transcript()
        return len(self.transcript_entries) - 1

    def _upsert_tool_activity(self, call_id: str, text: str, summary: str):
        index = self._tool_activity_by_call.get(call_id)
        if index is None or index >= len(self.transcript_entries):
            index = self.add_entry("TOOL", text, summary=summary)
            if call_id and index is not None:
                self._tool_activity_by_call[call_id] = index
            return
        self.transcript_entries[index].update(text=text, summary=summary)
        self._transcript_render_timer.stop()
        self.render_transcript()

    def append_stream(self, role: str, text: str):
        if self.current_stream_index is None or self.current_stream_index != len(self.transcript_entries) - 1:
            self.transcript_entries.append({"role": role, "text": text})
            self.current_stream_index = len(self.transcript_entries) - 1
        else:
            self.transcript_entries[self.current_stream_index]["text"] += text
        if not self._transcript_render_timer.isActive():
            self._transcript_render_timer.start()

    def _on_transcript_link_clicked(self, url):
        address = url.toString()
        prefix, separator, raw_index = address.partition(":")
        if prefix not in {"entry", "activity", "reply"} or not separator:
            return
        try:
            index = int(raw_index)
        except ValueError:
            return
        if not 0 <= index < len(self.transcript_entries):
            return
        if prefix == "reply":
            self.transcript.scrollToAnchor(f"message-{index}")
            return
        key = index if prefix == "entry" else f"activity:{index}"
        if key in self._expanded_entries:
            self._expanded_entries.remove(key)
        else:
            self._expanded_entries.add(key)
        self.render_transcript()

    def render_transcript(self):
        scroll_bar = self.transcript.verticalScrollBar()
        old_value = scroll_bar.value
        old_maximum = scroll_bar.maximum
        if callable(old_value):
            old_value = old_value()
        if callable(old_maximum):
            old_maximum = old_maximum()
        was_at_bottom = self._transcript_scroll_pending or old_maximum - old_value <= 32
        body = render_conversation(
            self.transcript_entries, self._expanded_entries, self._markdown_to_html_fragment,
            working=bool(self._awaiting_response),
        )
        self.transcript.setHtml(
            f"<div lang='zh-CN' style=\"font-family:{UI_FONT_STACK};background:#111827;\">"
            + body + "</div>"
        )
        self._transcript_render_revision += 1
        revision = self._transcript_render_revision
        if was_at_bottom:
            self._transcript_scroll_pending = True
        else:
            maximum = scroll_bar.maximum
            if callable(maximum):
                maximum = maximum()
            scroll_bar.setValue(min(old_value, maximum))
        # QTextBrowser updates its scroll range after setHtml returns. Scroll on
        # the next UI event so streamed content stays visible above the activity bar.
        qt.QTimer.singleShot(0, lambda: self._settle_transcript_scroll(revision, was_at_bottom, old_value))

    def _settle_transcript_scroll(self, revision: int, follow_tail: bool, old_value: int):
        if revision != self._transcript_render_revision:
            return
        scroll_bar = self.transcript.verticalScrollBar()
        maximum = scroll_bar.maximum
        if callable(maximum):
            maximum = maximum()
        scroll_bar.setValue(maximum if follow_tail else min(old_value, maximum))
        self._transcript_scroll_pending = False

    def _compact_json(self, value: Any) -> str:
        if value in (None, "", {}, []):
            return ""
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, indent=2)

    def _summarize_event(self, kind: str, value: Any) -> str:
        if isinstance(value, str):
            return f"{kind} · {value}"
        if not isinstance(value, dict):
            return f"{kind} · {self._compact_json(value)}"
        parts = []
        for key in ("status", "stage", "name", "message", "detail", "path"):
            raw = value.get(key)
            if raw:
                parts.append(str(raw))
        if not parts:
            return f"{kind} · {self._compact_json(value)}"
        return " · ".join([kind, *parts])

    def _workspace_metadata(
        self,
        path: str,
        root_label: str,
        kind_override: Optional[str] = None,
    ) -> dict[str, Any]:
        metadata = collect_file_metadata(path, root_label)
        if kind_override is not None:
            metadata["kind"] = kind_override
        return metadata

    def _workspace_filter_query(self) -> str:
        return self.workspace_search_edit.text.strip().lower()

    def _item_matches_workspace_filters(self, item) -> bool:
        query = self._workspace_filter_query()
        if not query:
            return True
        values = [
            item.text(0),
            str(item.data(0, qt.Qt.UserRole) or ""),
            str(item.data(0, qt.Qt.UserRole + 4) or ""),
            str(item.data(0, qt.Qt.UserRole + 5) or ""),
            str(item.data(0, qt.Qt.UserRole + 3) or ""),
        ]
        return any(query in value.lower() for value in values if value)

    def _apply_workspace_filters(self, *_args):
        if self.workspace_tree.topLevelItemCount == 0:
            self.workspace_summary_label.text = "No workspace data is loaded."
            return

        root_item = self.workspace_tree.topLevelItem(0)
        stats = {"visibleDirs": 0, "visibleFiles": 0}

        def walk(item):
            is_dir = bool(item.data(0, qt.Qt.UserRole + 2))
            matches_self = self._item_matches_workspace_filters(item)
            child_visible = False
            for index in range(item.childCount()):
                if walk(item.child(index)):
                    child_visible = True
            visible = matches_self or child_visible or item is root_item
            item.setHidden(not visible)
            if visible:
                if is_dir:
                    stats["visibleDirs"] += 1
                else:
                    stats["visibleFiles"] += 1
            return visible

        walk(root_item)
        self.workspace_tree.expandToDepth(1 if self._workspace_filter_query() else 0)
        self.workspace_summary_label.text = (
            f"{stats['visibleFiles']} files · {stats['visibleDirs']} folders"
        )

    def _refresh_workspace_statuses(self):
        def walk(item):
            path = str(item.data(0, qt.Qt.UserRole) or "")
            kind = str(item.data(0, qt.Qt.UserRole + 3) or "")
            if path:
                item.setText(4, self._status_for_path(path, kind))
            for index in range(item.childCount()):
                walk(item.child(index))

        for index in range(self.workspace_tree.topLevelItemCount):
            walk(self.workspace_tree.topLevelItem(index))
        self._apply_workspace_filters()


    def _status_for_path(self, path: str, kind: str) -> str:
        flags: list[str] = []
        if self.asset_registry.get_by_path(path) is not None:
            flags.append("Loaded")
        if path in self.recent_outputs:
            flags.append("Output")
        if not flags and kind == "folder":
            return "—"
        return " · ".join(flags) if flags else "Available"

    def _find_workspace_item_by_path(self, target_path: str):
        def walk(item):
            if str(item.data(0, qt.Qt.UserRole) or "") == target_path:
                return item
            for index in range(item.childCount()):
                match = walk(item.child(index))
                if match is not None:
                    return match
            return None

        for index in range(self.workspace_tree.topLevelItemCount):
            match = walk(self.workspace_tree.topLevelItem(index))
            if match is not None:
                return match
        return None

    def _select_workspace_path(self, target_path: str) -> bool:
        item = self._find_workspace_item_by_path(target_path)
        if item is None:
            return False
        parent = item.parent()
        while parent is not None:
            parent.setExpanded(True)
            parent = parent.parent()
        self.workspace_tree.setCurrentItem(item)
        self.workspace_tree.scrollToItem(item)
        return True

    def _toggle_approval_details(self, expanded: bool):
        self.approval_details_edit.setVisible(expanded)
        self.approval_details_button.text = "Hide technical details" if expanded else "View technical details"
        self._update_approval_layout()

    def _update_approval_layout(self):
        # Slicer's module scroll area can retain the collapsed minimum height.
        # Propagate the expanded card's real minimum before the next paint.
        self.approval_card.setMinimumHeight(0)
        self.approval_card.layout().invalidate()
        self.approval_card.layout().activate()
        hint = self.approval_card.minimumSizeHint
        hint = hint() if callable(hint) else hint
        height = hint.height
        height = height() if callable(height) else height
        self.approval_card.setMinimumHeight(max(0, int(height)))
        self.approval_card.updateGeometry()
        self._apply_chat_height()

    def _set_approval_ui(self, label: str, enabled: bool, summary: str = "", details: str = ""):
        self.approval_label.text = label
        self.approval_summary_label.setHtml(review_html(summary))
        self.approval_summary_label.setVisible(enabled and bool(summary))
        self.approval_details_edit.setPlainText(details)
        self.approval_details_button.setChecked(False)
        self.approval_details_button.text = "View technical details"
        self.approval_details_button.setVisible(enabled and bool(details))
        self.approval_details_edit.setVisible(False)
        self.approve_button.setEnabled(enabled)
        self.reject_button.setEnabled(enabled)
        self.approval_card.setVisible(enabled)
        self._update_approval_layout()


class DMRIAgentSlicerLogic(ServiceSlicerLogic, ScriptedLoadableModuleLogic):
    pass


class DMRIAgentSlicerWidget(ServiceWidgetMixin, BaseAgentPanelWidget):
    pass
