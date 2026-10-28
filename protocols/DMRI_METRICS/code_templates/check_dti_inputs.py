#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from pathlib import Path

import nibabel as nib
import numpy as np


def count_values(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").split())


def bvec_shape(path: Path) -> tuple[int, int]:
    rows = [line.split() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        return 0, 0
    return len(rows), len(rows[0])


def find_subject_inputs(subject_dir: Path) -> dict[str, Path | str]:
    subject = subject_dir.name
    search_root = subject_dir / "single_shell" if (subject_dir / "single_shell").is_dir() else subject_dir
    dwi = search_root / f"{subject}.nii.gz"
    bval = search_root / f"{subject}.bval"
    bvec = search_root / f"{subject}.bvec"
    mask = search_root / f"{subject}_bse-multi_BrainMask.nii.gz"
    if not dwi.exists():
        candidates = sorted(p for p in search_root.glob("*.nii.gz") if "mask" not in p.name.lower() and "_bse" not in p.name)
        dwi = candidates[0] if len(candidates) == 1 else dwi
    if not bval.exists():
        candidates = sorted(search_root.glob("*.bval"))
        bval = candidates[0] if len(candidates) == 1 else bval
    if not bvec.exists():
        candidates = sorted(search_root.glob("*.bvec"))
        bvec = candidates[0] if len(candidates) == 1 else bvec
    if not mask.exists():
        candidates = sorted(p for p in search_root.glob("*.nii.gz") if "mask" in p.name.lower())
        mask = candidates[0] if len(candidates) == 1 else mask
    return {
        "subject_id": subject,
        "dwi_path": dwi,
        "bval_path": bval,
        "bvec_path": bvec,
        "mask_path": mask,
    }


def validate(row: dict[str, Path | str]) -> dict[str, str]:
    out = {k: str(v) for k, v in row.items()}
    errors: list[str] = []
    paths = {k: Path(str(out[k])) for k in ("dwi_path", "bval_path", "bvec_path", "mask_path")}
    for label, path in paths.items():
        if not path.exists():
            errors.append(f"missing_{label}")
    if errors:
        out.update(status="fail", note=";".join(errors))
        return out

    try:
        dwi_img = nib.load(paths["dwi_path"])
        mask_img = nib.load(paths["mask_path"])
        n_vols = dwi_img.shape[3] if len(dwi_img.shape) == 4 else 1
        n_bval = count_values(paths["bval_path"])
        bvec_rows, bvec_cols = bvec_shape(paths["bvec_path"])
        mask_data = np.asanyarray(mask_img.dataobj)
        if bvec_rows != 3:
            errors.append(f"bvec_rows_{bvec_rows}")
        if n_bval != n_vols:
            errors.append(f"bval_count_{n_bval}_vs_dwi_vols_{n_vols}")
        if bvec_cols != n_vols:
            errors.append(f"bvec_cols_{bvec_cols}_vs_dwi_vols_{n_vols}")
        if mask_img.shape[:3] != dwi_img.shape[:3]:
            errors.append(f"mask_shape_{mask_img.shape[:3]}_vs_dwi_shape_{dwi_img.shape[:3]}")
        if int(np.count_nonzero(mask_data)) == 0:
            errors.append("empty_mask")
        out["dwi_volumes"] = str(n_vols)
        out["mask_voxels"] = str(int(np.count_nonzero(mask_data)))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"exception_{type(exc).__name__}:{exc}")

    out.update(status="fail" if errors else "ok", note=";".join(errors) if errors else "")
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-root", required=True)
    parser.add_argument("--out-tsv", required=True)
    args = parser.parse_args()

    dataset_root = Path(args.dataset_root)
    rows = [validate(find_subject_inputs(p)) for p in sorted(dataset_root.iterdir()) if p.is_dir()]
    out_tsv = Path(args.out_tsv)
    out_tsv.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "subject_id", "status", "note", "dwi_path", "bval_path", "bvec_path", "mask_path",
        "dwi_volumes", "mask_voxels",
    ]
    with out_tsv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
