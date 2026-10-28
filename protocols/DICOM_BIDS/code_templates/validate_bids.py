#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _version(executable: str) -> str:
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    lines = [
        line.strip()
        for line in (completed.stdout + "\n" + completed.stderr).splitlines()
        if line.strip()
    ]
    return lines[0] if lines else "unknown"


def _severity_counts(payload: Any) -> dict[str, int]:
    counts: dict[str, int] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            severity = value.get("severity")
            if isinstance(severity, str):
                key = severity.lower()
                counts[key] = counts.get(key, 0) + 1
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the official BIDS validator and retain QC evidence.")
    parser.add_argument("--bids-root", type=Path, required=True)
    parser.add_argument("--qc-root", type=Path, required=True)
    parser.add_argument("--validator", default="bids-validator-deno")
    args = parser.parse_args()

    bids_root = args.bids_root.expanduser().resolve()
    qc_root = args.qc_root.expanduser().resolve()
    if not bids_root.is_dir():
        raise SystemExit(f"BIDS root is not a directory: {bids_root}")
    executable = (
        str(Path(args.validator).expanduser().resolve())
        if Path(args.validator).expanduser().is_absolute()
        else shutil.which(args.validator)
    )
    if not executable or not os.access(executable, os.X_OK):
        raise SystemExit("BIDS validator is not executable; refresh software inventory or install it")

    output_root = qc_root / "bids"
    output_root.mkdir(parents=True, exist_ok=True)
    os.chmod(qc_root, 0o700)
    validator_json = output_root / "validator.json"
    command = [
        executable,
        str(bids_root),
        "--format",
        "json_pp",
        "--outfile",
        str(validator_json),
        "--no-color",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=3600,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        completed_payload = {
            "return_code": 124,
            "stdout_tail": str(exc.stdout or "")[-12000:],
            "stderr_tail": (str(exc.stderr or "") + "\nvalidator timed out")[-12000:],
        }
        validator_payload: Any = {}
        parse_error = "validator timed out after 3600 seconds"
    else:
        completed_payload = {
            "return_code": completed.returncode,
            "stdout_tail": completed.stdout[-12000:],
            "stderr_tail": completed.stderr[-12000:],
        }
        parse_error = None
        try:
            validator_payload = json.loads(validator_json.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            validator_payload = {}
            parse_error = f"{type(exc).__name__}: {exc}"

    severity_counts = _severity_counts(validator_payload)
    error_count = severity_counts.get("error", 0)
    status = (
        "success"
        if completed_payload["return_code"] == 0 and not parse_error and error_count == 0
        else "failed"
    )
    run_manifest = {
        "schema_version": 1,
        "created_at": utc_now(),
        "status": status,
        "bids_root": str(bids_root),
        "validator": {"path": executable, "version": _version(executable)},
        "command": command,
        **completed_payload,
        "severity_counts": severity_counts,
        "parse_error": parse_error,
        "validator_json": str(validator_json),
    }
    run_path = output_root / "validator_run.json"
    run_path.write_text(
        json.dumps(run_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": status,
                "validator_json": str(validator_json),
                "run_manifest": str(run_path),
                "severity_counts": severity_counts,
            },
            ensure_ascii=False,
        )
    )
    return 0 if status == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
