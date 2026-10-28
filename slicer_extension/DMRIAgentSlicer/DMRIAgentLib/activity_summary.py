"""Human-readable summaries for Slicer agent plan and tool events."""

import json
import html
import re
import ast
import ntpath
import posixpath
from pathlib import PurePath
from typing import Any


def _short(text: Any, limit: int = 150) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "..."


def _field(instruction: str, name: str) -> str:
    match = re.search(rf"(?im)^\s*{re.escape(name)}:\s*(.+)$", instruction)
    return match.group(1).strip() if match else ""


def plan_review(value: dict[str, Any]) -> tuple[str, str]:
    """Return a compact approval preview and the full inspectable plan."""
    raw = str(value.get("raw") or "").strip()
    steps = [str(step).strip() for step in value.get("steps") or [] if str(step).strip()]
    current = str(value.get("current") or "").strip()
    detail_lines = ["Plan:", *(f"{index}. {step}" for index, step in enumerate(steps, 1))]
    if current:
        detail_lines.append(f"\nCurrent: {current}")
    details = raw or "\n".join(detail_lines)
    preview = "Steps: " + "\n".join(f"{index}. {step}" for index, step in enumerate(steps, 1)) if steps else raw
    if current:
        preview += f"\n\nNext step: {current}"
    return preview or "Review the proposed workflow before execution.", details



def stage_review(instruction: str) -> tuple[str, str]:
    plan = _field(instruction, "Plan")
    current = _field(instruction, "Current")
    task = _field(instruction, "Task")
    lines = []
    if plan:
        lines.append(f"Plan: {_short(plan, 180)}")
    if current:
        lines.append(f"Current: {_short(current, 150)}")
    if not lines and task:
        lines.append(_short(task, 180))
    return "\n".join(lines) or "Review the processing stage before execution.", instruction.strip()


def _readable_name(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).replace("_", " ").strip().capitalize()


