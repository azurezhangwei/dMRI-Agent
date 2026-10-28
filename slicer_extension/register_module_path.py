"""Register or remove dMRI-Agent using the target Slicer's own settings API."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import traceback

import slicer


def _value(obj, name: str, default=""):
    value = getattr(obj, name, default)
    return value() if callable(value) else value


def _as_list(value) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    try:
        return [str(item) for item in value]
    except TypeError:
        return [str(value)]


def _canonical(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.expanduser(path)))


def _absolute_paths(paths: list[str]) -> list[str]:
    converter = getattr(slicer.app, "toSlicerHomeAbsolutePaths", None)
    if converter is not None:
        try:
            return _as_list(converter(paths))
        except Exception:
            pass
    slicer_home = str(_value(slicer.app, "slicerHome", ""))
    result = []
    for path in paths:
        candidate = Path(path)
        if not candidate.is_absolute() and slicer_home:
            candidate = Path(slicer_home) / candidate
        result.append(os.fspath(candidate))
    return result


def _write_result(payload: dict[str, object]) -> None:
    result_path = Path(os.environ["DMRI_SLICER_REGISTRATION_RESULT"])
    result_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{result_path.name}.", suffix=".tmp", dir=result_path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True, ensure_ascii=True)
            stream.write("\n")
        os.replace(temporary, result_path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _finish(code: int) -> None:
    try:
        slicer.util.exit(code)
    except Exception:
        pass


def main() -> int:
    action = os.environ.get("DMRI_SLICER_REGISTRATION_ACTION", "")
    requested_path = os.environ.get("DMRI_SLICER_MODULE_PATH", "")
    if action not in {"register", "unregister"}:
        raise ValueError(f"Unsupported registration action: {action!r}")
    if not requested_path:
        raise ValueError("DMRI_SLICER_MODULE_PATH is required")
    module_path = _canonical(requested_path)
    if action == "register":
        module_file = Path(module_path) / "DMRIAgentSlicer.py"
        if not module_file.is_file():
            raise ValueError(f"Slicer module was not found: {module_file}")

    settings = slicer.app.revisionUserSettings()
    key = "Modules/AdditionalPaths"
    stored_paths = _as_list(settings.value(key, []))
    absolute_paths = _absolute_paths(stored_paths)

    changed = False
    if action == "register":
        if not any(_canonical(path) == module_path for path in absolute_paths):
            absolute_paths.append(module_path)
            changed = True
    else:
        retained = [path for path in absolute_paths if _canonical(path) != module_path]
        changed = len(retained) != len(absolute_paths)
        absolute_paths = retained

    if changed:
        try:
            stored_value = slicer.app.toSlicerHomeRelativePaths(absolute_paths)
        except Exception:
            stored_value = absolute_paths
        if absolute_paths:
            settings.setValue(key, stored_value)
        else:
            settings.remove(key)
        settings.sync()

    _write_result(
        {
            "schema_version": 1,
            "ok": True,
            "action": action,
            "changed": changed,
            "module_path": module_path,
            "revision": str(_value(slicer.app, "revision", "")),
            "version": str(_value(slicer.app, "applicationVersion", "")),
            "settings_file": str(
                _value(slicer.app, "slicerRevisionUserSettingsFilePath", "")
            ),
        }
    )
    return 0


try:
    _exit_code = main()
except Exception as exc:
    _write_result(
        {
            "schema_version": 1,
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
    )
    _exit_code = 1
_finish(_exit_code)
