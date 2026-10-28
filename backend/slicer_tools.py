from __future__ import annotations

from backend.slicer_transport import (
    SLICER_WEB_SERVER_URL as SLICER_WEB_SERVER_URL,
    MCP_SLICER_TOOL_TIMEOUT_SEC as MCP_SLICER_TOOL_TIMEOUT_SEC,
    get_proxy_config as get_proxy_config,
    _scene_auth_token as _scene_auth_token,
    _scene_request_url as _scene_request_url,
    _public_scene_url as _public_scene_url,
    _redact_scene_secret as _redact_scene_secret,
    _sanitize_scene_value as _sanitize_scene_value,
    _classify_transport_error as _classify_transport_error,
    _fetch_mrml_json as _fetch_mrml_json,
    _webserver_healthcheck as _webserver_healthcheck,
    _ensure_webserver as _ensure_webserver,
    _execute_python_via_mcp as _execute_python_via_mcp,
)

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

import requests

from backend.path_mapping import map_host_to_container
from backend.truncation import TruncationPolicy, formatted_truncate_text


# Behavior adapted from zhaoyouj/mcp-slicer; see THIRD_PARTY_NOTICES.md.
# All requests use the authenticated local scene endpoint owned by the visible
# Slicer Agent session.
MCP_SLICER_EXEC_HEAVY_TIMEOUT_SEC = float(os.environ.get("MCP_SLICER_EXEC_HEAVY_TIMEOUT_SEC", "180"))
MCP_SLICER_MAX_TOKENS = int(os.environ.get("MCP_SLICER_MAX_TOKENS", "1500"))
MCP_SLICER_SCREENSHOT_DIR = os.environ.get("MCP_SLICER_SCREENSHOT_DIR", "/tmp/mcp_slicer_captures")
MCP_SLICER_STDOUT_MAX_LINES = int(os.environ.get("MCP_SLICER_STDOUT_MAX_LINES", "80"))
MCP_SLICER_STDERR_MAX_LINES = int(os.environ.get("MCP_SLICER_STDERR_MAX_LINES", "60"))
MCP_SLICER_STDIO_HEAD_LINES = int(os.environ.get("MCP_SLICER_STDIO_HEAD_LINES", "24"))
MCP_SLICER_STDIO_TAIL_LINES = int(os.environ.get("MCP_SLICER_STDIO_TAIL_LINES", "24"))
MCP_SLICER_STDIO_DEDUP = os.environ.get("MCP_SLICER_STDIO_DEDUP", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
MCP_SLICER_INSPECT_METHODS_RETURN_MAX = int(os.environ.get("MCP_SLICER_INSPECT_METHODS_RETURN_MAX", "24"))
MCP_SLICER_INSPECT_PARAMS_RETURN_MAX = int(os.environ.get("MCP_SLICER_INSPECT_PARAMS_RETURN_MAX", "40"))
MCP_SLICER_RUN_AVAILABLE_METHODS_RETURN_MAX = int(
    os.environ.get("MCP_SLICER_RUN_AVAILABLE_METHODS_RETURN_MAX", "24")
)
MCP_SLICER_RESULT_STRING_MAX_CHARS = int(os.environ.get("MCP_SLICER_RESULT_STRING_MAX_CHARS", "1200"))
MCP_SLICER_RESULT_LIST_MAX_ITEMS = int(os.environ.get("MCP_SLICER_RESULT_LIST_MAX_ITEMS", "40"))
MCP_SLICER_RESULT_DICT_MAX_ITEMS = int(os.environ.get("MCP_SLICER_RESULT_DICT_MAX_ITEMS", "40"))
MCP_SLICER_ALLOW_SCENE_CLEAR = os.environ.get("MCP_SLICER_ALLOW_SCENE_CLEAR", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

_TRUNCATION_POLICY = TruncationPolicy(mode="tokens", limit=MCP_SLICER_MAX_TOKENS)

# Keep scene listing focused on data-bearing MRML nodes by default.
# This avoids flooding context with default UI/runtime nodes (color tables, slice views, etc.).
DATA_NODE_CLASS_ALLOWLIST = [
    "vtkMRMLScalarVolumeNode",
    "vtkMRMLVectorVolumeNode",
    "vtkMRMLDiffusionWeightedVolumeNode",
    "vtkMRMLDiffusionTensorVolumeNode",
    "vtkMRMLLabelMapVolumeNode",
    "vtkMRMLSegmentationNode",
    "vtkMRMLModelNode",
    "vtkMRMLFiberBundleNode",
    "vtkMRMLMarkupsFiducialNode",
    "vtkMRMLMarkupsCurveNode",
    "vtkMRMLMarkupsLineNode",
    "vtkMRMLMarkupsROINode",
    "vtkMRMLLinearTransformNode",
    "vtkMRMLBSplineTransformNode",
    "vtkMRMLGridTransformNode",
]

SYSTEM_NAME_EXACT_BLOCKLIST = {
    "Red Volume Slice",
    "Green Volume Slice",
    "Yellow Volume Slice",
    "Red Transform",
    "Green Transform",
    "Yellow Transform",
}

_DANGEROUS_SCENE_PATTERNS = (
    "slicer.mrmlscene.clear(",
    "mrmlscene.clear(",
    "slicer.util.loadscene(",
)

_MANUAL_DWI_CONSTRUCTION_PATTERNS = (
    "vtkmrmldiffusionweightedvolumenode",
    "adddiffusiongradient(",
    ".setbvalue(",
    "np.loadtxt(",
    "vtkniftiimagereader(",
    "timeasvectoron(",
)

_UNVERIFIED_DICOM_DB_METHOD_PATTERNS = (
    ".studies(",
    ".modalityforseries(",
    ".seriesnumberofseries(",
)

_UNVERIFIED_DICOM_PROBING_PATTERNS = (
    "slicer.modules.dicom.widgetrepresentation(",
    "modulemanager.modules(",
    "slicer.modulenames",
    ".category",
)

# Canonical module-name aliases to reduce user/prompt spelling drift.
# Key is normalized by keeping alnum only and lower-casing.
SLICER_MODULE_NAME_ALIASES: dict[str, str] = {
    "diffusiontensorestimation": "DWIToDTIEstimation",
    "dwitodtiestimation": "DWIToDTIEstimation",
    "diffusionweightedscalarmaps": "DiffusionTensorScalarMeasurements",
    "diffusiontensorscalarmeasurements": "DiffusionTensorScalarMeasurements",
    "dwiconvert": "DWIConvert",
    "diffusionweightedvolumeconversion": "DWIConvert",
    "ukftractography": "UKFTractography",
    "fibertractmeasurements": "FiberTractMeasurements",
}

_LONG_RUNNING_EXEC_HINTS = (
    "slicer.util.savenode(",
    "open(",
    "for ",
    "while ",
    "slicer.cli.runsync(",
    "slicer.cli.run(",
)


def _json_error(message: str, **extra: Any) -> str:
    payload: dict[str, Any] = {"ok": False, "error": message}
    payload.update(extra)
    return json.dumps(_sanitize_scene_value(payload), ensure_ascii=True)


def _truncate_json_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(_sanitize_scene_value(payload), ensure_ascii=True)
    return formatted_truncate_text(raw, _TRUNCATION_POLICY)


def _coerce_line_limit(value: int, *, default: int, ceiling: int = 2000) -> int:
    try:
        parsed = int(value)
    except Exception:  # noqa: BLE001
        parsed = default
    return max(1, min(parsed, ceiling))


def _ellipsis_line(omitted: int) -> str:
    # Preserve the beginning and end of output and mark omitted content.
    return f"... (+{max(0, int(omitted))} lines omitted) ..."


def _dedupe_lines(lines: list[str]) -> tuple[list[str], int]:
    seen: set[str] = set()
    out: list[str] = []
    removed = 0
    for line in lines:
        if line in seen:
            removed += 1
            continue
        seen.add(line)
        out.append(line)
    return out, removed


def _truncate_lines_middle(lines: list[str], *, max_lines: int, head_lines: int, tail_lines: int) -> tuple[list[str], int]:
    total = len(lines)
    if total <= max_lines:
        return lines, 0
    if max_lines <= 1:
        return [_ellipsis_line(total)], total

    head = max(0, min(head_lines, max_lines - 1))
    tail = max(0, min(tail_lines, max_lines - head - 1))
    if head + tail + 1 > max_lines:
        tail = max_lines - head - 1
    if head == 0 and tail == 0:
        head = (max_lines - 1) // 2
        tail = max_lines - head - 1

    omitted = max(0, total - head - tail)
    kept: list[str] = []
    if head > 0:
        kept.extend(lines[:head])
    kept.append(_ellipsis_line(omitted))
    if tail > 0:
        kept.extend(lines[-tail:])
    return kept, omitted


def _compact_result_payload(result: Any) -> Any:
    # Avoid duplicating stdout/stderr in both top-level payload and nested result.
    if isinstance(result, dict):
        compacted = dict(result)
        for noisy_key in ("stdout", "stderr"):
            if noisy_key in compacted and isinstance(compacted[noisy_key], str):
                compacted.pop(noisy_key, None)
        list_cap = max(5, min(int(MCP_SLICER_RESULT_LIST_MAX_ITEMS), 200))
        dict_cap = max(5, min(int(MCP_SLICER_RESULT_DICT_MAX_ITEMS), 200))
        for key, value in list(compacted.items()):
            if isinstance(value, list) and len(value) > list_cap:
                compacted[key] = value[:list_cap]
                compacted[f"{key}Omitted"] = len(value) - list_cap
            elif isinstance(value, dict) and len(value) > dict_cap:
                items = list(value.items())
                compacted[key] = dict(items[:dict_cap])
                compacted[f"{key}Omitted"] = len(items) - dict_cap
        return compacted
    if isinstance(result, str):
        # Collapse long free-text result strings to a preview.
        max_chars = max(200, min(int(MCP_SLICER_RESULT_STRING_MAX_CHARS), 20000))
        if len(result) > max_chars:
            return result[:max_chars] + f"\n... ({len(result) - max_chars} chars omitted)"
    return result


def _truncate_text_chars(text: str, max_chars: int) -> str:
    max_chars = max(64, min(int(max_chars), 50000))
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... ({len(text) - max_chars} chars omitted)"


def _contains_dangerous_scene_operation(code: str) -> bool:
    lowered = str(code or "").lower()
    return any(pattern in lowered for pattern in _DANGEROUS_SCENE_PATTERNS)


def _contains_manual_dwi_construction(code: str) -> bool:
    lowered = str(code or "").lower()
    # Require both diffusion-node intent and low-level reconstruction signals.
    has_node = "vtkmrmldiffusionweightedvolumenode" in lowered
    low_level_hits = sum(
        1
        for pattern in _MANUAL_DWI_CONSTRUCTION_PATTERNS
        if pattern in lowered
    )
    return has_node and low_level_hits >= 2


def _contains_unverified_dicom_db_api(code: str) -> bool:
    lowered = str(code or "").lower()
    if "ctkdicomdatabase" not in lowered and "dicomdatabase" not in lowered:
        return False
    return any(pattern in lowered for pattern in _UNVERIFIED_DICOM_DB_METHOD_PATTERNS)


def _contains_unverified_dicom_probing(code: str) -> bool:
    lowered = str(code or "").lower()
    if "dicom" not in lowered:
        return False
    return any(pattern in lowered for pattern in _UNVERIFIED_DICOM_PROBING_PATTERNS)


def _estimate_exec_timeout_seconds(code: str) -> float:
    """Choose a safer timeout for Python execution inside Slicer.

    Keep short snippets responsive, but avoid false timeouts for expectedly heavier
    operations (saving large nodes, file IO, CLI calls, loops).
    """
    base = max(3.0, float(MCP_SLICER_TOOL_TIMEOUT_SEC))
    text = str(code or "").lower()
    if any(hint in text for hint in _LONG_RUNNING_EXEC_HINTS):
        return max(base, float(MCP_SLICER_EXEC_HEAVY_TIMEOUT_SEC))
    return base


def _compact_cli_parameters(cli_parameters: Any, cap: int) -> tuple[Any, int]:
    if not isinstance(cli_parameters, list):
        return cli_parameters, 0
    trimmed: list[dict[str, Any]] = []
    omitted = max(0, len(cli_parameters) - cap)
    for item in cli_parameters[:cap]:
        if not isinstance(item, dict):
            continue
        compact_item = {
            "group": item.get("group"),
            "name": item.get("name"),
            "label": item.get("label"),
            "type": item.get("type"),
            "channel": item.get("channel"),
            "default": item.get("default"),
        }
        # Drop null/empty values to reduce payload size.
        compact_item = {k: v for k, v in compact_item.items() if v not in (None, "")}
        trimmed.append(compact_item)
    return trimmed, omitted


def _compact_inspect_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    method_cap = _coerce_line_limit(
        MCP_SLICER_INSPECT_METHODS_RETURN_MAX,
        default=24,
        ceiling=500,
    )
    param_cap = _coerce_line_limit(
        MCP_SLICER_INSPECT_PARAMS_RETURN_MAX,
        default=40,
        ceiling=1000,
    )

    methods = out.get("logicMethods")
    if isinstance(methods, list):
        deduped_methods = _dedupe_sequence(methods)
        out["logicMethodCount"] = len(deduped_methods)
        if len(deduped_methods) > method_cap:
            out["logicMethods"] = deduped_methods[:method_cap]
            out["logicMethodsOmitted"] = len(deduped_methods) - method_cap
        else:
            out["logicMethods"] = deduped_methods

    cli_params = out.get("cliParameters")
    compact_params, omitted = _compact_cli_parameters(cli_params, param_cap)
    if isinstance(compact_params, list):
        out["cliParameterCount"] = len(cli_params) if isinstance(cli_params, list) else len(compact_params)
        out["cliParameters"] = compact_params
        if omitted > 0:
            out["cliParametersOmitted"] = omitted
    return out


def _compact_run_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    method_cap = _coerce_line_limit(
        MCP_SLICER_RUN_AVAILABLE_METHODS_RETURN_MAX,
        default=24,
        ceiling=500,
    )
    available = out.get("availableMethods")
    if isinstance(available, list):
        deduped = _dedupe_sequence(available)
        out["availableMethodCount"] = len(deduped)
        if len(deduped) > method_cap:
            out["availableMethods"] = deduped[:method_cap]
            out["availableMethodsOmitted"] = len(deduped) - method_cap
        else:
            out["availableMethods"] = deduped

    if isinstance(out.get("error"), str):
        out["error"] = _truncate_text_chars(out["error"], 3000)
    if isinstance(out.get("hint"), str):
        out["hint"] = _truncate_text_chars(out["hint"], 1000)
    return out


def _summarize_stdio(
    text: str,
    *,
    max_lines: int,
    dedupe: bool,
    head_lines: int,
    tail_lines: int,
) -> tuple[str, dict[str, Any]]:
    if not text:
        return "", {
            "total_lines": 0,
            "unique_lines": 0,
            "returned_lines": 0,
            "dedup_removed_lines": 0,
            "omitted_lines": 0,
            "truncated": False,
        }

    raw_lines = text.splitlines()
    dedup_removed = 0
    lines = raw_lines
    structured_json = False
    if text.lstrip().startswith(("{", "[")):
        try:
            structured_json = isinstance(json.loads(text), (dict, list))
        except (ValueError, TypeError):
            pass
    if dedupe and not structured_json:
        lines, dedup_removed = _dedupe_lines(raw_lines)

    truncated_lines, omitted = _truncate_lines_middle(
        lines,
        max_lines=max_lines,
        head_lines=head_lines,
        tail_lines=tail_lines,
    )

    summary = {
        "total_lines": len(raw_lines),
        "unique_lines": len(lines),
        "returned_lines": len(truncated_lines),
        "dedup_removed_lines": dedup_removed,
        "omitted_lines": omitted,
        "truncated": omitted > 0,
    }
    return "\n".join(truncated_lines), summary


def _wrap_python_code_with_capture(user_code: str) -> str:
    # Wrap user code to:
    # 1) capture stdout/stderr
    # 2) preserve explicit __execResult if user sets it
    # 3) auto-inject a structured result when __execResult is missing
    user_code_json = json.dumps(user_code, ensure_ascii=True)
    return f"""
import contextlib
import io
import traceback

_dmri_user_code = {user_code_json}
_dmri_stdout = io.StringIO()
_dmri_stderr = io.StringIO()
_dmri_locals = {{}}
_dmri_error = None
_dmri_global_had_exec_result = "__execResult" in globals()
_dmri_prev_exec_result = globals().get("__execResult")

with contextlib.redirect_stdout(_dmri_stdout), contextlib.redirect_stderr(_dmri_stderr):
    try:
        exec(_dmri_user_code, globals(), _dmri_locals)
    except Exception:
        _dmri_error = traceback.format_exc()

_dmri_stdout_value = _dmri_stdout.getvalue()
_dmri_stderr_value = _dmri_stderr.getvalue()
_dmri_has_explicit_local = "__execResult" in _dmri_locals
_dmri_has_explicit_global = "__execResult" in globals() and globals().get("__execResult") is not _dmri_prev_exec_result
if _dmri_has_explicit_local:
    _dmri_explicit = _dmri_locals.get("__execResult")
elif _dmri_has_explicit_global:
    _dmri_explicit = globals().get("__execResult")
else:
    _dmri_explicit = None
_dmri_has_explicit = bool(_dmri_has_explicit_local or _dmri_has_explicit_global)

if _dmri_error is not None:
    _dmri_final_exec_result = {{
        "ok": False,
        "error": _dmri_error,
        "stdout": _dmri_stdout_value,
        "stderr": _dmri_stderr_value,
        "has_explicit_result": _dmri_has_explicit,
        "result": _dmri_explicit,
        "auto_injected_result": not _dmri_has_explicit,
    }}
elif _dmri_has_explicit:
    _dmri_final_exec_result = {{
        "ok": True,
        "has_explicit_result": True,
        "result": _dmri_explicit,
        "stdout": _dmri_stdout_value,
        "stderr": _dmri_stderr_value,
        "auto_injected_result": False,
    }}
else:
    _dmri_auto = {{}}
    if _dmri_stdout_value:
        _dmri_auto["stdout"] = _dmri_stdout_value
    if _dmri_stderr_value:
        _dmri_auto["stderr"] = _dmri_stderr_value
    if not _dmri_auto:
        _dmri_auto["message"] = "Code executed successfully (no explicit __execResult and no stdout/stderr)."
    _dmri_final_exec_result = {{
        "ok": True,
        "has_explicit_result": False,
        "result": _dmri_auto,
        "stdout": _dmri_stdout_value,
        "stderr": _dmri_stderr_value,
        "auto_injected_result": True,
    }}

if _dmri_global_had_exec_result:
    globals()["__execResult"] = _dmri_prev_exec_result
elif "__execResult" in globals():
    del globals()["__execResult"]
__execResult = _dmri_final_exec_result
"""


def _dedupe_sequence(values: list[Any]) -> list[Any]:
    seen: set[Any] = set()
    out: list[Any] = []
    for value in values:
        if isinstance(value, (str, int, float, bool, type(None))):
            key: Any = value
        else:
            key = json.dumps(value, sort_keys=True, ensure_ascii=True)
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _is_noise_by_name(node_name: str) -> bool:
    return node_name.strip() in SYSTEM_NAME_EXACT_BLOCKLIST


def mcp_slicer_list_nodes(
    filter_type: str = "names",
    class_name: Optional[str] = None,
    name: Optional[str] = None,
    id: Optional[str] = None,
    limit: int = 80,
    include_system_nodes: bool = False,
) -> str:
    ok, msg = _ensure_webserver(require_exec=False)
    if not ok:
        return _json_error("Slicer Web Server unavailable", detail=msg, web_server_url=SLICER_WEB_SERVER_URL)

    endpoint_map = {"names": "/mrml/names", "ids": "/mrml/ids", "properties": "/mrml/properties"}
    if filter_type not in endpoint_map:
        return _json_error("Invalid filter_type", allowed=list(endpoint_map.keys()))
    if filter_type == "properties" and not any([class_name, name, id]):
        return _json_error(
            "properties listing requires a targeted filter",
            detail=(
                "To avoid unstable large `/mrml/properties` enumeration, `filter_type=properties` "
                "must include at least one of: `class_name`, `name`, or `id`. "
                "Use `filter_type=names` or `filter_type=ids` for broad scene introspection."
            ),
        )

    params: dict[str, str] = {}
    if class_name:
        params["class"] = class_name
    if name:
        params["name"] = name
    if id:
        params["id"] = id

    filter_scope = "all_nodes"
    class_scope: list[str] = []
    class_warnings: list[str] = []
    endpoint = endpoint_map[filter_type]

    try:
        # Default behavior: suppress scene noise by only listing data-bearing classes.
        # If user explicitly asks for class/id or include_system_nodes=True, keep full behavior.
        use_data_only = not include_system_nodes and not class_name and not id
        if use_data_only:
            filter_scope = "data_nodes_only"
            class_scope = list(DATA_NODE_CLASS_ALLOWLIST)
            merged_names: list[str] = []
            merged_ids: list[str] = []
            merged_props: dict[str, Any] = {}
            fetch_endpoint = "/mrml/properties" if filter_type == "properties" else endpoint

            for cls in class_scope:
                cls_params = dict(params)
                cls_params["class"] = cls
                try:
                    _, class_payload = _fetch_mrml_json(fetch_endpoint, cls_params)
                    if filter_type == "properties":
                        if isinstance(class_payload, dict):
                            merged_props.update(class_payload)
                    elif filter_type == "ids":
                        if isinstance(class_payload, list):
                            merged_ids.extend(str(item) for item in class_payload if item)
                    else:
                        if isinstance(class_payload, list):
                            merged_names.extend(str(item) for item in class_payload if item)
                except Exception as cls_exc:  # noqa: BLE001
                    class_warnings.append(f"{cls}: {_redact_scene_secret(cls_exc)}")

            if filter_type == "properties":
                filtered_props: dict[str, Any] = {}
                for node_id, props in merged_props.items():
                    if not isinstance(props, dict):
                        continue
                    node_name = str(props.get("Name", "") or "")
                    if _is_noise_by_name(node_name):
                        continue
                    filtered_props[node_id] = props
                data = filtered_props
            elif filter_type == "ids":
                data = _dedupe_sequence([node_id for node_id in merged_ids if not _is_noise_by_name(str(node_id))])
            else:
                data = _dedupe_sequence([node_name for node_name in merged_names if not _is_noise_by_name(node_name)])
            request_url = _public_scene_url(endpoint)
        else:
            request_url, data = _fetch_mrml_json(endpoint, params)
            if class_name:
                class_scope = [class_name]
    except Exception as exc:  # noqa: BLE001
        return _json_error(
            "Failed to list Slicer nodes",
            detail=_redact_scene_secret(exc),
            request_url=_public_scene_url(endpoint),
        )

    payload: dict[str, Any] = {
        "ok": True,
        "tool": "mcp_slicer_list_nodes",
        "filter_type": filter_type,
        "request_url": request_url,
        "scope": filter_scope,
        "include_system_nodes": bool(include_system_nodes),
    }
    if class_scope:
        if class_name:
            payload["class_scope"] = class_scope
        elif filter_scope == "data_nodes_only":
            payload["data_class_count"] = len(class_scope)
    if class_warnings:
        payload["skipped_class_count"] = len(class_warnings)
    if isinstance(data, list):
        capped = max(1, min(int(limit), 2000))
        payload["total"] = len(data)
        payload["nodes"] = data[:capped]
        if len(data) > capped:
            payload["truncated"] = len(data) - capped
    elif isinstance(data, dict):
        items = list(data.items())
        capped = max(1, min(int(limit), 2000))
        payload["total"] = len(items)
        payload["nodes"] = dict(items[:capped])
        if len(items) > capped:
            payload["truncated"] = len(items) - capped
    else:
        payload["nodes"] = data

    return _truncate_json_payload(payload)


def mcp_slicer_execute_python_code(code: str) -> str:
    if not code or not code.strip():
        return _json_error("code is required")
    if _contains_unverified_dicom_probing(code):
        return _json_error(
            "Unverified DICOM module probing rejected",
            detail=(
                "Do not inspect DICOM via widgetRepresentation/moduleManager/category probing. "
                "Use `run_slicer_module(action=\"inspect\", module=\"DICOM\")` and "
                "`run_slicer_module(action=\"run\", module=\"DICOM\", inputs={\"dicomDirectory\": ...})`."
            ),
            blocked_patterns=list(_UNVERIFIED_DICOM_PROBING_PATTERNS),
        )
    if _contains_unverified_dicom_db_api(code):
        return _json_error(
            "Unverified DICOM database API usage rejected",
            detail=(
                "Use verified ctkDICOMDatabase traversal methods only: "
                "patients() -> studiesForPatient(patient) -> seriesForStudy(study) -> "
                "filesForSeries(series). For metadata, use fileValue(file, tag) or "
                "seriesDescription(series)."
            ),
            verified_snippet=(
                "db = slicer.dicomDatabase\n"
                "patients = db.patients()\n"
                "for patient in patients:\n"
                "    for study in db.studiesForPatient(patient):\n"
                "        for series in db.seriesForStudy(study):\n"
                "            files = db.filesForSeries(series)\n"
                "            if files:\n"
                "                print(db.seriesDescription(series), len(files))"
            ),
            blocked_patterns=list(_UNVERIFIED_DICOM_DB_METHOD_PATTERNS),
        )
    if not MCP_SLICER_ALLOW_SCENE_CLEAR and _contains_dangerous_scene_operation(code):
        return _json_error(
            "Dangerous Slicer scene operation rejected",
            detail=(
                "Global scene clearing/reloading is disabled for safety. "
                "Reuse the current scene, operate on specific nodes, or use dedicated helpers "
                "such as tractography loading/display instead."
            ),
        )
    if _contains_manual_dwi_construction(code):
        return _json_error(
            "Manual diffusion-volume construction rejected",
            detail=(
                "Do not hand-build `vtkMRMLDiffusionWeightedVolumeNode` objects with "
                "low-level NIfTI/bval/bvec parsing inside `mcp_slicer_execute_python_code`. "
                "Convert `NIfTI + bval + bvec` to a diffusion-aware `NHDR/NRRD` representation "
                "first, then load that converted volume into Slicer before using SlicerDMRI modules."
            ),
        )

    wrapped_code = _wrap_python_code_with_capture(code)
    exec_timeout_sec = _estimate_exec_timeout_seconds(code)
    ok, result, error_text, meta = _execute_python_via_mcp(
        wrapped_code,
        timeout_sec=exec_timeout_sec,
    )
    if not ok:
        return _json_error("Failed to execute Python code in Slicer", detail=error_text, **meta)
    execution_ok = True
    has_explicit_result = None
    auto_injected_result = None
    stdout_text = ""
    stderr_text = ""
    normalized_result = result
    if isinstance(result, dict):
        execution_ok = bool(result.get("ok", True))
        has_explicit_result = result.get("has_explicit_result")
        auto_injected_result = result.get("auto_injected_result")
        stdout_text = str(result.get("stdout") or "")
        stderr_text = str(result.get("stderr") or "")
        normalized_result = result.get("result")

    stdout_limit = _coerce_line_limit(MCP_SLICER_STDOUT_MAX_LINES, default=80, ceiling=4000)
    stderr_limit = _coerce_line_limit(MCP_SLICER_STDERR_MAX_LINES, default=60, ceiling=4000)
    head_limit = _coerce_line_limit(MCP_SLICER_STDIO_HEAD_LINES, default=24, ceiling=2000)
    tail_limit = _coerce_line_limit(MCP_SLICER_STDIO_TAIL_LINES, default=24, ceiling=2000)

    stdout_preview, stdout_summary = _summarize_stdio(
        stdout_text,
        max_lines=stdout_limit,
        dedupe=MCP_SLICER_STDIO_DEDUP,
        head_lines=head_limit,
        tail_lines=tail_limit,
    )
    stderr_preview, stderr_summary = _summarize_stdio(
        stderr_text,
        max_lines=stderr_limit,
        dedupe=MCP_SLICER_STDIO_DEDUP,
        head_lines=head_limit,
        tail_lines=tail_limit,
    )
    compact_result = _compact_result_payload(normalized_result)

    if not execution_ok:
        error_payload: dict[str, Any] = {
            "ok": False,
            "error": "Python code execution failed in Slicer",
            "detail": _truncate_text_chars(str(result.get("error") if isinstance(result, dict) else result), 3000),
            "request_url": meta.get("request_url"),
            "status_code": meta.get("status_code"),
            "has_explicit_result": has_explicit_result,
            "auto_injected_result": auto_injected_result,
            "stdout_summary": stdout_summary,
            "stderr_summary": stderr_summary,
        }
        if stdout_preview:
            error_payload["stdout_preview"] = stdout_preview
        if stderr_preview:
            error_payload["stderr_preview"] = stderr_preview
        return _truncate_json_payload(error_payload)

    payload: dict[str, Any] = {
        "ok": True,
        "status_code": meta.get("status_code"),
        "result": compact_result,
        "stdout_summary": stdout_summary,
        "stderr_summary": stderr_summary,
        "has_explicit_result": has_explicit_result,
        "auto_injected_result": auto_injected_result,
        "request_url": meta.get("request_url"),
    }
    if stdout_preview:
        payload["stdout_preview"] = stdout_preview
    if stderr_preview:
        payload["stderr_preview"] = stderr_preview
    return _truncate_json_payload(payload)


def mcp_slicer_load_tractography(
    path: str,
    node_name: Optional[str] = None,
    visible: bool = True,
    color: Optional[str] = None,
    line_width: float = 1.0,
) -> str:
    if not path or not str(path).strip():
        return _json_error("path is required")

    if os.path.isabs(path):
        host_path = Path(path).expanduser()
        if not host_path.exists():
            host_path = (Path.cwd() / path).resolve()
        container_path = str(map_host_to_container(host_path.resolve()))
    else:
        container_path = str(path)
    requested_name = str(node_name or "").strip()
    wrapped_code = f"""
import os
import slicer
import vtk

tract_path = {json.dumps(container_path, ensure_ascii=True)}
requested_name = {json.dumps(requested_name, ensure_ascii=True)}
visible = {repr(bool(visible))}
line_width = {float(line_width)}
color_spec = {json.dumps(color, ensure_ascii=True)}

if not os.path.exists(tract_path):
    __execResult = {{
        "ok": False,
        "error": f"tractography file not found: {{tract_path}}",
    }}
else:
    ext = os.path.splitext(tract_path)[1].lower()
    if tract_path.lower().endswith(".nii.gz"):
        ext = ".nii.gz"
    if ext == ".vtp":
        reader = vtk.vtkXMLPolyDataReader()
    elif ext == ".vtk":
        reader = vtk.vtkPolyDataReader()
    else:
        reader = None

    if reader is None:
        __execResult = {{
            "ok": False,
            "error": f"unsupported tractography extension: {{ext}}",
            "supported": [".vtp", ".vtk"],
        }}
    else:
        reader.SetFileName(tract_path)
        reader.Update()
        poly = reader.GetOutput()
        if poly is None or poly.GetNumberOfLines() == 0:
            __execResult = {{
                "ok": False,
                "error": "tractography file loaded but contains no fiber lines",
                "path": tract_path,
                "points": int(poly.GetNumberOfPoints()) if poly is not None else 0,
                "lines": int(poly.GetNumberOfLines()) if poly is not None else 0,
            }}
        else:
            target_name = requested_name or os.path.splitext(os.path.basename(tract_path))[0]
            existing = None
            for node in slicer.util.getNodesByClass("vtkMRMLFiberBundleNode"):
                if node.GetAttribute("DMRIAgent.SourcePath") == tract_path or node.GetName() == target_name:
                    existing = node
                    break
            fb_node = existing
            if fb_node is None:
                fb_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLFiberBundleNode", target_name)
            fb_node.SetAndObservePolyData(poly)
            fb_node.SetAttribute("DMRIAgent.SourcePath", tract_path)
            if fb_node.GetDisplayNode() is None:
                fb_node.CreateDefaultDisplayNodes()
            display = fb_node.GetDisplayNode()
            if display is not None:
                display.SetVisibility(1 if visible else 0)
                try:
                    display.SetLineWidth(float(line_width))
                except Exception:
                    pass
                if color_spec:
                    named = {{
                        "orange": (0.95, 0.55, 0.20),
                        "green": (0.25, 0.72, 0.42),
                        "blue": (0.25, 0.55, 0.95),
                        "white": (0.95, 0.95, 0.95),
                    }}
                    rgb = named.get(str(color_spec).lower())
                    if rgb is not None:
                        display.SetColor(*rgb)
            __execResult = {{
                "ok": True,
                "path": tract_path,
                "node": {{
                    "id": fb_node.GetID(),
                    "name": fb_node.GetName(),
                    "class": fb_node.GetClassName(),
                }},
                "points": int(poly.GetNumberOfPoints()),
                "lines": int(poly.GetNumberOfLines()),
                "reusedExistingNode": bool(existing is not None),
            }}
"""
    ok, result, error_text, meta = _execute_python_via_mcp(
        _wrap_python_code_with_capture(wrapped_code),
        timeout_sec=max(MCP_SLICER_TOOL_TIMEOUT_SEC, 60.0),
    )
    if not ok:
        return _json_error("Failed to load tractography in Slicer", detail=error_text, **meta)
    payload = result if isinstance(result, dict) else {"ok": False, "error": "Unexpected tractography response"}
    payload["tool"] = "mcp_slicer_load_tractography"
    payload["backend_route"] = "mcp_exec"
    payload["request_url"] = meta.get("request_url")
    return _truncate_json_payload(payload)


def mcp_slicer_capture_screenshot(
    view_type: str = "application",
    view_name: Optional[str] = None,
    slice_offset: Optional[float] = None,
    slice_orientation: Optional[str] = None,
    camera_axis: Optional[str] = None,
    image_size: Optional[int] = None,
) -> str:
    ok, msg = _ensure_webserver(require_exec=False)
    if not ok:
        return _json_error("Slicer Web Server unavailable", detail=msg, web_server_url=SLICER_WEB_SERVER_URL)

    params: dict[str, Any] = {}
    description = ""
    if view_type == "application":
        endpoint = "/screenshot"
        description = "full application window"
    elif view_type == "slice":
        if not view_name:
            return _json_error("view_name is required for slice screenshots")
        endpoint = "/slice"
        params["view"] = str(view_name).lower()
        description = f"{view_name} slice view"
        if slice_offset is not None:
            params["offset"] = slice_offset
        if slice_orientation:
            params["orientation"] = str(slice_orientation).lower()
        if image_size:
            params["size"] = int(image_size)
    elif view_type == "3d":
        endpoint = "/threeD"
        description = "3D view"
        if camera_axis:
            params["lookFromAxis"] = str(camera_axis).upper()
        if image_size:
            params["size"] = int(image_size)
    else:
        return _json_error("Invalid view_type", allowed=["application", "slice", "3d"])

    url = _scene_request_url(endpoint)
    public_url = _public_scene_url(endpoint)
    try:
        resp = requests.get(
            url,
            params=params,
            timeout=MCP_SLICER_TOOL_TIMEOUT_SEC,
            proxies=get_proxy_config(url),
        )
        resp.raise_for_status()
        if not resp.headers.get("Content-Type", "").startswith("image/"):
            return _json_error(
                "Unexpected response content type",
                content_type=resp.headers.get("Content-Type", ""),
                status_code=resp.status_code,
            )
    except Exception as exc:  # noqa: BLE001
        return _json_error(
            "Failed to capture screenshot",
            detail=_redact_scene_secret(exc),
            request_url=public_url,
        )

    output_dir = Path(MCP_SLICER_SCREENSHOT_DIR).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    suffix = "png"
    filename = f"slicer_{view_type}_{timestamp}_{int(time.time() * 1000) % 1000:03d}.{suffix}"
    out_path = output_dir / filename
    out_path.write_bytes(resp.content)

    payload = {
        "ok": True,
        "tool": "mcp_slicer_capture_screenshot",
        "description": description,
        "view_type": view_type,
        "request_url": public_url,
        "bytes": len(resp.content),
        "content_type": resp.headers.get("Content-Type", ""),
        "saved_path": str(map_host_to_container(out_path)),
    }
    return _truncate_json_payload(payload)


def inspect_slicer_module(module: str, include_methods: bool = False, method_limit: int = 60) -> str:
    module_name = str(module or "").strip()
    if not module_name:
        return _json_error("module is required")
    capped = max(1, min(int(method_limit), 500))
    request_payload = {
        "module": module_name,
        "include_methods": bool(include_methods),
        "method_limit": capped,
        "module_aliases": SLICER_MODULE_NAME_ALIASES,
    }
    request_blob = json.dumps(request_payload, ensure_ascii=True)
    py_code = f"""
import json
import slicer
import vtk

req = json.loads({json.dumps(request_blob, ensure_ascii=True)})
module_name = str(req.get("module", "")).strip()
include_methods = bool(req.get("include_methods", True))
method_limit = int(req.get("method_limit", 120))
method_limit = max(1, min(method_limit, 500))
module_aliases = req.get("module_aliases") if isinstance(req.get("module_aliases"), dict) else {{}}

def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {{str(k): _json_safe(v) for k, v in value.items()}}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    try:
        json.dumps(value)
        return value
    except Exception:
        return str(value)

def _normalize_module_key(text):
    return "".join(ch for ch in str(text or "").lower() if ch.isalnum())

def _resolve_module_name(raw_name):
    requested = str(raw_name or "").strip()
    if not requested:
        return {{"requested": requested, "resolved": requested, "strategy": "empty"}}
    try:
        slicer.util.getModule(requested)
        return {{"requested": requested, "resolved": requested, "strategy": "exact"}}
    except Exception:
        pass

    available = []
    try:
        available = [str(name) for name in slicer.util.moduleNames()]
    except Exception:
        available = []

    requested_lower = requested.lower()
    for name in available:
        if name.lower() == requested_lower:
            return {{
                "requested": requested,
                "resolved": name,
                "strategy": "case_insensitive",
            }}

    norm_key = _normalize_module_key(requested)
    alias_target = module_aliases.get(norm_key)
    if alias_target:
        alias_target = str(alias_target).strip()
        if alias_target:
            try:
                slicer.util.getModule(alias_target)
                return {{
                    "requested": requested,
                    "resolved": alias_target,
                    "strategy": "alias",
                    "aliasTarget": alias_target,
                }}
            except Exception:
                pass
            alias_lower = alias_target.lower()
            for name in available:
                if name.lower() == alias_lower:
                    return {{
                        "requested": requested,
                        "resolved": name,
                        "strategy": "alias",
                        "aliasTarget": alias_target,
                    }}

    return {{
        "requested": requested,
        "resolved": requested,
        "strategy": "unresolved",
    }}

if not module_name:
    __execResult = {{
        "ok": False,
        "error": "module is required",
        "error_type": "invalid_request",
        "retryable": False,
    }}
else:
    resolution = _resolve_module_name(module_name)
    requested_module = str(resolution.get("requested") or module_name)
    resolved_module = str(resolution.get("resolved") or requested_module)
    resolve_strategy = str(resolution.get("strategy") or "exact")
    try:
        module = slicer.util.getModule(resolved_module)
    except Exception:
        __execResult = {{
            "ok": False,
            "module": requested_module,
            "requested_module": requested_module,
            "resolved_module": resolved_module,
            "resolve_strategy": resolve_strategy,
            "error": f"Module not found: {{requested_module}}",
            "error_type": "module_not_found",
            "retryable": False,
        }}
    else:
        logic_obj = None
        logic_class = None
        try:
            logic_obj = slicer.util.getModuleLogic(resolved_module)
            logic_class = f"{{logic_obj.__class__.__module__}}.{{logic_obj.__class__.__name__}}"
        except Exception:
            logic_obj = None

        module_class = module.className() if hasattr(module, "className") else type(module).__name__
        is_cli = bool(logic_class and "vtkSlicerCLIModuleLogic" in logic_class)
        if is_cli:
            module_type = "cli"
        elif "Scripted" in module_class:
            module_type = "scripted"
        elif logic_obj is not None:
            module_type = "loadable"
        else:
            module_type = "unknown"

        out = {{
            "ok": True,
            "module": resolved_module,
            "requested_module": requested_module,
            "resolved_module": resolved_module,
            "resolve_strategy": resolve_strategy,
            "type": module_type,
            "moduleClass": module_class,
            "logicClass": logic_class,
        }}
        if module_type == "cli":
            cli_params = []
            cli_node = None
            try:
                logic_for_schema = module.logic() if hasattr(module, "logic") else None
                if logic_for_schema is not None and hasattr(logic_for_schema, "CreateNodeInScene"):
                    cli_node = logic_for_schema.CreateNodeInScene()
                if cli_node is not None:
                    group_count = int(cli_node.GetNumberOfParameterGroups())
                    for gi in range(group_count):
                        try:
                            group_label = str(cli_node.GetParameterGroupLabel(gi))
                        except Exception:
                            group_label = f"group_{{gi}}"
                        param_count = int(cli_node.GetNumberOfParametersInGroup(gi))
                        for pi in range(param_count):
                            item = {{
                                "group": group_label,
                            }}
                            for field, method_name in (
                                ("name", "GetParameterName"),
                                ("label", "GetParameterLabel"),
                                ("tag", "GetParameterTag"),
                                ("type", "GetParameterType"),
                                ("channel", "GetParameterChannel"),
                                ("default", "GetParameterDefault"),
                            ):
                                try:
                                    method = getattr(cli_node, method_name, None)
                                    if callable(method):
                                        item[field] = str(method(gi, pi))
                                except Exception:
                                    pass
                            cli_params.append(item)
            except Exception as exc:
                out["cliSchemaError"] = str(exc)
            finally:
                try:
                    if cli_node is not None:
                        slicer.mrmlScene.RemoveNode(cli_node)
                except Exception:
                    pass
            if cli_params:
                out["cliParameters"] = cli_params
        if include_methods and logic_obj is not None:
            methods = []
            for name in dir(logic_obj):
                if name.startswith("_"):
                    continue
                member = getattr(logic_obj, name, None)
                if callable(member):
                    methods.append(name)
            methods = sorted(set(methods))
            out["logicMethods"] = methods[:method_limit]
            if len(methods) > method_limit:
                out["logicMethodsTruncated"] = len(methods) - method_limit
        __execResult = _json_safe(out)
"""
    ok, result, error_text, meta = _execute_python_via_mcp(
        py_code,
        timeout_sec=max(10, MCP_SLICER_TOOL_TIMEOUT_SEC),
    )
    if not ok:
        retryable, error_type = _classify_transport_error(error_text)
        return _json_error(
            "Failed to inspect Slicer module",
            detail=error_text,
            module=module_name,
            error_type=error_type,
            retryable=retryable,
            **meta,
        )
    payload = result if isinstance(result, dict) else {"ok": False, "error": "Unexpected inspect response"}
    if isinstance(payload, dict):
        payload = _compact_inspect_payload(payload)
    payload["tool"] = "inspect_slicer_module"
    payload["backend_route"] = "mcp_exec"
    payload["request_url"] = meta.get("request_url")
    return _truncate_json_payload(payload)


def run_slicer_module(
    module: str,
    action: str = "run",
    mode: str = "auto",
    inputs: Optional[dict[str, Any]] = None,
    outputs: Optional[dict[str, Any]] = None,
    parameters: Optional[dict[str, Any]] = None,
    method: Optional[str] = None,
    methodArgs: Optional[Any] = None,
    timeout_sec: int = 1200,
    include_methods: bool = False,
    method_limit: int = 60,
) -> str:
    module_name = str(module or "").strip()
    if not module_name:
        return _json_error("module is required")

    normalized_action = str(action or "run").strip().lower()
    if normalized_action not in {"run", "inspect"}:
        return _json_error("invalid action", allowed=["run", "inspect"])

    if normalized_action == "inspect":
        inspect_text = inspect_slicer_module(
            module=module_name,
            include_methods=bool(include_methods),
            method_limit=int(method_limit),
        )
        try:
            parsed = json.loads(str(inspect_text or "").strip())
        except Exception:  # noqa: BLE001
            parsed = None
        if not isinstance(parsed, dict):
            return inspect_text
        parsed["tool"] = "run_slicer_module"
        parsed["action"] = "inspect"
        parsed.setdefault("backend_route", "mcp_exec")
        return _truncate_json_payload(_compact_inspect_payload(parsed))

    normalized_mode = str(mode or "auto").strip().lower()
    if normalized_mode not in {"auto", "cli", "logic"}:
        return _json_error("invalid mode", allowed=["auto", "cli", "logic"])

    timeout_sec = max(5, min(int(timeout_sec), 7200))
    request_payload: dict[str, Any] = {
        "module": module_name,
        "mode": normalized_mode,
        "inputs": inputs or {},
        "outputs": outputs or {},
        "parameters": parameters or {},
        "execution": {"timeoutSec": timeout_sec},
        "module_aliases": SLICER_MODULE_NAME_ALIASES,
    }
    if method is not None:
        request_payload["method"] = str(method)
    if methodArgs is not None:
        request_payload["methodArgs"] = methodArgs
    request_blob = json.dumps(request_payload, ensure_ascii=True)
    py_code = f"""
import json
import os
import slicer
import tempfile
import time
import traceback
import vtk

payload = json.loads({json.dumps(request_blob, ensure_ascii=True)})
module_name = str(payload.get("module", "")).strip()
mode = str(payload.get("mode", "auto")).strip().lower()
inputs = payload.get("inputs", {{}})
outputs = payload.get("outputs", {{}})
parameters = payload.get("parameters", {{}})
method_name = payload.get("method")
method_args = payload.get("methodArgs")
module_aliases = payload.get("module_aliases") if isinstance(payload.get("module_aliases"), dict) else {{}}
_dmri_t0 = time.time()

def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        out = {{}}
        for k, v in value.items():
            out[str(k)] = _json_safe(v)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    try:
        json.dumps(value)
        return value
    except Exception:
        return str(value)

def _normalize_module_key(text):
    return "".join(ch for ch in str(text or "").lower() if ch.isalnum())

def _resolve_module_name(raw_name):
    requested = str(raw_name or "").strip()
    if not requested:
        return {{"requested": requested, "resolved": requested, "strategy": "empty"}}
    try:
        slicer.util.getModule(requested)
        return {{"requested": requested, "resolved": requested, "strategy": "exact"}}
    except Exception:
        pass

    available = []
    try:
        available = [str(name) for name in slicer.util.moduleNames()]
    except Exception:
        available = []

    requested_lower = requested.lower()
    for name in available:
        if name.lower() == requested_lower:
            return {{
                "requested": requested,
                "resolved": name,
                "strategy": "case_insensitive",
            }}

    norm_key = _normalize_module_key(requested)
    alias_target = module_aliases.get(norm_key)
    if alias_target:
        alias_target = str(alias_target).strip()
        if alias_target:
            try:
                slicer.util.getModule(alias_target)
                return {{
                    "requested": requested,
                    "resolved": alias_target,
                    "strategy": "alias",
                    "aliasTarget": alias_target,
                }}
            except Exception:
                pass
            alias_lower = alias_target.lower()
            for name in available:
                if name.lower() == alias_lower:
                    return {{
                        "requested": requested,
                        "resolved": name,
                        "strategy": "alias",
                        "aliasTarget": alias_target,
                    }}

    return {{
        "requested": requested,
        "resolved": requested,
        "strategy": "unresolved",
    }}

def inspect_module(name):
    resolution = _resolve_module_name(name)
    requested_name = str(resolution.get("requested") or str(name or ""))
    resolved_name = str(resolution.get("resolved") or requested_name)
    resolve_strategy = str(resolution.get("strategy") or "exact")
    try:
        mod = slicer.util.getModule(resolved_name)
    except Exception:
        return {{
            "ok": False,
            "module": requested_name,
            "requested_module": requested_name,
            "resolved_module": resolved_name,
            "resolve_strategy": resolve_strategy,
            "error": f"Module not found: {{requested_name}}",
            "error_type": "module_not_found",
            "retryable": False,
        }}
    logic_obj = None
    logic_class = None
    try:
        logic_obj = slicer.util.getModuleLogic(resolved_name)
        logic_class = f"{{logic_obj.__class__.__module__}}.{{logic_obj.__class__.__name__}}"
    except Exception:
        logic_obj = None
    module_class = mod.className() if hasattr(mod, "className") else type(mod).__name__
    is_cli = bool(logic_class and "vtkSlicerCLIModuleLogic" in logic_class)
    if is_cli:
        module_type = "cli"
    elif "Scripted" in module_class:
        module_type = "scripted"
    elif logic_obj is not None:
        module_type = "loadable"
    else:
        module_type = "unknown"
    methods = []
    if logic_obj is not None:
        for n in dir(logic_obj):
            if n.startswith("_"):
                continue
            member = getattr(logic_obj, n, None)
            if callable(member):
                methods.append(n)

    cli_params = []
    if module_type == "cli":
        cli_node = None
        try:
            logic_for_schema = mod.logic() if hasattr(mod, "logic") else None
            if logic_for_schema is not None and hasattr(logic_for_schema, "CreateNodeInScene"):
                cli_node = logic_for_schema.CreateNodeInScene()
            if cli_node is not None:
                group_count = int(cli_node.GetNumberOfParameterGroups())
                for gi in range(group_count):
                    try:
                        group_label = str(cli_node.GetParameterGroupLabel(gi))
                    except Exception:
                        group_label = f"group_{{gi}}"
                    param_count = int(cli_node.GetNumberOfParametersInGroup(gi))
                    for pi in range(param_count):
                        item = {{"group": group_label}}
                        for field, method_name in (
                            ("name", "GetParameterName"),
                            ("label", "GetParameterLabel"),
                            ("tag", "GetParameterTag"),
                            ("type", "GetParameterType"),
                            ("channel", "GetParameterChannel"),
                            ("default", "GetParameterDefault"),
                        ):
                            try:
                                method = getattr(cli_node, method_name, None)
                                if callable(method):
                                    item[field] = str(method(gi, pi))
                            except Exception:
                                pass
                        cli_params.append(item)
        except Exception:
            pass
        finally:
            try:
                if cli_node is not None:
                    slicer.mrmlScene.RemoveNode(cli_node)
            except Exception:
                pass

    return {{
        "ok": True,
        "module": resolved_name,
        "requested_module": requested_name,
        "resolved_module": resolved_name,
        "resolve_strategy": resolve_strategy,
        "type": module_type,
        "moduleClass": module_class,
        "logicClass": logic_class,
        "logicMethods": sorted(set(methods)),
        "cliParameters": cli_params,
    }}

def _looks_like_path(text):
    if "/" in text or "\\\\" in text:
        return True
    lower = text.lower()
    for ext in (".nii", ".nii.gz", ".nrrd", ".nhdr", ".mha", ".mhd", ".vtk", ".vtp", ".stl"):
        if lower.endswith(ext):
            return True
    return False

def _normalize_workspace_path(text):
    if not isinstance(text, str):
        return text
    value = text.strip()
    if not value:
        return value
    aliases = []
    aliases_env = os.environ.get("MRI_AGENT_PATH_ALIASES", "").strip()
    if aliases_env:
        for item in aliases_env.split(","):
            token = item.strip()
            if not token or "=" not in token:
                continue
            legacy_prefix, canonical_prefix = token.split("=", 1)
            legacy_prefix = legacy_prefix.strip().rstrip("/\\\\")
            canonical_prefix = canonical_prefix.strip().rstrip("/\\\\")
            if legacy_prefix and canonical_prefix:
                aliases.append((legacy_prefix, canonical_prefix))
    for legacy_prefix, canonical_prefix in aliases:
        if value == legacy_prefix or value.startswith(legacy_prefix + "/"):
            suffix = value[len(legacy_prefix):]
            return canonical_prefix + suffix
    if "/data_processed/" in value:
        candidate = value.replace("/data_processed/", "/processed/")
        if (not os.path.exists(value)) and os.path.exists(candidate):
            return candidate
    if value.endswith("/data_processed"):
        candidate = value[: -len("/data_processed")] + "/processed"
        if (not os.path.exists(value)) and os.path.exists(candidate):
            return candidate
    return value

def _collect_dicom_files(root_path):
    files = []
    root_text = str(root_path or "").strip()
    if not root_text or not os.path.exists(root_text):
        return files
    if os.path.isfile(root_text):
        return [root_text] if root_text.lower().endswith(".dcm") else []
    for current_root, _, names in os.walk(root_text):
        for name in sorted(names):
            if str(name).lower().endswith(".dcm"):
                files.append(os.path.join(current_root, name))
    return files

def _snapshot_scene_nodes():
    rows = []
    scene = slicer.mrmlScene
    try:
        total = int(scene.GetNumberOfNodes())
    except Exception:
        total = 0
    for idx in range(total):
        node = scene.GetNthNode(idx)
        if node is None:
            continue
        try:
            rows.append((
                str(node.GetID() or ""),
                str(node.GetName() or ""),
                str(node.GetClassName() or ""),
            ))
        except Exception:
            continue
    return rows

def _new_scene_nodes(before_rows, after_rows):
    before_ids = set(row[0] for row in before_rows if row and row[0])
    created = []
    for node_id, name, class_name in after_rows:
        if not node_id or node_id in before_ids:
            continue
        created.append({{"id": node_id, "name": name, "class": class_name}})
    return created

def _run_dicom_diffusion_import(module_name, requested_module_name, resolved_module_name, resolve_strategy, inputs, parameters):
    dicom_dir = ""
    if isinstance(inputs, dict):
        for key in ("dicomDirectory", "inputDicomDirectory", "directory", "path"):
            value = inputs.get(key)
            if isinstance(value, str) and value.strip():
                dicom_dir = _normalize_workspace_path(value)
                break
    if not dicom_dir and isinstance(parameters, dict):
        for key in ("dicomDirectory", "inputDicomDirectory", "directory", "path"):
            value = parameters.get(key)
            if isinstance(value, str) and value.strip():
                dicom_dir = _normalize_workspace_path(value)
                break
    if not dicom_dir:
        return {{
            "ok": False,
            "module": module_name,
            "requested_module": requested_module_name,
            "resolved_module": resolved_module_name,
            "resolve_strategy": resolve_strategy,
            "mode": "policy",
            "policy": "dicom_diffusion_import",
            "error": "Missing DICOM directory input",
            "hint": "Pass inputs.dicomDirectory to run_slicer_module(module='DICOM', ...).",
            "error_type": "invalid_request",
            "retryable": False,
        }}
    if not os.path.exists(dicom_dir):
        return {{
            "ok": False,
            "module": module_name,
            "requested_module": requested_module_name,
            "resolved_module": resolved_module_name,
            "resolve_strategy": resolve_strategy,
            "mode": "policy",
            "policy": "dicom_diffusion_import",
            "error": f"DICOM directory not found: {{dicom_dir}}",
            "error_type": "file_format_invalid",
            "retryable": False,
        }}

    plugins = getattr(slicer.modules, "dicomPlugins", {{}})
    plugin_class = plugins.get("DICOMDiffusionVolumePlugin") if isinstance(plugins, dict) else None
    if plugin_class is None:
        return {{
            "ok": False,
            "module": module_name,
            "requested_module": requested_module_name,
            "resolved_module": resolved_module_name,
            "resolve_strategy": resolve_strategy,
            "mode": "policy",
            "policy": "dicom_diffusion_import",
            "error": "DICOMDiffusionVolumePlugin is not registered in current Slicer session",
            "hint": "Ensure SlicerDMRI diffusion DICOM plugin is available before DICOM import.",
            "error_type": "module_logic_unavailable",
            "retryable": False,
        }}

    dicom_files = _collect_dicom_files(dicom_dir)
    if not dicom_files:
        return {{
            "ok": False,
            "module": module_name,
            "requested_module": requested_module_name,
            "resolved_module": resolved_module_name,
            "resolve_strategy": resolve_strategy,
            "mode": "policy",
            "policy": "dicom_diffusion_import",
            "error": f"No .dcm files found under {{dicom_dir}}",
            "error_type": "file_format_invalid",
            "retryable": False,
        }}

    before_rows = _snapshot_scene_nodes()
    plugin = plugin_class()
    try:
        loadables = plugin.examineForImport([dicom_files])
    except Exception as exc:
        return {{
            "ok": False,
            "module": module_name,
            "requested_module": requested_module_name,
            "resolved_module": resolved_module_name,
            "resolve_strategy": resolve_strategy,
            "mode": "policy",
            "policy": "dicom_diffusion_import",
            "error": f"DICOM diffusion examine failed: {{exc}}",
            "detail": traceback.format_exc(),
            "error_type": "conversion_failed",
            "retryable": False,
        }}
    if not loadables:
        return {{
            "ok": False,
            "module": module_name,
            "requested_module": requested_module_name,
            "resolved_module": resolved_module_name,
            "resolve_strategy": resolve_strategy,
            "mode": "policy",
            "policy": "dicom_diffusion_import",
            "error": "No diffusion DICOM loadable recognized in the provided directory",
            "hint": "Directory may not contain a diffusion series recognized by SlicerDMRI.",
            "error_type": "conversion_failed",
            "retryable": False,
            "dicomFileCount": len(dicom_files),
        }}

    selected = None
    for loadable in loadables:
        if bool(getattr(loadable, "selected", False)):
            selected = loadable
            break
    if selected is None:
        selected = max(loadables, key=lambda item: float(getattr(item, "confidence", 0.0) or 0.0))

    try:
        success = bool(plugin.load(selected))
    except Exception as exc:
        return {{
            "ok": False,
            "module": module_name,
            "requested_module": requested_module_name,
            "resolved_module": resolved_module_name,
            "resolve_strategy": resolve_strategy,
            "mode": "policy",
            "policy": "dicom_diffusion_import",
            "error": f"DICOM diffusion load failed: {{exc}}",
            "detail": traceback.format_exc(),
            "error_type": "conversion_failed",
            "retryable": False,
            "dicomFileCount": len(dicom_files),
        }}

    after_rows = _snapshot_scene_nodes()
    created_nodes = _new_scene_nodes(before_rows, after_rows)
    created_dwi_nodes = [
        item
        for item in created_nodes
        if isinstance(item, dict)
        and "diffusionweightedvolumenode" in str(item.get("class") or "").lower()
    ]
    if not success:
        # Some SlicerDMRI plugin paths can return False while still materializing
        # a valid DWI node in scene. Treat that as recoverable success so the
        # executor can continue the module pipeline instead of failing hard.
        if created_dwi_nodes:
            return {{
                "ok": True,
                "module": module_name,
                "requested_module": requested_module_name,
                "resolved_module": resolved_module_name,
                "resolve_strategy": resolve_strategy,
                "mode": "policy",
                "policy": "dicom_diffusion_import",
                "dicomDirectory": dicom_dir,
                "dicomFileCount": len(dicom_files),
                "selectedLoadableName": str(getattr(selected, "name", "") or ""),
                "selectedLoadableTooltip": str(getattr(selected, "tooltip", "") or ""),
                "createdNodes": created_nodes,
                "warning": "DICOM diffusion plugin returned failure but created a diffusion-weighted scene node; continuing with created node(s).",
            }}
        return {{
            "ok": False,
            "module": module_name,
            "requested_module": requested_module_name,
            "resolved_module": resolved_module_name,
            "resolve_strategy": resolve_strategy,
            "mode": "policy",
            "policy": "dicom_diffusion_import",
            "error": "DICOM diffusion plugin returned failure",
            "error_type": "conversion_failed",
            "retryable": False,
            "dicomFileCount": len(dicom_files),
            "createdNodes": created_nodes,
        }}

    return {{
        "ok": True,
        "module": module_name,
        "requested_module": requested_module_name,
        "resolved_module": resolved_module_name,
        "resolve_strategy": resolve_strategy,
        "mode": "policy",
        "policy": "dicom_diffusion_import",
        "dicomDirectory": dicom_dir,
        "dicomFileCount": len(dicom_files),
        "selectedLoadableName": str(getattr(selected, "name", "") or ""),
        "selectedLoadableTooltip": str(getattr(selected, "tooltip", "") or ""),
        "createdNodes": created_nodes,
    }}

def _first_created_node_by_class(created_nodes, class_fragment):
    target = str(class_fragment or "").strip().lower()
    for item in (created_nodes or []):
        if not isinstance(item, dict):
            continue
        class_name = str(item.get("class") or "").strip().lower()
        node_id = str(item.get("id") or "").strip()
        if target and target in class_name and node_id:
            node = slicer.mrmlScene.GetNodeByID(node_id)
            if node is not None:
                return node
    return None

def _save_node_to_path(node_obj, path_text):
    path_value = str(path_text or "").strip()
    if node_obj is None or not path_value:
        return None
    path_value = _normalize_workspace_path(path_value)
    parent = os.path.dirname(path_value)
    if parent:
        os.makedirs(parent, exist_ok=True)
    saved = bool(slicer.util.saveNode(node_obj, path_value))
    if not saved:
        raise RuntimeError(f"saveNode failed for '{{path_value}}'")
    if not os.path.exists(path_value):
        raise RuntimeError(f"saveNode reported success but file missing: {{path_value}}")
    return path_value

def _reroute_dwiconvert_dicom_to_nrrd(module_name, requested_module_name, resolved_module_name, resolve_strategy, inputs, outputs, parameters):
    import_result = _run_dicom_diffusion_import(
        module_name,
        requested_module_name,
        resolved_module_name,
        resolve_strategy,
        inputs,
        parameters,
    )
    if not bool(import_result.get("ok")):
        return import_result

    created_nodes = list(import_result.get("createdNodes") or [])
    dwi_node = _first_created_node_by_class(created_nodes, "DiffusionWeightedVolumeNode")
    if dwi_node is None:
        return {{
            "ok": False,
            "module": module_name,
            "requested_module": requested_module_name,
            "resolved_module": resolved_module_name,
            "resolve_strategy": resolve_strategy,
            "mode": "policy",
            "policy": "dwiconvert_dicom_reroute",
            "error": "DICOM diffusion import succeeded but no diffusion-weighted scene node was created.",
            "error_type": "conversion_failed",
            "retryable": False,
            "createdNodes": created_nodes,
        }}

    saved_paths = []
    out_path = ""
    out_dir = ""
    if isinstance(outputs, dict):
        for key in ("outputVolume", "outputvolume"):
            value = outputs.get(key)
            if isinstance(value, str) and value.strip():
                out_path = value.strip()
                break
        for key in ("outputDirectory", "outputdirectory"):
            value = outputs.get(key)
            if isinstance(value, str) and value.strip():
                out_dir = value.strip()
                break
    if not out_path and out_dir:
        out_path = os.path.join(str(out_dir).strip(), f"{{dwi_node.GetName()}}.nhdr")
    if out_path:
        saved = _save_node_to_path(dwi_node, out_path)
        if saved:
            saved_paths.append(saved)

    return {{
        "ok": True,
        "module": module_name,
        "requested_module": requested_module_name,
        "resolved_module": resolved_module_name,
        "resolve_strategy": resolve_strategy,
        "mode": "policy",
        "policy": "dwiconvert_dicom_reroute",
        "reroutedFrom": "DWIConvert(DicomToNrrd)",
        "reroutedTo": "DICOMDiffusionVolumePlugin",
        "createdNodes": created_nodes,
        "savedPaths": saved_paths,
        "selectedLoadableName": import_result.get("selectedLoadableName"),
        "selectedLoadableTooltip": import_result.get("selectedLoadableTooltip"),
        "dicomDirectory": import_result.get("dicomDirectory"),
        "dicomFileCount": import_result.get("dicomFileCount"),
    }}

def _infer_output_class(param_name):
    key = str(param_name or "").lower()
    if "tensor" in key:
        return "vtkMRMLDiffusionTensorVolumeNode"
    if "label" in key or "mask" in key:
        return "vtkMRMLLabelMapVolumeNode"
    if "segment" in key:
        return "vtkMRMLSegmentationNode"
    if "fiber" in key or "tract" in key:
        return "vtkMRMLFiberBundleNode"
    if "model" in key:
        return "vtkMRMLModelNode"
    if "transform" in key:
        return "vtkMRMLLinearTransformNode"
    return "vtkMRMLScalarVolumeNode"

def _infer_output_class_from_meta(param_name, param_meta):
    key = str(param_name or "").lower()
    ptype = str((param_meta or {{}}).get("type") or "").strip().lower()
    if "tensor" in key:
        return "vtkMRMLDiffusionTensorVolumeNode"
    if ptype == "diffusion-weighted":
        return "vtkMRMLDiffusionWeightedVolumeNode"
    if ptype == "vector":
        return "vtkMRMLVectorVolumeNode"
    if ptype == "scalar":
        return "vtkMRMLScalarVolumeNode"
    if ptype == "transform":
        return "vtkMRMLLinearTransformNode"
    if ptype == "geometry":
        if "fiber" in key or "tract" in key:
            return "vtkMRMLFiberBundleNode"
        return "vtkMRMLModelNode"
    if ptype == "table":
        return "vtkMRMLTableNode"
    if "label" in key or "mask" in key:
        return "vtkMRMLLabelMapVolumeNode"
    return _infer_output_class(param_name)

def _meta_type(param_meta):
    return str((param_meta or {{}}).get("type") or "").strip().lower()

def _meta_channel(param_meta):
    return str((param_meta or {{}}).get("channel") or "").strip().lower()

def _is_likely_cli_file_param_name(param_name):
    key = str(param_name or "").strip().lower()
    if not key:
        return False
    file_markers = (
        "file",
        "directory",
        "bval",
        "bvec",
        "bvalues",
        "bvectors",
        "gradientvector",
    )
    return any(marker in key for marker in file_markers)

def _is_cli_file_like_param(param_meta, param_name=""):
    ptype = _meta_type(param_meta)
    if ptype in {{"file", "directory"}}:
        return True
    # Some CLI modules (notably DWIConvert) label path-like parameters as "scalar".
    if ptype == "scalar" and _is_likely_cli_file_param_name(param_name):
        return True
    return False

def _is_cli_node_like_param(param_meta, param_name=""):
    ptype = _meta_type(param_meta)
    if ptype == "scalar" and _is_likely_cli_file_param_name(param_name):
        return False
    # Per Slicer CLI contract: images/geometry/points/regions are node-based.
    return ptype in {{
        "image",
        "scalar",
        "label",
        "vector",
        "tensor",
        "diffusion-weighted",
        "geometry",
        "transform",
        "table",
        "point",
        "region",
        "seed",
        "fiberbundle",
    }}

def _is_cli_node_output(param_meta, param_name=""):
    return _meta_channel(param_meta) == "output" and _is_cli_node_like_param(param_meta, param_name)

def _is_ukf_module(module_name_text):
    return str(module_name_text or "").strip().lower() == "ukftractography"

def _default_output_node_name(module_name, param_name):
    safe_module = str(module_name or "Module").replace(" ", "_")
    safe_param = str(param_name or "output").replace(" ", "_")
    return f"{{safe_module}}_{{safe_param}}"

def _node_name_from_output_path(path_text, fallback_name):
    try:
        text = str(path_text or "").strip()
    except Exception:
        return str(fallback_name or "output")
    if not text:
        return str(fallback_name or "output")
    base = os.path.basename(text)
    lowered = base.lower()
    for ext in (".nii.gz", ".nhdr", ".nrrd", ".vtk", ".vtp", ".mha", ".mhd", ".h5", ".tfm"):
        if lowered.endswith(ext):
            base = base[: -len(ext)]
            break
    if not base:
        return str(fallback_name or "output")
    safe = "".join(ch if (ch.isalnum() or ch in ("_", "-", ".")) else "_" for ch in base)
    return safe or str(fallback_name or "output")

def _infer_temp_ext_for_node(param_name, param_meta, node_obj):
    key = str(param_name or "").lower()
    ptype = _meta_type(param_meta)
    cls = node_obj.GetClassName() if hasattr(node_obj, "GetClassName") else ""
    cls_lower = str(cls).lower()
    if "fiber" in key or "tract" in key or "fiberbundle" in cls_lower or ptype in {{"geometry", "fiberbundle"}}:
        return ".vtp"
    if "tensor" in key or "diffusiontensor" in cls_lower or ptype == "tensor":
        return ".nrrd"
    if "diffusionweighted" in cls_lower or ptype == "diffusion-weighted":
        return ".nhdr"
    if "labelmap" in cls_lower or ptype == "label" or "mask" in key:
        return ".nrrd"
    if ptype == "transform" or "transform" in cls_lower:
        return ".h5"
    return ".nrrd"

def _materialize_node_for_cli_path(node_obj, param_name, param_meta):
    if node_obj is None:
        raise ValueError(f"Cannot materialize empty node for parameter '{{param_name}}'")
    out_dir = os.path.join(tempfile.gettempdir(), "dmri_agent_slicer_cli")
    os.makedirs(out_dir, exist_ok=True)
    ext = _infer_temp_ext_for_node(param_name, param_meta, node_obj)
    node_name = node_obj.GetName() if hasattr(node_obj, "GetName") else "node"
    stamp = str(int(time.time() * 1000))
    file_name = f"{{module_name}}_{{param_name}}_{{node_name}}_{{stamp}}{{ext}}".replace(" ", "_")
    out_path = os.path.join(out_dir, file_name)
    saved = bool(slicer.util.saveNode(node_obj, out_path))
    if not saved:
        raise RuntimeError(f"saveNode failed for '{{param_name}}' to '{{out_path}}'")
    if not os.path.exists(out_path):
        raise RuntimeError(f"saveNode reported success but file missing: {{out_path}}")
    return out_path

def _load_node_for_cli_input(path_text, param_name, param_meta):
    text = str(path_text or "").strip()
    if not text:
        return None
    if not os.path.exists(text):
        return None
    ptype = _meta_type(param_meta)
    key = str(param_name or "").lower()
    lower = text.lower()
    try:
        if ptype == "fiberbundle" or "tract" in key or lower.endswith((".vtk", ".vtp")):
            loaded = slicer.util.loadFiberBundle(text)
            return loaded if loaded is not False else None
        loaded = slicer.util.loadVolume(text)
        return loaded if loaded is not False else None
    except Exception:
        return None

def _ensure_labelmap_node(node_obj, name_hint="mask"):
    if node_obj is None:
        return None
    try:
        cls = str(node_obj.GetClassName() or "")
    except Exception:
        cls = ""
    if "LabelMapVolumeNode" in cls:
        return _force_volume_to_unsigned_char(node_obj)
    if not _is_volume_node(node_obj):
        return node_obj
    label_name = f"{{str(name_hint or 'mask')}}_labelmap"
    label_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", label_name)
    if label_node is None:
        return node_obj
    try:
        volumes_logic = slicer.modules.volumes.logic() if hasattr(slicer.modules, "volumes") else None
        if volumes_logic is None:
            return node_obj
        volumes_logic.CreateLabelVolumeFromVolume(slicer.mrmlScene, label_node, node_obj)
        return _force_volume_to_unsigned_char(label_node)
    except Exception:
        try:
            slicer.mrmlScene.RemoveNode(label_node)
        except Exception:
            pass
        return node_obj

def _force_volume_to_unsigned_char(node_obj):
    if node_obj is None or not _is_volume_node(node_obj):
        return node_obj
    try:
        image = node_obj.GetImageData()
        if image is None:
            return node_obj
        scalar_type = int(image.GetScalarType())
    except Exception:
        return node_obj
    allowed = {{
        int(vtk.VTK_CHAR),
        int(vtk.VTK_UNSIGNED_CHAR),
        int(vtk.VTK_SHORT),
        int(vtk.VTK_UNSIGNED_SHORT),
    }}
    if scalar_type in allowed:
        return node_obj
    try:
        cast = vtk.vtkImageCast()
        cast.SetInputData(image)
        cast.SetOutputScalarTypeToUnsignedChar()
        cast.ClampOverflowOn()
        cast.Update()
        node_obj.SetAndObserveImageData(cast.GetOutput())
        node_obj.Modified()
    except Exception:
        pass
    return node_obj

def _count_nonzero_voxels(node_obj):
    if node_obj is None or not _is_volume_node(node_obj):
        return None
    try:
        import numpy as _np
        arr = slicer.util.arrayFromVolume(node_obj)
        if arr is None:
            return None
        return int(_np.count_nonzero(arr))
    except Exception:
        return None

def _as_node_if_available(text):
    node = slicer.mrmlScene.GetNodeByID(text)
    if node is not None:
        return node
    try:
        return slicer.util.getNode(text)
    except Exception:
        return None

def _is_scene_node_reference(value):
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    if text.startswith(("node:", "new:")):
        return text.startswith("node:")
    return _as_node_if_available(text) is not None

def resolve_value(v, created_nodes):

    if isinstance(v, str) and v.startswith("node:"):
        node_id = v[len("node:"):]
        node = slicer.mrmlScene.GetNodeByID(node_id)
        if node is None:
            raise ValueError(f"MRML node not found: {{node_id}}")
        return node
    if isinstance(v, str) and v.startswith("new:"):
        class_name = v[len("new:"):]
        if class_name in ("ScalarVolume", "VectorVolume", "DiffusionWeightedVolume", "DiffusionTensorVolume", "LabelMapVolume", "Model", "Segmentation", "FiberBundle", "LinearTransform"):
            class_name = "vtkMRML" + class_name + "Node"
        elif not class_name.startswith("vtkMRML"):
            class_name = "vtkMRML" + class_name + "Node"
        node = slicer.mrmlScene.AddNewNodeByClass(class_name)
        if node is None:
            raise ValueError(f"Failed to create node class: {{class_name}}")
        created_nodes.append({{"id": node.GetID(), "name": node.GetName(), "class": node.GetClassName()}})
        return node
    return v

def _normalize_key(text):
    return "".join(ch for ch in str(text or "").lower() if ch.isalnum())

def _build_cli_name_map(cli_parameters):
    name_map = {{}}
    for item in (cli_parameters or []):
        canonical = str(item.get("name") or "").strip()
        if not canonical:
            continue
        for candidate in (
            canonical,
            item.get("label"),
            item.get("tag"),
            canonical.replace("_", ""),
            canonical.replace("-", ""),
        ):
            normalized = _normalize_key(candidate)
            if normalized and normalized not in name_map:
                name_map[normalized] = canonical
    return name_map

def _build_cli_meta_map(cli_parameters):
    meta_map = {{}}
    for item in (cli_parameters or []):
        if not isinstance(item, dict):
            continue
        canonical = str(item.get("name") or "").strip()
        if not canonical:
            continue
        meta_map[canonical] = item
    return meta_map

def _canonical_cli_key(raw_key, name_map):
    normalized = _normalize_key(raw_key)
    return name_map.get(normalized, str(raw_key))

def _is_volume_node(node):
    return hasattr(node, "GetImageData") and callable(getattr(node, "GetImageData"))

def _node_signature(node):
    if node is None or not _is_volume_node(node):
        return None
    image = node.GetImageData()
    dims = tuple(image.GetDimensions()) if image is not None else ()
    spacing = tuple(float(v) for v in node.GetSpacing()) if hasattr(node, "GetSpacing") else ()
    origin = tuple(float(v) for v in node.GetOrigin()) if hasattr(node, "GetOrigin") else ()
    matrix = vtk.vtkMatrix4x4()
    if hasattr(node, "GetIJKToRASMatrix"):
        node.GetIJKToRASMatrix(matrix)
    mat_vals = tuple(round(float(matrix.GetElement(r, c)), 6) for r in range(4) for c in range(4))
    return (dims, spacing, origin, mat_vals)

def _signatures_close(sig_a, sig_b, tol=1e-4):
    if sig_a is None or sig_b is None:
        return False
    dims_a, spacing_a, origin_a, mat_a = sig_a
    dims_b, spacing_b, origin_b, mat_b = sig_b
    if tuple(dims_a) != tuple(dims_b):
        return False
    for seq_a, seq_b in ((spacing_a, spacing_b), (origin_a, origin_b), (mat_a, mat_b)):
        if len(seq_a) != len(seq_b):
            return False
        for av, bv in zip(seq_a, seq_b):
            if abs(float(av) - float(bv)) > tol:
                return False
    return True

def _generate_mask_from_dwi_scene(dwi_node, created_nodes, module_name_text):
    if dwi_node is None or not _is_volume_node(dwi_node):
        return (None, None, "Input DWI node is missing or invalid.")
    baseline_name = f"{{str(module_name_text or 'DWI')}}_baseline"
    mask_name = f"{{str(module_name_text or 'DWI')}}_mask"
    baseline_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", baseline_name)
    mask_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", mask_name)
    if baseline_node is None or mask_node is None:
        return (None, None, "Failed to create baseline/mask output nodes in Slicer scene.")
    if baseline_node is not None:
        created_nodes.append({{"id": baseline_node.GetID(), "name": baseline_node.GetName(), "class": baseline_node.GetClassName()}})
    if mask_node is not None:
        created_nodes.append({{"id": mask_node.GetID(), "name": mask_node.GetName(), "class": mask_node.GetClassName()}})
    try:
        cli_module = slicer.modules.diffusionweightedvolumemasking
    except Exception:
        cli_module = None
    if cli_module is None:
        return (None, None, "DiffusionWeightedVolumeMasking module is not available in this Slicer session.")
    params = {{
        "inputVolume": dwi_node,
        "outputBaseline": baseline_node,
        "thresholdMask": mask_node,
        "baselineBValueThreshold": 100,
        "removeIslands": True,
    }}
    cli_node = slicer.cli.runSync(cli_module, None, params, update_display=False)
    if cli_node is None:
        return (None, None, "DiffusionWeightedVolumeMasking returned no CLI node.")
    status_text = ""
    error_text = ""
    try:
        status_text = str(cli_node.GetStatusString() or "")
    except Exception:
        status_text = ""
    try:
        error_text = str(cli_node.GetErrorText() or "")
    except Exception:
        error_text = ""
    try:
        failed = bool(cli_node.GetStatus() & cli_node.ErrorsMask)
    except Exception:
        failed = False
    if failed:
        detail = error_text or status_text or "DiffusionWeightedVolumeMasking failed."
        return (None, None, detail)
    mask_node = _ensure_labelmap_node(mask_node, mask_name)
    nonzero = _count_nonzero_voxels(mask_node)
    if nonzero is not None and nonzero <= 0:
        return (None, None, "DiffusionWeightedVolumeMasking produced an empty mask.")
    return (baseline_node, mask_node, "")

def _classify_cli_failure(error_text, status_text):
    message = str(error_text or "").strip()
    status_msg = str(status_text or "").strip()
    lowered = (message + "\\n" + status_msg).lower()
    if not lowered:
        return ("cli_failed", False)
    rules = [
        ("module_not_found", ("module not found", "no such module")),
        ("parameter_schema_mismatch", ("unknown parameter", "invalid parameter", "parameter resolution failed", "missing output volume name")),
        ("conversion_failed", ("conversionmode", "dwiconvert", "allowlossyconversion", "output file type is not supported")),
        ("file_format_invalid", ("not in the same coordinate space", "unsupported format", "invalid file", "not supported currently")),
    ]
    for error_type, markers in rules:
        if any(marker in lowered for marker in markers):
            return (error_type, False)
    transient_markers = ("temporarily unavailable", "connection refused", "timeout", "timed out", "resource busy", "i/o error")
    if any(marker in lowered for marker in transient_markers):
        return ("transport_unavailable", True)
    return ("cli_failed", False)

if not module_name:
    __execResult = {{
        "ok": False,
        "error": "module is required",
        "error_type": "invalid_request",
        "retryable": False,
    }}
else:
    inspected = inspect_module(module_name)
    if not inspected.get("ok"):
        __execResult = inspected
    else:
        requested_module_name = str(inspected.get("requested_module") or module_name)
        resolved_module_name = str(inspected.get("resolved_module") or inspected.get("module") or module_name)
        resolve_strategy = str(inspected.get("resolve_strategy") or "exact")
        module_name = resolved_module_name
        module_type = inspected.get("type")
        created_nodes = []
        if module_name.lower() == "dicom":
            __execResult = _run_dicom_diffusion_import(
                module_name,
                requested_module_name,
                resolved_module_name,
                resolve_strategy,
                inputs,
                parameters,
            )
        elif mode == "logic" or (mode == "auto" and module_type in ("scripted", "loadable")):
            try:
                logic = slicer.util.getModuleLogic(module_name)
            except Exception:
                __execResult = {{
                    "ok": False,
                    "module": module_name,
                    "requested_module": requested_module_name,
                    "resolved_module": resolved_module_name,
                    "resolve_strategy": resolve_strategy,
                    "mode": "logic",
                    "error": "No module logic available",
                    "error_type": "module_logic_unavailable",
                    "retryable": False,
                }}
            else:
                available_methods = inspected.get("logicMethods", [])
                picked_method = method_name
                if module_type == "loadable" and picked_method is None:
                    __execResult = {{
                        "ok": False,
                        "module": module_name,
                        "requested_module": requested_module_name,
                        "resolved_module": resolved_module_name,
                        "resolve_strategy": resolve_strategy,
                        "mode": "logic",
                        "error": (
                            "No logic method specified for loadable module. "
                            "Use run_slicer_module(action='inspect') to choose a method, "
                            "or use mcp_slicer_execute_python_code for scene glue "
                            "(load volume/mask, node binding, display control)."
                        ),
                        "error_type": "parameter_schema_mismatch",
                        "retryable": False,
                        "availableMethods": available_methods[:120],
                    }}
                else:
                    if picked_method is None:
                        if mode == "logic":
                            picked_method = "run"
                        else:
                            for candidate in ("run", "process", "apply", "execute"):
                                if candidate in available_methods:
                                    picked_method = candidate
                                    break
                    member = getattr(logic, picked_method, None) if picked_method else None
                    if not callable(member):
                        __execResult = {{
                            "ok": False,
                            "module": module_name,
                            "requested_module": requested_module_name,
                            "resolved_module": resolved_module_name,
                            "resolve_strategy": resolve_strategy,
                            "mode": "logic",
                            "error": f"Method not callable: {{picked_method}}",
                            "error_type": "parameter_schema_mismatch",
                            "retryable": False,
                            "availableMethods": available_methods[:120],
                        }}
                    else:
                        try:
                            if isinstance(method_args, dict):
                                result = member(**method_args)
                            elif isinstance(method_args, list):
                                result = member(*method_args)
                            else:
                                result = member()
                            __execResult = {{
                                "ok": True,
                                "module": module_name,
                                "requested_module": requested_module_name,
                                "resolved_module": resolved_module_name,
                                "resolve_strategy": resolve_strategy,
                                "mode": "logic",
                                "method": picked_method,
                                "result": str(result),
                                "createdNodes": created_nodes,
                            }}
                        except Exception:
                            __execResult = {{
                                "ok": False,
                                "module": module_name,
                                "requested_module": requested_module_name,
                                "resolved_module": resolved_module_name,
                                "resolve_strategy": resolve_strategy,
                                "mode": "logic",
                                "method": picked_method,
                                "error": traceback.format_exc(),
                                "error_type": "logic_execution_failed",
                                "retryable": False,
                            }}
        elif mode in ("cli", "auto") and module_type == "cli":
            params = {{}}
            bound_output_nodes = {{}}
            try:
                cli_name_map = _build_cli_name_map(inspected.get("cliParameters", []))
                cli_meta_map = _build_cli_meta_map(inspected.get("cliParameters", []))
                is_ukf_module = _is_ukf_module(module_name)
                bound_inputs = {{}}
                materialized_inputs = []
                pending_output_saves = []
                if isinstance(inputs, dict):
                    for raw_key, value in inputs.items():
                        key = _canonical_cli_key(raw_key, cli_name_map)
                        key_meta = cli_meta_map.get(key, {{}})
                        if isinstance(value, str):
                            value = _normalize_workspace_path(value)
                        if isinstance(value, str) and not value.startswith(("node:", "new:")):
                            candidate = _as_node_if_available(value.strip())
                            if candidate is not None:
                                resolved = candidate
                            else:
                                resolved = value
                        else:
                            resolved = resolve_value(value, created_nodes)

                        if isinstance(resolved, str) and _looks_like_path(resolved) and _is_cli_node_like_param(key_meta, key):
                            loaded_node = _load_node_for_cli_input(resolved, key, key_meta)
                            if loaded_node is not None:
                                resolved = loaded_node
                                bound_inputs[key] = resolved

                        if is_ukf_module and key in ("seedsFile", "maskFile") and hasattr(resolved, "GetID"):
                            normalized_node = _ensure_labelmap_node(resolved, key)
                            if normalized_node is not None:
                                resolved = normalized_node
                                bound_inputs[key] = resolved

                        if _is_cli_file_like_param(key_meta, key) and hasattr(resolved, "GetID") and not (
                            is_ukf_module and key in ("maskFile", "seedsFile")
                        ):
                            staged_path = _materialize_node_for_cli_path(resolved, key, key_meta)
                            params[key] = staged_path
                            materialized_inputs.append({{
                                "key": key,
                                "sourceNodeId": resolved.GetID(),
                                "stagedPath": staged_path,
                            }})
                        else:
                            params[key] = resolved
                        bound_inputs[key] = resolved
                if is_ukf_module:
                    if "seedsFile" not in params and "maskFile" in params:
                        params["seedsFile"] = params["maskFile"]
                    seeds_param = params.get("seedsFile")
                    seeds_per_voxel_raw = params.get("seedsPerVoxel", 1)
                    try:
                        seeds_per_voxel = int(float(seeds_per_voxel_raw))
                    except Exception:
                        seeds_per_voxel = 1
                    if seeds_per_voxel < 1:
                        seeds_per_voxel = 1
                    if hasattr(seeds_param, "GetID"):
                        seed_nonzero = _count_nonzero_voxels(seeds_param)
                        if seed_nonzero is not None:
                            estimated_seeds = int(seed_nonzero * seeds_per_voxel)
                            if estimated_seeds > 200000:
                                __execResult = {{
                                    "ok": False,
                                    "module": module_name,
                                    "requested_module": requested_module_name,
                                    "resolved_module": resolved_module_name,
                                    "resolve_strategy": resolve_strategy,
                                    "mode": "cli",
                                    "error": (
                                        f"UKF seeding volume is too large: nonzero_voxels={{seed_nonzero}}, "
                                        f"seedsPerVoxel={{seeds_per_voxel}}, estimated_seeds={{estimated_seeds}}. "
                                        "This likely triggers extremely long runs or blocked output."
                                    ),
                                    "hint": (
                                        "Use a small ROI labelmap as seedsFile (e.g., corpus callosum), "
                                        "and keep maskFile as tracking mask."
                                    ),
                                    "error_type": "file_format_invalid",
                                    "retryable": False,
                                    "createdNodes": created_nodes,
                                }}
                                raise RuntimeError("ukf_seed_volume_too_large")
                explicit_output_keys = set()
                dwiconvert_path_params = {
                    "inputdicomdirectory",
                    "fslniftifile",
                    "inputbvalues",
                    "inputbvectors",
                    "outputvolume",
                    "outputdirectory",
                    "outputniftifile",
                    "outputbvalues",
                    "outputbvectors",
                }
                if isinstance(outputs, dict):
                    for raw_key, value in outputs.items():
                        key = _canonical_cli_key(raw_key, cli_name_map)
                        key_meta = cli_meta_map.get(key, {{}})
                        key_channel = str(key_meta.get("channel") or "").strip().lower()
                        is_node_output = _is_cli_node_output(key_meta, key)
                        key_lower = str(key or "").strip().lower()
                        dwiconvert_path_like = (
                            module_name.lower() == "dwiconvert" and key_lower in dwiconvert_path_params
                        )
                        explicit_output_keys.add(key)
                        if isinstance(value, dict):
                            # Accept typed output specs produced by planners, e.g.
                            # a mapping with keys like type/path or mrmlNodeId.
                            if value.get("path") is not None:
                                value = value.get("path")
                            elif value.get("node") is not None:
                                value = value.get("node")
                            elif value.get("mrmlNodeId") is not None:
                                value = value.get("mrmlNodeId")
                            elif value.get("id") is not None:
                                value = value.get("id")
                        if is_node_output and isinstance(value, str) and value.startswith("node:"):
                            node_ref = value[len("node:"):].strip()
                            existing_node = slicer.mrmlScene.GetNodeByID(node_ref) if node_ref else None
                            if existing_node is None and node_ref:
                                existing_node = _as_node_if_available(node_ref)
                            if existing_node is not None:
                                params[key] = existing_node
                                bound_output_nodes[key] = existing_node
                                continue
                            out_class = _infer_output_class_from_meta(key, key_meta)
                            fallback_name = _default_output_node_name(module_name, key)
                            out_name = _node_name_from_output_path(node_ref, fallback_name)
                            out_node = slicer.mrmlScene.AddNewNodeByClass(out_class, out_name)
                            if out_node is None:
                                raise ValueError(f"Failed to create output node class={{out_class}} name={{out_name}}")
                            created_nodes.append({{"id": out_node.GetID(), "name": out_node.GetName(), "class": out_node.GetClassName()}})
                            params[key] = out_node
                            bound_output_nodes[key] = out_node
                            continue
                        if isinstance(value, str) and value:
                            raw = _normalize_workspace_path(value.strip())
                            if raw and not raw.startswith(("node:", "new:")):
                                candidate = _as_node_if_available(raw)
                                if candidate is not None:
                                    params[key] = candidate
                                    continue
                                if key_channel == "output" and _looks_like_path(raw) and (
                                    _is_cli_file_like_param(key_meta, key) or dwiconvert_path_like
                                ):
                                    params[key] = raw
                                    continue
                                if dwiconvert_path_like and _looks_like_path(raw):
                                    params[key] = raw
                                    continue
                            if is_node_output and raw and not raw.startswith(("node:", "new:")):
                                if dwiconvert_path_like and _looks_like_path(raw):
                                    params[key] = raw
                                    continue
                                out_class = _infer_output_class_from_meta(key, key_meta)
                                fallback_name = _default_output_node_name(module_name, key)
                                out_name = raw if not _looks_like_path(raw) else _node_name_from_output_path(raw, fallback_name)
                                out_node = slicer.mrmlScene.AddNewNodeByClass(out_class, out_name)
                                if out_node is None:
                                    raise ValueError(f"Failed to create output node class={{out_class}} name={{out_name}}")
                                created_nodes.append({{"id": out_node.GetID(), "name": out_node.GetName(), "class": out_node.GetClassName()}})
                                params[key] = out_node
                                bound_output_nodes[key] = out_node
                                if _looks_like_path(raw):
                                    pending_output_saves.append({{
                                        "key": key,
                                        "node": out_node,
                                        "path": raw,
                                    }})
                                continue
                        resolved_output = resolve_value(value, created_nodes)
                        params[key] = resolved_output
                        if hasattr(resolved_output, "GetID") and hasattr(resolved_output, "GetClassName"):
                            bound_output_nodes[key] = resolved_output
                if isinstance(parameters, dict):
                    for raw_key, value in parameters.items():
                        key = _canonical_cli_key(raw_key, cli_name_map)
                        if isinstance(value, str):
                            value = _normalize_workspace_path(value)
                        params[key] = value

                blocked_cli_result = None

                # Normalize common conversion aliases for DWIConvert.
                if module_name.lower() == "dwiconvert":
                    conv_key = _canonical_cli_key("conversionMode", cli_name_map)
                    conv_value = str(params.get(conv_key, "") or "").strip()
                    aliases = {{
                        "niftitonrrd": "FSLToNrrd",
                        "nifti_to_nrrd": "FSLToNrrd",
                        "nifti2nrrd": "FSLToNrrd",
                        "fsltonrrd": "FSLToNrrd",
                    }}
                    normalized = aliases.get(conv_value.lower())
                    if normalized:
                        params[conv_key] = normalized
                    # If caller omits conversionMode but provides FSL-style inputs,
                    # force FSLToNrrd to avoid falling back to DicomToNrrd defaults.
                    if not str(params.get(conv_key, "") or "").strip():
                        fsl_keys = (
                            _canonical_cli_key("fslNIFTIFile", cli_name_map),
                            _canonical_cli_key("inputBValues", cli_name_map),
                            _canonical_cli_key("inputBVectors", cli_name_map),
                        )
                        if any(str(params.get(k, "") or "").strip() for k in fsl_keys):
                            params[conv_key] = "FSLToNrrd"
                    conv_final = str(params.get(conv_key, "") or "").strip().lower()
                    if conv_final == "dicomtonrrd":
                        blocked_cli_result = _reroute_dwiconvert_dicom_to_nrrd(
                            module_name,
                            requested_module_name,
                            resolved_module_name,
                            resolve_strategy,
                            inputs if isinstance(inputs, dict) else {{}},
                            outputs if isinstance(outputs, dict) else {{}},
                            parameters if isinstance(parameters, dict) else {{}},
                        )
                    elif conv_final == "fsltonrrd":
                        # DWIConvert FSLToNrrd expects path-style outputVolume and in practice
                        # is sensitive to how output path pieces are passed. Normalize to:
                        #   inputVolume=<nifti>, outputVolume=<file name>, outputDirectory=<parent dir>
                        in_vol_key = _canonical_cli_key("inputVolume", cli_name_map)
                        fsl_nii_key = _canonical_cli_key("fslNIFTIFile", cli_name_map)
                        out_vol_key = _canonical_cli_key("outputVolume", cli_name_map)
                        out_dir_key = _canonical_cli_key("outputDirectory", cli_name_map)
                        if (not str(params.get(in_vol_key, "") or "").strip()) and str(params.get(fsl_nii_key, "") or "").strip():
                            params[in_vol_key] = params.get(fsl_nii_key)
                        out_vol_raw = str(params.get(out_vol_key, "") or "").strip()
                        if not out_vol_raw:
                            guessed_name = "converted_dwi.nhdr"
                            in_guess = str(params.get(fsl_nii_key, "") or "").strip()
                            if in_guess:
                                in_base = os.path.basename(in_guess)
                                if in_base.lower().endswith(".nii.gz"):
                                    guessed_name = in_base[:-7] + ".nhdr"
                                elif in_base.lower().endswith(".nii"):
                                    guessed_name = in_base[:-4] + ".nhdr"
                            params[out_vol_key] = guessed_name
                            out_vol_raw = guessed_name
                        if out_vol_raw:
                            out_vol_raw = _normalize_workspace_path(out_vol_raw)
                            if _looks_like_path(out_vol_raw):
                                out_parent = os.path.dirname(out_vol_raw) or "."
                                out_name = os.path.basename(out_vol_raw)
                                out_name_lower = out_name.lower()
                                # Per Slicer docs, outputVolume should be .nhdr/.nrrd for FSLToNrrd.
                                if out_name_lower.endswith(".nii") or out_name_lower.endswith(".nii.gz"):
                                    out_name = os.path.splitext(os.path.splitext(out_name)[0])[0] + ".nhdr"
                                elif not (out_name_lower.endswith(".nhdr") or out_name_lower.endswith(".nrrd")):
                                    out_name = out_name + ".nhdr"
                                params[out_vol_key] = out_name
                                if not str(params.get(out_dir_key, "") or "").strip():
                                    params[out_dir_key] = out_parent
                        lossy_key = _canonical_cli_key("allowLossyConversion", cli_name_map)
                        if lossy_key not in params:
                            params[lossy_key] = True

                if blocked_cli_result is not None:
                    __execResult = blocked_cli_result
                else:

                    # Auto-create missing node outputs for common Slicer CLI modules.
                    for key, key_meta in cli_meta_map.items():
                        if key in params:
                            continue
                        key_lower = str(key or "").strip().lower()
                        # UKF second-tensor output is optional and often empty.
                        # Only auto-create it when explicitly requested.
                        if module_name.lower() == "ukftractography" and "secondtensor" in str(key).lower():
                            if key not in explicit_output_keys:
                                continue
                        if module_name.lower() == "dwiconvert" and (
                            _is_cli_file_like_param(key_meta, key) or key_lower in dwiconvert_path_params
                        ):
                            continue
                        if not _is_cli_node_output(key_meta, key):
                            continue
                        out_class = _infer_output_class_from_meta(key, key_meta)
                        out_name = _default_output_node_name(module_name, key)
                        out_node = slicer.mrmlScene.AddNewNodeByClass(out_class, out_name)
                        if out_node is None:
                            continue
                        created_nodes.append({{"id": out_node.GetID(), "name": out_node.GetName(), "class": out_node.GetClassName()}})
                        params[key] = out_node
                        bound_output_nodes[key] = out_node

                    # Preflight: guard against common DWI/mask geometry mismatch in SlicerDMRI tensor fitting.
                    if module_name.lower() == "dwitodtiestimation":
                        dwi_node = None
                        mask_node = None
                        dwi_input_ref = None
                        mask_input_ref = None
                        for key, node_value in bound_inputs.items():
                            lowered_key = str(key).lower()
                            if dwi_node is None and ("inputvolume" in lowered_key or "inputdwi" in lowered_key):
                                if _is_volume_node(node_value):
                                    dwi_node = node_value
                                if isinstance(inputs, dict):
                                    dwi_input_ref = inputs.get(key, inputs.get(str(key), dwi_input_ref))
                            if mask_node is None and ("mask" in lowered_key):
                                if _is_volume_node(node_value):
                                    mask_node = node_value
                                if isinstance(inputs, dict):
                                    mask_input_ref = inputs.get(key, inputs.get(str(key), mask_input_ref))
                        if dwi_input_ref is not None and not _is_scene_node_reference(dwi_input_ref):
                            __execResult = {{
                                "ok": False,
                                "module": module_name,
                                "requested_module": requested_module_name,
                                "resolved_module": resolved_module_name,
                                "resolve_strategy": resolve_strategy,
                                "mode": "cli",
                                "error": (
                                    "DWIToDTIEstimation requires the DWI input to be an existing node "
                                    "in the current Slicer scene, not an external file path."
                                ),
                                "hint": (
                                    "Load the converted diffusion volume into the current scene first, "
                                    "then pass its node ID to run_slicer_module."
                                ),
                                "error_type": "invalid_request",
                                "retryable": False,
                                "createdNodes": created_nodes,
                            }}
                            raise RuntimeError("dwi_scene_node_required")
                        auto_generate_mask = False
                        auto_mask_reason = ""
                        if mask_input_ref is None:
                            auto_generate_mask = True
                            auto_mask_reason = "Mask input was not provided."
                        elif not _is_scene_node_reference(mask_input_ref):
                            auto_generate_mask = True
                            auto_mask_reason = "Mask input was an external file path instead of a current scene node."
                        if dwi_node is not None and mask_node is not None and not auto_generate_mask:
                            sig_dwi = _node_signature(dwi_node)
                            sig_mask = _node_signature(mask_node)
                            if sig_dwi is not None and sig_mask is not None and not _signatures_close(sig_dwi, sig_mask):
                                auto_generate_mask = True
                                auto_mask_reason = "Existing mask node is not in the same coordinate space as the DWI node."
                        if dwi_node is not None and auto_generate_mask:
                            baseline_node, generated_mask_node, mask_error = _generate_mask_from_dwi_scene(
                                dwi_node,
                                created_nodes,
                                module_name,
                            )
                            if generated_mask_node is None:
                                __execResult = {{
                                    "ok": False,
                                    "module": module_name,
                                    "requested_module": requested_module_name,
                                    "resolved_module": resolved_module_name,
                                    "resolve_strategy": resolve_strategy,
                                    "mode": "cli",
                                    "error": (
                                        "Unable to obtain a valid brain mask from the same DWI scene for "
                                        "DWIToDTIEstimation."
                                    ),
                                    "hint": (
                                        f"Auto-mask generation was attempted because: {{auto_mask_reason}} "
                                        f"Mask generation error: {{mask_error}}"
                                    ),
                                    "error_type": "file_format_invalid",
                                    "retryable": False,
                                    "createdNodes": created_nodes,
                                }}
                                raise RuntimeError("dwi_mask_geometry_mismatch")
                            # Rebind the mask input to the newly generated labelmap node.
                            mask_node = generated_mask_node
                            for candidate_key in ("inputMaskVolume", "maskVolume", "brainMask", "thresholdMask"):
                                if candidate_key in cli_name_map.values() or candidate_key in params:
                                    params[_canonical_cli_key(candidate_key, cli_name_map)] = generated_mask_node
                            bound_inputs["inputMaskVolume"] = generated_mask_node
                        if dwi_node is not None and mask_node is not None:
                            sig_dwi = _node_signature(dwi_node)
                            sig_mask = _node_signature(mask_node)
                            if sig_dwi is not None and sig_mask is not None and not _signatures_close(sig_dwi, sig_mask):
                                __execResult = {{
                                    "ok": False,
                                    "module": module_name,
                                    "requested_module": requested_module_name,
                                    "resolved_module": resolved_module_name,
                                    "resolve_strategy": resolve_strategy,
                                    "mode": "cli",
                                    "error": (
                                        "DWI/mask geometry mismatch: mask is not in the same coordinate "
                                        "space as the DWI node even after attempting an in-scene mask generation."
                                    ),
                                    "hint": (
                                        "Use the DWI node already loaded in the current scene and let "
                                        "DiffusionWeightedVolumeMasking create the mask from that same node."
                                    ),
                                    "error_type": "file_format_invalid",
                                    "retryable": False,
                                    "createdNodes": created_nodes,
                                }}
                                raise RuntimeError("dwi_mask_geometry_mismatch")
            except Exception as exc:
                if str(exc) in (
                    "dwi_mask_geometry_mismatch",
                    "ukf_seed_volume_too_large",
                    "dwi_scene_node_required",
                    "dwi_mask_scene_node_required",
                ):
                    pass
                else:
                    __execResult = {{
                        "ok": False,
                        "module": module_name,
                        "requested_module": requested_module_name,
                        "resolved_module": resolved_module_name,
                        "resolve_strategy": resolve_strategy,
                        "mode": "cli",
                        "error": f"Parameter resolution failed: {{exc}}",
                        "error_type": "parameter_schema_mismatch",
                        "retryable": False,
                    }}
            else:
                if "__execResult" in globals() and isinstance(__execResult, dict) and not __execResult.get("ok", True):
                    pass
                else:
                    mod = slicer.util.getModule(resolved_module_name)
                    cli_node = slicer.cli.runSync(mod, None, params, update_display=False)
                    status = int(cli_node.GetStatus()) if cli_node else None
                    ok_cli = bool(cli_node) and not bool(cli_node.GetStatus() & cli_node.ErrorsMask)
                    saved_outputs = []
                    save_errors = []
                    auto_load_warnings = []
                    cli_error_text = cli_node.GetErrorText() if cli_node else ""
                    cli_status_string = ""
                    if cli_node is not None:
                        try:
                            cli_status_string = str(cli_node.GetStatusString() or "")
                        except Exception:
                            cli_status_string = ""
                    if ok_cli and pending_output_saves:
                        import os
                        for item in pending_output_saves:
                            node = item.get("node")
                            save_path = str(item.get("path") or "").strip()
                            key = str(item.get("key") or "")
                            try:
                                parent_dir = os.path.dirname(save_path)
                                if parent_dir:
                                    os.makedirs(parent_dir, exist_ok=True)
                                saved_ok = bool(slicer.util.saveNode(node, save_path))
                                if not saved_ok:
                                    raise RuntimeError("saveNode returned False")
                                saved_outputs.append({{
                                    "key": key,
                                    "path": save_path,
                                    "sizeBytes": int(os.path.getsize(save_path)) if os.path.exists(save_path) else None,
                                }})
                            except Exception as exc:
                                save_errors.append(f"{{key}} -> {{save_path}}: {{exc}}")
                        if save_errors:
                            ok_cli = False
                    if ok_cli and saved_outputs:
                        empty_outputs = [
                            str(item.get("path") or "")
                            for item in saved_outputs
                            if item.get("sizeBytes") in (None, 0)
                        ]
                        if empty_outputs:
                            ok_cli = False
                            save_errors.append(
                                "Generated output file is missing or empty: "
                                + ", ".join(path for path in empty_outputs if path)
                            )
                    if ok_cli and module_name.lower() == "ukftractography":
                        for ukf_key in ("tracts", "tractsWithSecondTensor"):
                            ukf_out = params.get(ukf_key)
                            if not isinstance(ukf_out, str):
                                continue
                            ukf_out = ukf_out.strip()
                            if not ukf_out or not os.path.exists(ukf_out):
                                continue
                            if not any(str(item.get("path") or "") == ukf_out for item in saved_outputs):
                                saved_outputs.append({{
                                    "key": ukf_key,
                                    "path": ukf_out,
                                    "sizeBytes": int(os.path.getsize(ukf_out)) if os.path.exists(ukf_out) else None,
                                }})
                            try:
                                loaded_node = slicer.util.loadFiberBundle(ukf_out)
                                if loaded_node is not None:
                                    created_nodes.append({{
                                        "id": loaded_node.GetID(),
                                        "name": loaded_node.GetName(),
                                        "class": loaded_node.GetClassName(),
                                    }})
                            except Exception as exc:
                                auto_load_warnings.append(f"{{ukf_key}} load warning: {{exc}}")
                    __execResult = {{
                        "ok": ok_cli,
                        "module": module_name,
                        "requested_module": requested_module_name,
                        "resolved_module": resolved_module_name,
                        "resolve_strategy": resolve_strategy,
                        "mode": "cli",
                        "status": status,
                        "statusString": cli_status_string,
                        "createdNodes": created_nodes,
                        "outputNodes": {{
                            str(out_key): {{
                                "id": out_node.GetID(),
                                "name": out_node.GetName(),
                                "class": out_node.GetClassName(),
                            }}
                            for out_key, out_node in bound_output_nodes.items()
                            if hasattr(out_node, "GetID") and hasattr(out_node, "GetClassName")
                        }},
                        "materializedInputs": materialized_inputs,
                        "savedOutputs": saved_outputs,
                        "error": None if ok_cli else (
                            "; ".join(save_errors)
                            if save_errors
                            else (
                                cli_error_text
                                or cli_status_string
                                or ("No CLI node returned" if cli_node is None else "CLI failed without explicit error text")
                            )
                        ),
                    }}
                    if not ok_cli:
                        classified_type, classified_retryable = _classify_cli_failure(
                            __execResult.get("error"),
                            cli_status_string,
                        )
                        __execResult["error_type"] = classified_type
                        __execResult["retryable"] = bool(classified_retryable)
                    if auto_load_warnings:
                        __execResult["warnings"] = auto_load_warnings
        else:
            __execResult = {{
                "ok": False,
                "module": module_name,
                "requested_module": requested_module_name,
                "resolved_module": resolved_module_name,
                "resolve_strategy": resolve_strategy,
                "mode": mode,
                "error": f"Unsupported module routing for type={{module_type}}",
                "error_type": "unsupported_module_route",
                "retryable": False,
            }}

try:
    if "__execResult" in globals():
        if isinstance(__execResult, dict):
            __execResult.setdefault("elapsedSec", round(time.time() - _dmri_t0, 3))
            if __execResult.get("ok") is False:
                __execResult.setdefault("retryable", False)
                __execResult.setdefault("error_type", "module_execution_failed")
        __execResult = _json_safe(__execResult)
except Exception:
    __execResult = {{
        "ok": False,
        "error": "Failed to serialize run_slicer_module result",
        "detail": traceback.format_exc(),
        "elapsedSec": round(time.time() - _dmri_t0, 3),
    }}
"""
    ok, result, error_text, meta = _execute_python_via_mcp(py_code, timeout_sec=float(timeout_sec) + 5.0)
    if not ok:
        retryable, error_type = _classify_transport_error(error_text)
        return _json_error(
            "Failed to run Slicer module",
            detail=error_text,
            module=module_name,
            mode=normalized_mode,
            error_type=error_type,
            retryable=retryable,
            **meta,
        )
    payload = result if isinstance(result, dict) else {"ok": False, "error": "Unexpected run response"}
    if isinstance(payload, dict):
        payload = _compact_run_payload(payload)
    payload["tool"] = "run_slicer_module"
    payload["action"] = "run"
    payload["backend_route"] = "mcp_exec"
    payload["request_url"] = meta.get("request_url")
    return _truncate_json_payload(payload)
