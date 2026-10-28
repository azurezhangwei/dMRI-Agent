#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydicom import dcmread
from pydicom.errors import InvalidDicomError


IDENTITY_TAGS = [
    "PatientID",
    "PatientName",
    "StudyInstanceUID",
    "StudyDate",
    "SeriesInstanceUID",
    "SeriesNumber",
    "SeriesDescription",
    "SOPClassUID",
    "SOPInstanceUID",
]


@dataclass(frozen=True)
class DicomIdentity:
    patient_source: str
    patient_source_kind: str
    study_source: str
    series_uid: str
    series_number: str
    series_description: str


@dataclass(frozen=True)
class SourceGroup:
    source_dir: Path
    identity: DicomIdentity
    files: tuple[Path, ...] = ()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str:
    return str(value or "").strip()


def read_identity(path: Path, dicom_root: Path) -> DicomIdentity | None:
    try:
        dataset = dcmread(
            path,
            stop_before_pixels=True,
            specific_tags=IDENTITY_TAGS,
            force=True,
        )
    except (InvalidDicomError, OSError, ValueError):
        return None
    if not (_text(dataset.get("SOPClassUID")) or _text(dataset.get("SOPInstanceUID"))):
        return None

    patient_id = _text(dataset.get("PatientID"))
    patient_name = _text(dataset.get("PatientName"))
    if patient_id:
        patient_source = patient_id
        patient_source_kind = "PatientID"
    elif patient_name:
        patient_source = patient_name
        patient_source_kind = "PatientName"
    else:
        parent = path.parent
        relative = parent.relative_to(dicom_root) if parent != dicom_root else Path(dicom_root.name)
        patient_source = relative.as_posix()
        patient_source_kind = "relative_directory"

    study_source = (
        _text(dataset.get("StudyInstanceUID"))
        or _text(dataset.get("StudyDate"))
        or "single-study"
    )
    return DicomIdentity(
        patient_source=patient_source,
        patient_source_kind=patient_source_kind,
        study_source=study_source,
        series_uid=_text(dataset.get("SeriesInstanceUID")) or path.parent.as_posix(),
        series_number=_text(dataset.get("SeriesNumber")),
        series_description=_text(dataset.get("SeriesDescription")),
    )


def discover_sources(
    dicom_root: Path,
    *,
    excluded_roots: tuple[Path, ...] = (),
) -> tuple[list[SourceGroup], list[dict[str, Any]]]:
    groups: list[SourceGroup] = []
    observations: list[dict[str, Any]] = []
    excluded = tuple(path.resolve() for path in excluded_roots)

    for current, directory_names, file_names in os.walk(dicom_root, followlinks=False):
        current_path = Path(current).resolve()
        directory_names[:] = sorted(
            name
            for name in directory_names
            if not name.startswith(".")
            and not any((current_path / name).resolve() == root for root in excluded)
        )
        files = [current_path / name for name in sorted(file_names) if not name.startswith(".")]
        if not files:
            continue

        identified: list[tuple[Path, DicomIdentity]] = []
        for path in files:
            identity = read_identity(path, dicom_root)
            if identity is not None:
                identified.append((path, identity))
        if not identified:
            continue

        identity_keys = {
            (item.patient_source, item.study_source)
            for _, item in identified
        }
        if len(identity_keys) == 1:
            groups.append(SourceGroup(source_dir=current_path, identity=identified[0][1]))
            observations.append(
                {
                    "source_dir": str(current_path),
                    "mode": "directory",
                    "readable_dicoms": len(identified),
                    "identity_groups": 1,
                }
            )
            continue

        split: dict[tuple[str, str], list[tuple[Path, DicomIdentity]]] = {}
        for path, identity in identified:
            split.setdefault((identity.patient_source, identity.study_source), []).append(
                (path, identity)
            )
        for values in split.values():
            groups.append(
                SourceGroup(
                    source_dir=current_path,
                    identity=values[0][1],
                    files=tuple(path for path, _ in values),
                )
            )
        observations.append(
                {
                    "source_dir": str(current_path),
                    "mode": "mixed_directory_split",
                    "readable_dicoms": len(identified),
                    "identity_groups": len(split),
                    "dicom_files": sum(len(values) for values in split.values()),
                }
        )
    return groups, observations


