#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


REQUIRED_FIELDS = (
    "schema_version",
    "report_id",
    "created_at",
    "task",
    "status",
    "stages",
    "outputs",
    "qc",
    "limitations",
)
REPORT_STATUSES = {"success", "partial", "failed", "not_run"}
STAGE_STATUSES = {"success", "warning", "failed", "skipped", "not_run"}
EVIDENCE_REQUIRED_STAGE_STATUSES = {"success", "warning"}
EXISTING_OUTPUT_STATUSES = {"created", "success"}
PRIVATE_KEYS = {"patientid", "patientname", "studyinstanceuid", "seriesinstanceuid"}
UID_RE = re.compile(r"(?<![\d.])(?:\d+\.){4,}\d+(?![\d.])")


def _path_exists(reference: str, manifest_path: Path) -> bool:
    candidate = Path(reference).expanduser()
    if candidate.is_absolute():
        return candidate.exists()
    roots = (manifest_path.parent, manifest_path.parent.parent, Path.cwd())
    return any((root / candidate).exists() for root in roots)


def _validate_privacy(value: Any, location: str = "manifest") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).replace("_", "").lower() in PRIVATE_KEYS and str(item).strip():
                raise ValueError(f"privacy-restricted field is not allowed at {location}.{key}")
            _validate_privacy(item, f"{location}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_privacy(item, f"{location}[{index}]")
        return
    if isinstance(value, str) and UID_RE.search(value):
        raise ValueError(f"raw DICOM UID is not allowed at {location}")


def validate_manifest(payload: dict[str, Any], manifest_path: Path) -> None:
    _validate_privacy(payload)
    if payload["status"] not in REPORT_STATUSES:
        raise ValueError(f"invalid report status: {payload['status']}")
    for index, stage in enumerate(payload["stages"]):
        if not isinstance(stage, dict):
            raise ValueError("stage entries must be objects")
        name = str(stage.get("name") or "").strip()
        status = str(stage.get("status") or "").strip()
        if not name or status not in STAGE_STATUSES:
            raise ValueError(f"invalid stage at index {index}")
        evidence = stage.get("evidence")
        if status in EVIDENCE_REQUIRED_STAGE_STATUSES:
            if not isinstance(evidence, list) or not evidence:
                raise ValueError(f"stage '{name}' status {status} requires evidence")
            missing = [str(item) for item in evidence if not _path_exists(str(item), manifest_path)]
            if missing:
                raise ValueError(f"stage '{name}' evidence does not exist: {missing}")
    for index, output in enumerate(payload["outputs"]):
        if not isinstance(output, dict):
            raise ValueError("output entries must be objects")
        status = str(output.get("status") or "").strip().lower()
        reference = str(output.get("path") or "").strip()
        if status in EXISTING_OUTPUT_STATUSES and (
            not reference or not _path_exists(reference, manifest_path)
        ):
            raise ValueError(f"output at index {index} is marked {status} but does not exist")


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("manifest root must be an object")
    missing = [field for field in REQUIRED_FIELDS if field not in payload]
    if missing:
        raise ValueError("missing required fields: " + ", ".join(missing))
    for field in ("stages", "outputs", "qc", "limitations"):
        if not isinstance(payload[field], list):
            raise ValueError(f"{field} must be a list")
    validate_manifest(payload, path.resolve())
    return payload


def value(value: Any) -> str:
    if value is None or value == "":
        return "not available"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def table(items: list[dict[str, Any]], columns: tuple[str, ...]) -> list[str]:
    if not items:
        return ["No evidence recorded."]
    lines = [
        "| " + " | ".join(column.replace("_", " ").title() for column in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("table entries must be objects")
        cells = [value(item.get(column)).replace("|", "\\|").replace("\n", " ") for column in columns]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def render(manifest: dict[str, Any]) -> str:
    lines = [
        f"# dMRI Report: {value(manifest['report_id'])}",
        "",
        f"- Created: {value(manifest['created_at'])}",
        f"- Status: {value(manifest['status'])}",
        f"- Schema: {value(manifest['schema_version'])}",
        "",
        "## Task",
        "",
        value(manifest["task"]),
        "",
        "## Stages",
        "",
        *table(manifest["stages"], ("name", "status", "summary", "evidence")),
        "",
        "## Outputs",
        "",
        *table(manifest["outputs"], ("name", "path", "status")),
        "",
        "## Quality Control",
        "",
        *table(manifest["qc"], ("name", "status", "detail")),
        "",
        "## Statistics",
        "",
        *table(manifest.get("statistics", []), ("name", "estimate", "uncertainty", "evidence")),
        "",
        "## Limitations",
        "",
    ]
    limitations = manifest["limitations"]
    lines.extend([f"- {value(item)}" for item in limitations] or ["- None recorded."])
    lines.extend(["", "## Provenance", ""])
    lines.extend(table(manifest.get("evidence", []), ("kind", "path", "hash")))
    return "\n".join(lines).rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render a dMRI evidence report")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = render(load_manifest(args.manifest))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
