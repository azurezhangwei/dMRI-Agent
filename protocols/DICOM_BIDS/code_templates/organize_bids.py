#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROTOCOL_VERSION = "1.1.0"
DEFAULT_BIDS_VERSION = "1.11.1"
DIRECT_IDENTIFIER_KEYS = {
    "AccessionNumber",
    "DeviceSerialNumber",
    "InstitutionAddress",
    "InstitutionName",
    "InstitutionalDepartmentName",
    "OperatorsName",
    "PatientAddress",
    "PatientAge",
    "PatientBirthDate",
    "PatientID",
    "PatientName",
    "PatientSex",
    "PatientTelephoneNumbers",
    "PatientWeight",
    "PerformingPhysicianName",
    "ReferringPhysicianName",
    "StationName",
    "StudyID",
}
TEXT_FIELDS = (
    "SeriesDescription",
    "ProtocolName",
    "SequenceName",
    "InternalPulseSequenceName",
    "PulseSequenceName",
    "ProcedureStepDescription",
    "ImageType",
    "ScanningSequence",
)
GENERIC_IDENTITY_VALUES = {
    "anon",
    "anonymous",
    "dicom",
    "na",
    "none",
    "patient",
    "subject",
    "test",
    "unknown",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bids_label(value: Any, fallback: str = "unknown") -> str:
    label = re.sub(r"[^A-Za-z0-9]+", "", str(value or ""))
    return label or fallback


def _stem_without_nifti(path: Path) -> str:
    return path.name[:-7] if path.name.endswith(".nii.gz") else path.stem


def _bundle_extension(path: Path) -> str:
    return ".nii.gz" if path.name.endswith(".nii.gz") else path.suffix


def _series_number(value: Any) -> tuple[int, str]:
    try:
        return int(value), ""
    except (TypeError, ValueError):
        return 2**31 - 1, str(value or "")


def _metadata_text(metadata: dict[str, Any], source_stem: str) -> str:
    values: list[str] = [source_stem]
    for key in TEXT_FIELDS:
        value = metadata.get(key)
        if isinstance(value, list):
            values.extend(str(item) for item in value)
        elif value is not None:
            values.append(str(value))
    return " ".join(values).lower()


def _has_term(text: str, *terms: str) -> bool:
    return any(
        re.search(rf"(?:^|[^a-z0-9]){re.escape(term)}(?:$|[^a-z0-9])", text)
        for term in terms
    )


def _direction_label(metadata: dict[str, Any]) -> str | None:
    explicit_text = " ".join(
        str(metadata.get(key) or "")
        for key in ("SeriesDescription", "ProtocolName")
    )
    explicit = re.search(
        r"(?:^|[^A-Za-z0-9])(AP|PA|LR|RL)(?:$|[^A-Za-z0-9])",
        explicit_text,
        re.IGNORECASE,
    )
    if explicit:
        return explicit.group(1).upper()
    phase_encoding = str(metadata.get("PhaseEncodingDirection") or "").strip()
    return {
        "i": "i",
        "i-": "ineg",
        "j": "j",
        "j-": "jneg",
        "k": "k",
        "k-": "kneg",
    }.get(phase_encoding)


def _sanitize_metadata(value: Any, removed: set[str]) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, child in value.items():
            if key in DIRECT_IDENTIFIER_KEYS:
                removed.add(key)
                continue
            cleaned[key] = _sanitize_metadata(child, removed)
        return cleaned
    if isinstance(value, list):
        return [_sanitize_metadata(child, removed) for child in value]
    return value


def _gradient_error(bval: Path | None, bvec: Path | None) -> str | None:
    if not bval or not bvec:
        return "diffusion sidecars are incomplete"
    try:
        bvals = bval.read_text(encoding="utf-8").split()
        rows = [line.split() for line in bvec.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeError) as exc:
        return f"gradient sidecars are unreadable: {exc}"
    if not bvals:
        return "bval contains no values"
    if len(rows) != 3:
        return f"bvec must contain 3 non-empty rows, found {len(rows)}"
    if any(len(row) != len(bvals) for row in rows):
        return "bval and bvec lengths do not match"
    try:
        [float(value) for value in bvals]
        [float(value) for row in rows for value in row]
    except ValueError:
        return "gradient sidecars contain non-numeric values"
    return None


@dataclass
class Bundle:
    unit_id: str
    subject: str
    session: str | None
    source_json: Path
    source_stem: str
    metadata: dict[str, Any]
    nifti: Path | None
    sidecars: dict[str, Path]
    json_error: str | None = None
    datatype: str | None = None
    suffix: str | None = None
    entities: dict[str, str] = field(default_factory=dict)
    reason: str | None = None
    removed_metadata_keys: set[str] = field(default_factory=set)
    destination_files: list[str] = field(default_factory=list)

    def sort_key(self) -> tuple[Any, ...]:
        return (
            str(self.metadata.get("AcquisitionTime") or ""),
            _series_number(self.metadata.get("SeriesNumber")),
            str(self.source_json),
        )


def _load_bundle(
    json_path: Path,
    *,
    unit_id: str,
    subject: str,
    session: str | None,
) -> Bundle:
    source_stem = json_path.name[:-5]
    json_error: str | None = None
    try:
        loaded = json.loads(json_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("JSON root is not an object")
        metadata = loaded
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        metadata = {}
        json_error = f"{type(exc).__name__}: {exc}"

    nifti = None
    for candidate in (json_path.with_name(source_stem + ".nii.gz"), json_path.with_name(source_stem + ".nii")):
        if candidate.is_file():
            nifti = candidate
            break
    sidecars = {
        extension: candidate
        for extension in (".bval", ".bvec")
        if (candidate := json_path.with_name(source_stem + extension)).is_file()
    }
    return Bundle(
        unit_id=unit_id,
        subject=subject,
        session=session,
        source_json=json_path,
        source_stem=source_stem,
        metadata=metadata,
        nifti=nifti,
        sidecars=sidecars,
        json_error=json_error,
    )


def discover_bundles(manifest: dict[str, Any]) -> tuple[list[Bundle], list[dict[str, str]]]:
    converted_root = Path(manifest["converted_root"]).expanduser().resolve()
    units = [unit for unit in manifest.get("units", []) if isinstance(unit, dict)]
    subject_units: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for unit in units:
        subject_units[str(unit.get("subject_token") or "unknown")].append(unit)
    sessioned = any(len(items) > 1 for items in subject_units.values())

    session_by_unit: dict[str, str | None] = {}
    for subject, items in subject_units.items():
        ordered = sorted(
            items,
            key=lambda item: (
                int(item.get("study_order") or 2**31 - 1),
                str(item.get("study_token") or ""),
            ),
        )
        for index, unit in enumerate(ordered, start=1):
            session_by_unit[str(unit.get("dicom_id"))] = f"{index:02d}" if sessioned else None

    bundles: list[Bundle] = []
    issues: list[dict[str, str]] = []
    for unit in units:
        unit_id = str(unit.get("dicom_id") or "")
        subject = bids_label(unit.get("subject_token"), "unknown")
        unit_root = (converted_root / unit_id).resolve()
        try:
            unit_root.relative_to(converted_root)
        except ValueError:
            issues.append({"unit_id": unit_id, "reason": "unit path escapes converted root"})
            continue
        if not unit_root.is_dir():
            issues.append({"unit_id": unit_id, "reason": "converted unit directory is missing"})
            continue
        json_paths = sorted(unit_root.rglob("*.json"))
        if not json_paths:
            issues.append({"unit_id": unit_id, "reason": "no converted JSON sidecars found"})
            continue
        for json_path in json_paths:
            bundles.append(
                _load_bundle(
                    json_path,
                    unit_id=unit_id,
                    subject=subject,
                    session=session_by_unit[unit_id],
                )
            )
    return bundles, issues


def classify(bundle: Bundle) -> None:
    if bundle.json_error:
        bundle.reason = f"invalid JSON sidecar: {bundle.json_error}"
        return
    if bundle.nifti is None:
        bundle.reason = "NIfTI image is missing"
        return

    bval = bundle.sidecars.get(".bval")
    bvec = bundle.sidecars.get(".bvec")
    if bval or bvec:
        error = _gradient_error(bval, bvec)
        if error:
            bundle.reason = error
            return
        bundle.datatype = "dwi"
        bundle.suffix = "dwi"
        direction = _direction_label(bundle.metadata)
        if direction:
            bundle.entities["dir"] = direction
        return

    text = _metadata_text(bundle.metadata, bundle.source_stem)
    if _has_term(text, "rest", "resting", "bold", "fmri", "task"):
        task_name = bundle.metadata.get("TaskName")
        if task_name:
            task = bids_label(task_name)
        elif _has_term(text, "rest", "resting"):
            task = "rest"
        else:
            bundle.reason = "BOLD-like series has no reliable TaskName"
            return
        bundle.datatype = "func"
        bundle.suffix = "bold"
        bundle.entities["task"] = task
        direction = _direction_label(bundle.metadata)
        if direction:
            bundle.entities["dir"] = direction
        return

    if _has_term(text, "flair"):
        bundle.datatype, bundle.suffix = "anat", "FLAIR"
    elif _has_term(text, "t2", "t2w"):
        bundle.datatype, bundle.suffix = "anat", "T2w"
    elif _has_term(text, "t1", "t1w", "mprage", "fspgr"):
        bundle.datatype, bundle.suffix = "anat", "T1w"
    elif _has_term(text, "fmap", "fieldmap", "topup", "reverse", "blip"):
        if not bundle.metadata.get("B0FieldIdentifier"):
            bundle.reason = "fieldmap-like series has no B0FieldIdentifier; IntendedFor cannot be inferred"
            return
        bundle.datatype, bundle.suffix = "fmap", "epi"
        direction = _direction_label(bundle.metadata)
        if direction:
            bundle.entities["dir"] = direction
    else:
        bundle.reason = "modality cannot be classified with high confidence"


def _entity_prefix(bundle: Bundle, *, run: int | None = None) -> str:
    parts = [f"sub-{bundle.subject}"]
    if bundle.session:
        parts.append(f"ses-{bundle.session}")
    for key in ("task", "acq", "dir"):
        if value := bundle.entities.get(key):
            parts.append(f"{key}-{value}")
    if run is not None:
        parts.append(f"run-{run:02d}")
    return "_".join(parts)


def assign_runs(bundles: Iterable[Bundle]) -> dict[int, int | None]:
    grouped: dict[tuple[str, ...], list[Bundle]] = defaultdict(list)
    for bundle in bundles:
        if not bundle.datatype or not bundle.suffix:
            continue
        grouped[
            (
                bundle.subject,
                bundle.session or "",
                bundle.datatype,
                bundle.suffix,
                bundle.entities.get("task", ""),
                bundle.entities.get("acq", ""),
                bundle.entities.get("dir", ""),
            )
        ].append(bundle)
    assigned: dict[int, int | None] = {}
    for items in grouped.values():
        ordered = sorted(items, key=Bundle.sort_key)
        for index, bundle in enumerate(ordered, start=1):
            assigned[id(bundle)] = index if len(ordered) > 1 else None
    return assigned


def _transfer(source: Path, destination: Path, mode: str) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if mode == "hardlink":
        try:
            os.link(source, destination)
            return "hardlink"
        except OSError:
            pass
    shutil.copy2(source, destination)
    return "copy"


def _write_json(destination: Path, metadata: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_classified(
    bundle: Bundle,
    root: Path,
    *,
    run: int | None,
    transfer_mode: str,
) -> Counter[str]:
    assert bundle.nifti and bundle.datatype and bundle.suffix
    directory = root / f"sub-{bundle.subject}"
    if bundle.session:
        directory /= f"ses-{bundle.session}"
    directory /= bundle.datatype
    filename = f"{_entity_prefix(bundle, run=run)}_{bundle.suffix}"
    transfers: Counter[str] = Counter()

    nifti_destination = directory / (filename + _bundle_extension(bundle.nifti))
    transfers[_transfer(bundle.nifti, nifti_destination, transfer_mode)] += 1
    bundle.destination_files.append(str(nifti_destination.relative_to(root)))

    removed: set[str] = set()
    metadata = _sanitize_metadata(bundle.metadata, removed)
    if bundle.datatype == "func" and "TaskName" not in metadata:
        metadata["TaskName"] = bundle.entities["task"]
    json_destination = directory / (filename + ".json")
    _write_json(json_destination, metadata)
    bundle.removed_metadata_keys.update(removed)
    bundle.destination_files.append(str(json_destination.relative_to(root)))

    for extension, source in sorted(bundle.sidecars.items()):
        destination = directory / (filename + extension)
        transfers[_transfer(source, destination, transfer_mode)] += 1
        bundle.destination_files.append(str(destination.relative_to(root)))
    return transfers


def _write_unclassified(
    bundle: Bundle,
    root: Path,
    *,
    index: int,
    transfer_mode: str,
) -> Counter[str]:
    directory = root / "sourcedata" / "unclassified" / f"sub-{bundle.subject}"
    if bundle.session:
        directory /= f"ses-{bundle.session}"
    directory /= f"series-{index:04d}"
    prefix = _entity_prefix(bundle) + f"_series-{index:04d}_unknown"
    transfers: Counter[str] = Counter()
    if bundle.nifti:
        destination = directory / (prefix + _bundle_extension(bundle.nifti))
        transfers[_transfer(bundle.nifti, destination, transfer_mode)] += 1
        bundle.destination_files.append(str(destination.relative_to(root)))
    for extension, source in sorted(bundle.sidecars.items()):
        destination = directory / (prefix + extension)
        transfers[_transfer(source, destination, transfer_mode)] += 1
        bundle.destination_files.append(str(destination.relative_to(root)))

    removed: set[str] = set()
    metadata = _sanitize_metadata(bundle.metadata, removed)
    metadata["dMRIUnclassifiedReason"] = bundle.reason
    if bundle.json_error:
        metadata["dMRIOriginalJSONRetainedInPrivateWorkspace"] = False
    destination = directory / (prefix + ".json")
    _write_json(destination, metadata)
    bundle.removed_metadata_keys.update(removed)
    bundle.destination_files.append(str(destination.relative_to(root)))
    return transfers


def _write_dataset_files(
    root: Path,
    *,
    dataset_name: str,
    bids_version: str,
    subjects: list[str],
    dcm2niix: dict[str, Any],
) -> None:
    generated_by = [
        {
            "Name": "dMRI-Agent DICOM_BIDS Protocol",
            "Version": PROTOCOL_VERSION,
            "Description": "Privacy-preserving deterministic DICOM-to-BIDS organization",
        }
    ]
    if dcm2niix:
        generated_by.append(
            {
                "Name": "dcm2niix",
                "Version": str(dcm2niix.get("version") or "unknown"),
            }
        )
    _write_json(
        root / "dataset_description.json",
        {
            "Name": dataset_name,
            "BIDSVersion": bids_version,
            "DatasetType": "raw",
            "GeneratedBy": generated_by,
        },
    )
    (root / "README").write_text(
        "This raw BIDS dataset was organized by the dMRI-Agent DICOM_BIDS Protocol.\n"
        "Uncertain series, when present, are retained under sourcedata/unclassified and are not BIDS claims.\n",
        encoding="utf-8",
    )
    with (root / "participants.tsv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(["participant_id"])
        for subject in subjects:
            writer.writerow([f"sub-{subject}"])
    sourcedata = root / "sourcedata"
    if sourcedata.exists():
        (sourcedata / "README").write_text(
            "unclassified contains de-identified converted series that require an explicit routing decision.\n",
            encoding="utf-8",
        )


def _private_identifiers(qc_root: Path) -> list[tuple[str, str]]:
    mapping = qc_root / "private" / "subject_mapping.tsv"
    if not mapping.is_file():
        return []
    identifiers: list[tuple[str, str]] = []
    try:
        with mapping.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle, delimiter="\t"):
                for kind, field in (
                    (str(row.get("patient_source_kind") or "patient"), "patient_source"),
                    ("StudyInstanceUID", "study_source"),
                ):
                    value = str(row.get(field) or "").strip()
                    collapsed = re.sub(r"[^a-z0-9]+", "", value.casefold())
                    if not value or collapsed in GENERIC_IDENTITY_VALUES:
                        continue
                    if kind != "StudyInstanceUID" and len(collapsed) < 6 and "^" not in value:
                        continue
                    identifiers.append((kind, value))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise RuntimeError(f"private subject mapping cannot be audited: {exc}") from exc
    return identifiers


def _forbidden_json_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in DIRECT_IDENTIFIER_KEYS:
                found.add(key)
            found.update(_forbidden_json_keys(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_forbidden_json_keys(child))
    return found


def privacy_audit(root: Path, qc_root: Path) -> dict[str, Any]:
    identifiers = _private_identifiers(qc_root)
    text_files = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and (path.suffix.lower() in {".json", ".tsv", ".txt", ".md"} or path.name == "README")
    ]
    matches: list[dict[str, str]] = []
    for path in text_files:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise RuntimeError(f"privacy audit cannot read {path.relative_to(root)}: {exc}") from exc
        if path.suffix.lower() == ".json":
            try:
                payload = json.loads(content)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"published JSON is invalid: {path.relative_to(root)}: {exc}") from exc
            for key in sorted(_forbidden_json_keys(payload)):
                matches.append(
                    {
                        "kind": "forbidden_json_key",
                        "token": hashlib.sha256(key.encode("utf-8")).hexdigest()[:12],
                        "path": str(path.relative_to(root)),
                    }
                )
        folded = content.casefold()
        for kind, identifier in identifiers:
            escaped = re.escape(identifier.casefold())
            if re.fullmatch(r"[a-z0-9]+", identifier.casefold()):
                pattern = rf"(?<![a-z0-9]){escaped}(?![a-z0-9])"
                found = re.search(pattern, folded) is not None
            else:
                found = identifier.casefold() in folded
            if found:
                matches.append(
                    {
                        "kind": kind,
                        "token": hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:12],
                        "path": str(path.relative_to(root)),
                    }
                )
    if matches:
        raise RuntimeError(
            "privacy audit blocked BIDS publication: "
            + json.dumps(matches, ensure_ascii=True)
        )
    return {
        "status": "pass",
        "mapping_values_checked": len(identifiers),
        "text_files_checked": len(text_files),
        "forbidden_keys": sorted(DIRECT_IDENTIFIER_KEYS),
    }


def _bundle_record(bundle: Bundle) -> dict[str, Any]:
    return {
        "unit_id": bundle.unit_id,
        "subject": f"sub-{bundle.subject}",
        "session": f"ses-{bundle.session}" if bundle.session else None,
        "source_json": str(bundle.source_json),
        "classification": bundle.datatype,
        "suffix": bundle.suffix,
        "entities": bundle.entities,
        "status": "classified" if bundle.datatype else "unclassified",
        "reason": bundle.reason,
        "removed_metadata_keys": sorted(bundle.removed_metadata_keys),
        "destination_files": bundle.destination_files,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Organize privacy-sanitized dcm2niix outputs into a BIDS dataset."
    )
    parser.add_argument("--conversion-manifest", type=Path, required=True)
    parser.add_argument("--bids-root", type=Path, required=True)
    parser.add_argument("--qc-root", type=Path, required=True)
    parser.add_argument("--dataset-name", default="dMRI-Agent converted dataset")
    parser.add_argument("--bids-version", default=DEFAULT_BIDS_VERSION)
    parser.add_argument("--transfer-mode", choices=("copy", "hardlink"), default="copy")
    args = parser.parse_args()

    manifest_path = args.conversion_manifest.expanduser().resolve()
    bids_root = args.bids_root.expanduser().resolve()
    qc_root = args.qc_root.expanduser().resolve()
    if not manifest_path.is_file():
        raise SystemExit(f"conversion manifest not found: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"conversion manifest is invalid: {exc}") from exc
    if not isinstance(manifest, dict) or not manifest.get("converted_root"):
        raise SystemExit("conversion manifest must be an object with converted_root")
    if bids_root.exists() and any(bids_root.iterdir()):
        raise SystemExit(f"BIDS root must be empty: {bids_root}")
    if bids_root.exists():
        bids_root.rmdir()

    qc_root.mkdir(parents=True, exist_ok=True)
    os.chmod(qc_root, 0o700)
    organization_qc = qc_root / "bids"
    organization_qc.mkdir(parents=True, exist_ok=True)

    bundles, discovery_issues = discover_bundles(manifest)
    if not bundles:
        raise SystemExit("no converted series bundles were found")
    for bundle in bundles:
        classify(bundle)
    run_by_bundle = assign_runs(bundles)

    bids_root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{bids_root.name}.staging-", dir=str(bids_root.parent))
    )
    transfer_counts: Counter[str] = Counter()
    privacy_result: dict[str, Any]
    try:
        unclassified_index: dict[tuple[str, str | None], int] = defaultdict(int)
        for bundle in sorted(bundles, key=lambda item: (item.subject, item.session or "", item.sort_key())):
            if bundle.datatype:
                transfer_counts.update(
                    _write_classified(
                        bundle,
                        staging,
                        run=run_by_bundle[id(bundle)],
                        transfer_mode=args.transfer_mode,
                    )
                )
            else:
                key = (bundle.subject, bundle.session)
                unclassified_index[key] += 1
                transfer_counts.update(
                    _write_unclassified(
                        bundle,
                        staging,
                        index=unclassified_index[key],
                        transfer_mode=args.transfer_mode,
                    )
                )
        subjects = sorted({bundle.subject for bundle in bundles})
        _write_dataset_files(
            staging,
            dataset_name=args.dataset_name,
            bids_version=args.bids_version,
            subjects=subjects,
            dcm2niix=manifest.get("dcm2niix") or {},
        )
        privacy_result = privacy_audit(staging, qc_root)
        os.replace(staging, bids_root)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    status_counts = Counter(
        "classified" if bundle.datatype else "unclassified" for bundle in bundles
    )
    conversion_counts = manifest.get("status_counts") or {}
    upstream_incomplete = any(
        int(conversion_counts.get(state, 0) or 0) > 0
        for state in ("partial", "failed")
    )
    overall_status = (
        "success"
        if not status_counts["unclassified"] and not discovery_issues and not upstream_incomplete
        else "partial"
    )
    organization_manifest = {
        "schema_version": 1,
        "created_at": utc_now(),
        "protocol": {"id": "DICOM_BIDS", "version": PROTOCOL_VERSION},
        "status": overall_status,
        "conversion_manifest": str(manifest_path),
        "bids_root": str(bids_root),
        "dataset_name": args.dataset_name,
        "bids_version": args.bids_version,
        "requested_transfer_mode": args.transfer_mode,
        "actual_transfers": dict(sorted(transfer_counts.items())),
        "privacy_audit": privacy_result,
        "status_counts": dict(status_counts),
        "conversion_status_counts": conversion_counts,
        "discovery_issues": discovery_issues,
        "series": [_bundle_record(bundle) for bundle in bundles],
    }
    output_manifest = organization_qc / "organization_manifest.json"
    output_manifest.write_text(
        json.dumps(organization_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": overall_status,
                "bids_root": str(bids_root),
                "manifest": str(output_manifest),
                "status_counts": dict(status_counts),
            },
            ensure_ascii=False,
        )
    )
    return 0 if overall_status == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