def load_or_create_salt(path: Path, supplied: str | None = None) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    if supplied:
        return supplied.encode("utf-8")
    if path.is_file():
        return path.read_text(encoding="utf-8").strip().encode("utf-8")
    value = secrets.token_hex(32)
    path.write_text(value + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return value.encode("utf-8")


def private_token(salt: bytes, namespace: str, value: str, length: int = 12) -> str:
    digest = hmac.new(salt, f"{namespace}:{value}".encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:length]


def _safe_log_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-")
    return cleaned[:80] or "series"


def _prepare_source(group: SourceGroup, staging_root: Path, source_index: int) -> Path:
    if not group.files:
        return group.source_dir
    staging = staging_root / f"source-{source_index:04d}"
    staging.mkdir(parents=True, exist_ok=True)
    for index, source in enumerate(group.files, start=1):
        target = staging / f"{index:08d}.dcm"
        if not target.exists():
            target.symlink_to(source)
    return staging


def _command_version(executable: str) -> str:
    completed = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    lines = [
        line.strip()
        for line in (completed.stdout + "\n" + completed.stderr).splitlines()
        if line.strip()
    ]
    return lines[-1] if lines else "unknown"


def _subprocess_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _run_unit(
    unit: dict[str, Any],
    *,
    executable: str,
    converted_root: Path,
    log_root: Path,
    staging_root: Path,
    compression: str,
) -> dict[str, Any]:
    output_dir = converted_root / unit["dicom_id"]
    output_dir.mkdir(parents=True, exist_ok=True)
    unit_log_root = log_root / unit["dicom_id"]
    unit_log_root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    for source_index, source in enumerate(unit.pop("_groups"), start=1):
        input_path = _prepare_source(source, staging_root / unit["dicom_id"], source_index)
        series_output = output_dir / f"input-{source_index:04d}"
        series_output.mkdir(parents=True, exist_ok=True)
        command = [
            executable,
            "-b",
            "y",
            "-ba",
            "y",
            "-z",
            compression,
            "-i",
            "n",
            "-f",
            "%p_%d_%z_%s_%u_%j",
            "-o",
            str(series_output),
            str(input_path),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=3600,
                check=False,
            )
            return_code = completed.returncode
            stdout_tail = completed.stdout[-12000:]
            stderr_tail = completed.stderr[-12000:]
        except subprocess.TimeoutExpired as exc:
            return_code = 124
            stdout_tail = _subprocess_text(exc.stdout)[-12000:]
            stderr_tail = (
                _subprocess_text(exc.stderr) + "\ndcm2niix timed out after 3600 seconds"
            )[-12000:]
        except OSError as exc:
            return_code = 127
            stdout_tail = ""
            stderr_tail = f"unable to execute dcm2niix: {exc}"
        log_path = unit_log_root / f"{source_index:04d}-{_safe_log_name(source.identity.series_uid)}.json"
        private_result = {
            "source_dir": str(source.source_dir),
            "series_description": source.identity.series_description,
            "command": command,
            "series_uid_token": hashlib.sha256(
                source.identity.series_uid.encode("utf-8")
            ).hexdigest()[:12],
            "series_number": source.identity.series_number,
            "return_code": return_code,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
        }
        log_path.write_text(
            json.dumps(private_result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(log_path, 0o600)
        result = {
            "source_token": private_token(
                b"dmri-agent-source-log",
                "source",
                str(source.source_dir),
            ),
            "series_uid_token": private_result["series_uid_token"],
            "series_number": source.identity.series_number,
            "return_code": return_code,
            "private_log_path": str(log_path),
        }
        results.append(result)

    output_files = sorted(
        str(path)
        for path in output_dir.rglob("*")
        if path.is_file() and not path.name.startswith(".")
    )
    succeeded = sum(result["return_code"] == 0 for result in results)
    unit["series_results"] = results
    unit["outputs"] = output_files
    unit["status"] = (
        "success"
        if results and succeeded == len(results)
        else "partial"
        if succeeded or output_files
        else "failed"
    )
    return unit


def _write_private_mapping(path: Path, units: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(
            [
                "subject_token",
                "study_token",
                "patient_source_kind",
                "patient_source",
                "study_source",
            ]
        )
        for unit in units:
            writer.writerow(
                [
                    unit["subject_token"],
                    unit["study_token"],
                    unit.pop("_patient_source_kind"),
                    unit.pop("_patient_source"),
                    unit.pop("_study_source"),
                ]
            )
    os.chmod(path, 0o600)


def default_workers() -> int:
    available = os.cpu_count() or 1
    return max(1, min(8, int(available * 0.7)))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Discover DICOM series and convert them with privacy-safe dcm2niix settings."
    )
    parser.add_argument("--dicom-root", type=Path, required=True)
    parser.add_argument("--converted-root", type=Path, required=True)
    parser.add_argument("--qc-root", type=Path, required=True)
    parser.add_argument("--dcm2niix", default="dcm2niix")
    parser.add_argument("--workers", type=int, default=default_workers())
    parser.add_argument("--compression", choices=("y", "i", "n"), default="y")
    parser.add_argument("--subject-salt")
    args = parser.parse_args()

    dicom_root = args.dicom_root.expanduser().resolve()
    converted_root = args.converted_root.expanduser().resolve()
    qc_root = args.qc_root.expanduser().resolve()
    if not dicom_root.is_dir():
        raise SystemExit(f"DICOM root is not a directory: {dicom_root}")
    if converted_root.exists() and any(converted_root.iterdir()):
        raise SystemExit(f"converted root must be empty: {converted_root}")
    executable = shutil.which(args.dcm2niix) if not Path(args.dcm2niix).is_absolute() else args.dcm2niix
    if not executable or not os.access(executable, os.X_OK):
        raise SystemExit("dcm2niix is not executable; refresh software inventory or install it")

    converted_root.mkdir(parents=True, exist_ok=True)
    qc_root.mkdir(parents=True, exist_ok=True)
    os.chmod(qc_root, 0o700)
    flatten_qc = qc_root / "flatten"
    conversion_qc = qc_root / "dcm2niix"
    private_root = qc_root / "private"
    private_root.mkdir(parents=True, exist_ok=True)
    os.chmod(private_root, 0o700)
    log_root = private_root / "dcm2niix" / "logs"
    staging_root = converted_root / ".mixed-input-staging"
    salt = load_or_create_salt(qc_root / "private" / "subject_salt.txt", args.subject_salt)
    sources, observations = discover_sources(
        dicom_root,
        excluded_roots=(converted_root, qc_root),
    )
    if not sources:
        raise SystemExit("no readable DICOM series were found")

    grouped: dict[tuple[str, str], list[SourceGroup]] = {}
    for source in sources:
        key = (source.identity.patient_source, source.identity.study_source)
        grouped.setdefault(key, []).append(source)

    units: list[dict[str, Any]] = []
    subject_study_orders: dict[str, int] = {}
    for (patient_source, study_source), groups in sorted(grouped.items()):
        subject_token = private_token(salt, "subject", patient_source)
        study_token = private_token(salt, "study", patient_source + "\0" + study_source, 8)
        study_order = subject_study_orders.get(subject_token, 0) + 1
        subject_study_orders[subject_token] = study_order
        units.append(
            {
                "dicom_id": f"unit-{subject_token}-{study_token}",
                "subject_token": subject_token,
                "study_token": study_token,
                "study_order": study_order,
                "input_group_count": len(groups),
                "_groups": groups,
                "_patient_source": patient_source,
                "_patient_source_kind": groups[0].identity.patient_source_kind,
                "_study_source": study_source,
            }
        )

    flatten_qc.mkdir(parents=True, exist_ok=True)
    private_observations = private_root / "discovery_manifest.json"
    private_observations.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "created_at": utc_now(),
                "dicom_root": str(dicom_root),
                "observations": observations,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(private_observations, 0o600)
    flatten_payload = {
        "schema_version": 1,
        "created_at": utc_now(),
        "dicom_root": str(dicom_root),
        "discovery_passes": 1,
        "source_directories": len(observations),
        "conversion_units": len(units),
        "observations": [
            {
                **{key: value for key, value in observation.items() if key != "source_dir"},
                "source_token": private_token(
                    b"dmri-agent-source-log",
                    "source",
                    observation["source_dir"],
                ),
            }
            for observation in observations
        ],
        "private_discovery_manifest": str(private_observations),
    }
    (flatten_qc / "manifest.json").write_text(
        json.dumps(flatten_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_private_mapping(qc_root / "private" / "subject_mapping.tsv", units)

    completed_units: list[dict[str, Any]] = []
    workers = min(max(1, args.workers), default_workers())
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _run_unit,
                unit,
                executable=str(executable),
                converted_root=converted_root,
                log_root=log_root,
                staging_root=staging_root,
                compression=args.compression,
            ): unit
            for unit in units
        }
        for future in as_completed(futures):
            unit = futures[future]
            try:
                completed_units.append(future.result())
            except Exception as exc:
                completed_units.append(
                    {
                        key: value
                        for key, value in unit.items()
                        if not key.startswith("_")
                    }
                    | {
                        "series_results": [],
                        "outputs": [],
                        "status": "failed",
                        "internal_error": f"{type(exc).__name__}: {exc}",
                    }
                )
    if staging_root.exists():
        shutil.rmtree(staging_root)

    completed_units.sort(key=lambda item: item["dicom_id"])
    status_counts = {
        state: sum(unit["status"] == state for unit in completed_units)
        for state in ("success", "partial", "failed")
    }
    manifest = {
        "schema_version": 1,
        "created_at": utc_now(),
        "dicom_root": str(dicom_root),
        "converted_root": str(converted_root),
        "qc_root": str(qc_root),
        "dcm2niix": {"path": str(executable), "version": _command_version(str(executable))},
        "settings": {
            "bids_sidecars": True,
            "anonymize_bids": True,
            "ignore_derived_localizer_2d": False,
            "compression": args.compression,
            "workers": workers,
        },
        "status_counts": status_counts,
        "units": completed_units,
    }
    conversion_qc.mkdir(parents=True, exist_ok=True)
    manifest_path = conversion_qc / "conversion_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": "success" if not status_counts["failed"] and not status_counts["partial"] else "partial",
                "manifest": str(manifest_path),
                "status_counts": status_counts,
            },
            ensure_ascii=False,
        )
    )
    return 0 if status_counts == {"success": len(completed_units), "partial": 0, "failed": 0} else 2


if __name__ == "__main__":
    raise SystemExit(main())