def _readable_value(value: Any) -> str:
    if isinstance(value, dict):
        return "\n".join(f"{_readable_name(str(key))}: {_readable_value(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return "\n".join(_readable_value(item) for item in value)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _python_scene_review(code: str) -> list[tuple[str, str]]:
    """Preview literal scene paths without evaluating any supplied Python."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    known = {}

    def value(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return known.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Div)):
            left, right = value(node.left), value(node.right)
            if left is not None and right is not None:
                if isinstance(node.op, ast.Add):
                    return left + right
                return (ntpath if ntpath.isabs(left) and not left.startswith('/') else posixpath).join(left, right)
        if isinstance(node, ast.Call):
            name = ast.unparse(node.func)
            parts = [value(item) for item in node.args]
            if parts and all(item is not None for item in parts):
                if name in {"Path", "pathlib.Path"} and len(parts) == 1:
                    return parts[0]
                if name in {"os.path.join", "ntpath.join", "posixpath.join"}:
                    return (ntpath if ntpath.isabs(parts[0]) and not parts[0].startswith('/') else posixpath).join(*parts)
        return None

    calls = []
    # Resolve calls in statement order. Do not attribute a later reassignment's
    # output path to an earlier save, or infer values through control flow.
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.Expr, ast.Import, ast.ImportFrom)):
            known.clear()
            continue
        for node in ast.walk(statement):
            if isinstance(node, ast.Call):
                calls.append((ast.unparse(node.func), [value(item) for item in node.args]))
        if isinstance(statement, ast.Assign):
            resolved = value(statement.value)
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    known[target.id] = resolved
    inputs, outputs, operations = [], [], []
    for name, arguments in calls:
        if name in {"slicer.util.getNode", "slicer.mrmlScene.GetNodeByID", "slicer.mrmlScene.GetFirstNodeByName", "slicer.util.loadVolume", "slicer.util.loadNodeFromFile"} and arguments:
            item = arguments[0]
            if item:
                inputs.append(item)
        if name in {"slicer.util.saveNode", "slicer.util.saveScene"}:
            index = 1 if name.endswith("saveNode") else 0
            if len(arguments) > index:
                item = arguments[index]
                if item:
                    outputs.append(item)
            operations.append("Save image" if index else "Save scene")
    if not inputs and not outputs:
        return []
    rows = [("Operation", ", ".join(dict.fromkeys(operations)) or "Access Slicer scene")]
    if inputs:
        rows.append(("Detected inputs", "\n".join(dict.fromkeys(inputs))))
    if outputs:
        rows.append(("Detected output paths", "\n".join(dict.fromkeys(outputs))))
    rows.append(("Review", "Literal paths detected from Python. Review full code for any additional effects."))
    return rows


def tool_review(name: str, args: Any) -> tuple[str, str]:
    """Show exact supplied operations and data separately from technical JSON."""
    args = args if isinstance(args, dict) else {}
    details = json.dumps({"tool": name, "arguments": args}, ensure_ascii=False, indent=2)
    rows = []
    if name in {"execute_stage", "execute_slicer_stage"}:
        instruction = str(args.get("instruction") or "")
        for label, key in (("Workflow", "Plan"), ("Current step", "Current"), ("Task", "Task"),
                           ("Inputs", "Inputs"), ("Outputs", "Outputs")):
            value = _field(instruction, key)
            if value:
                rows.append((label, value))
        if not rows:
            rows.append(("Task", instruction or "Run the proposed processing stage"))
        rows.append(("Execution", "The processing agent may run multiple tools for this stage."))
    else:
        if name == "mcp_slicer_execute_python_code":
            rows.extend(_python_scene_review(str(args.get("code") or args.get("python_code") or "")))
        if name == "run_slicer_module":
            module = str(args.get("module") or "unspecified")
            title = {"DWIToDTIEstimation": "Estimate diffusion tensor",
                     "DiffusionTensorScalarMeasurements": "Compute diffusion tensor measurements",
                     "ResampleScalarVolume": "Resample image"}.get(module, module)
            rows.append(("Operation", title))
        elif not rows:
            rows.append(("Operation", tool_activity(name, args)))
        labels = {"inputs": "Inputs", "outputs": "Save results to", "parameters": "Settings",
                  "path": "File", "file_path": "File", "dir_path": "Folder",
                  "command": "Command", "cmd": "Command", "code": "Python code",
                  "python_code": "Python code", "action": "Action"}
        for key, value in args.items():
            if key == "module" and name == "run_slicer_module":
                continue
            if value is None or value == "" or value == {}:
                continue
            rows.append((labels.get(key, _readable_name(str(key))), _readable_value(value)))
        rows = [(label, "Included in technical details below" if label in {"Python code", "Command"}
                 else value) for label, value in rows]
    return "\n\n".join(f"{label}: {value}" for label, value in rows), details


def review_html(summary: str) -> str:
    """Qt-compatible rich text with readable labels and wrapped file paths."""
    blocks = []
    for paragraph in summary.split("\n\n"):
        label, separator, value = paragraph.partition(": ")
        if not separator:
            blocks.append(f"<p>{html.escape(paragraph).replace(chr(10), '<br/>')}</p>")
            continue
        blocks.append(
            "<p style='margin:5px 0 11px 0;'>"
            f"<b style='color:#CBD5E1;'>{html.escape(label)}</b><br/>"
            f"{html.escape(value).replace(chr(10), '<br/>')}</p>"
        )
    return "".join(blocks)


def tool_activity(name: str, args: Any) -> str:
    args = args if isinstance(args, dict) else {}
    if name in {"execute_stage", "execute_slicer_stage"}:
        instruction = str(args.get("instruction") or "")
        stage = _field(instruction, "Current") or _field(instruction, "Task")
        return f"Processing {_short(stage or 'the planned stage', 130)}"
    if name == "load_protocol":
        return f"Reviewing {_short(args.get('name'), 90)} protocol"
    if name == "load_protocol_resource":
        resource = PurePath(str(args.get("relative_path") or "")).name
        return f"Reading {_short(args.get('name'), 65)} workflow template: {_short(resource, 65)}"
    if name == "list_dir":
        return f"Checking files in {_short(args.get('dir_path'), 130)}"
    if name == "glob_files":
        return f"Finding files matching {_short(args.get('pattern'), 110)}"
    if name == "grep_files":
        return f"Searching text for {_short(args.get('pattern'), 110)}"
    if name == "read_file":
        return f"Reading {_short(args.get('file_path'), 130)}"
    if name == "run_shell":
        command = str(args.get("command") or args.get("cmd") or "")
        tools = ("dtifit", "dcm2niix", "eddy", "topup", "UKFTractography", "fslmaths")
        executable = next(
            (item for item in tools if re.search(rf"\b{re.escape(item)}\b", command, re.IGNORECASE)),
            "",
        )
        return f"Running external processing ({executable})" if executable else "Running an external command"
    if name == "run_slicer_module":
        module = _short(args.get("module") or "module", 100)
        action = str(args.get("action") or "run").lower()
        return f"Inspecting Slicer module {module}" if action == "inspect" else f"Running Slicer module {module}"
    if name in {"mcp_slicer_load_image", "mcp_slicer_load_tractography"}:
        filename = PurePath(str(args.get("path") or "result")).name
        return f"Loading {_short(filename, 100)} into Slicer"
    if name == "mcp_slicer_list_nodes":
        return "Checking the Slicer scene"
    if name == "mcp_slicer_capture_screenshot":
        return "Capturing a Slicer screenshot"
    if name == "mcp_slicer_execute_python_code":
        return "Updating the Slicer scene"
    return f"Using {name}"


def tool_outcome(output: Any) -> str:
    if isinstance(output, str):
        try:
            parsed = json.loads(output)
        except (ValueError, TypeError):
            parsed = None
        if "cancelled" in output.lower() or "canceled" in output.lower():
            return "Cancelled"
        if output.lstrip().startswith("[rejected]"):
            return "Rejected"
        if output.lstrip().startswith(("[error]", "[rejected]")):
            return "Failed"
    else:
        parsed = output
    if isinstance(parsed, dict):
        if parsed.get("status") == "cancelled":
            return "Cancelled"
        if parsed.get("ok") is False or parsed.get("status") in {"error", "failed", "cancelled", "timeout"}:
            return "Failed"
        metadata = parsed.get("metadata") or {}
        if isinstance(metadata, dict) and metadata.get("exit_code") not in (None, 0):
            return "Failed"
    return "Completed"
