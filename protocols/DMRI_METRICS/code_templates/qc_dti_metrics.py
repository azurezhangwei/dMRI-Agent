#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from pathlib import Path

import nibabel as nib
import numpy as np


def robust_z(values: dict[str, float]) -> dict[str, float]:
    finite = np.array([v for v in values.values() if np.isfinite(v)], dtype=float)
    if finite.size < 3:
        return {key: float("nan") for key in values}
    med = float(np.median(finite))
    mad = float(np.median(np.abs(finite - med)))
    scale = 1.4826 * mad
    if scale <= 0:
        scale = float(np.std(finite))
    if scale <= 0:
        scale = 1e-12
    return {key: (value - med) / scale if np.isfinite(value) else float("nan") for key, value in values.items()}


def roughness(data: np.ndarray, mask: np.ndarray) -> float:
    vals = []
    for axis in range(3):
        left = [slice(None)] * 3
        right = [slice(None)] * 3
        left[axis] = slice(1, None)
        right[axis] = slice(None, -1)
        pair_mask = mask[tuple(left)] & mask[tuple(right)]
        if np.any(pair_mask):
            vals.append(np.abs(data[tuple(left)] - data[tuple(right)])[pair_mask])
    if not vals:
        return float("nan")
    diffs = np.concatenate(vals)
    denom = float(np.nanstd(data[mask])) + 1e-12
    return float(np.nanmean(diffs) / denom)


def v1_coherence(v1: np.ndarray, mask: np.ndarray) -> float:
    if v1.ndim != 4 or v1.shape[-1] != 3:
        return float("nan")
    vals = []
    for axis in range(3):
        left = [slice(None)] * 3
        right = [slice(None)] * 3
        left[axis] = slice(1, None)
        right[axis] = slice(None, -1)
        pair_mask = mask[tuple(left)] & mask[tuple(right)]
        if np.any(pair_mask):
            a = v1[tuple(left) + (slice(None),)][pair_mask]
            b = v1[tuple(right) + (slice(None),)][pair_mask]
            dots = np.abs(np.sum(a * b, axis=1))
            vals.append(dots[np.isfinite(dots)])
    if not vals:
        return float("nan")
    return float(np.nanmean(np.concatenate(vals)))


def summarize_metric(path: Path, mask_path: Path) -> dict[str, str]:
    img = nib.load(path)
    mask = np.asanyarray(nib.load(mask_path).dataobj) > 0
    data = np.asanyarray(img.dataobj)
    vals = data[mask & np.isfinite(data)]
    if vals.size == 0:
        return {"exists": str(path.exists()), "voxels": "0", "mean": "", "min": "", "max": ""}
    return {
        "exists": "true",
        "voxels": str(int(vals.size)),
        "mean": f"{float(vals.mean()):.8g}",
        "min": f"{float(vals.min()):.8g}",
        "max": f"{float(vals.max()):.8g}",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-qc", required=True)
    parser.add_argument("--processed-root", required=True)
    parser.add_argument("--out-tsv", required=True)
    args = parser.parse_args()

    processed_root = Path(args.processed_root)
    rows: list[dict[str, str]] = []
    with Path(args.input_qc).open("r", encoding="utf-8") as handle:
      reader = csv.DictReader(handle, delimiter="\t")
      for row in reader:
        subject = row["subject_id"]
        outdir = processed_root / subject / "dti_metrics"
        base = outdir / f"{subject}_dtifit"
        result = {"subject_id": subject, "input_status": row["status"], "input_note": row["note"]}
        if row["status"] != "ok":
            result.update(status="skipped", note=row["note"])
            rows.append(result)
            continue
        missing = [
            m
            for m in ("FA", "MD", "AD", "RD", "L1", "L2", "L3", "S0", "V1", "sse")
            if not (base.with_name(f"{base.name}_{m}.nii.gz")).exists()
        ]
        if missing:
            result.update(status="fail", note=f"missing_metrics:{','.join(missing)}")
            rows.append(result)
            continue
        try:
            mask = np.asanyarray(nib.load(row["mask_path"]).dataobj) > 0
            for metric in ("FA", "MD", "AD", "RD"):
                stats = summarize_metric(base.with_name(f"{base.name}_{metric}.nii.gz"), Path(row["mask_path"]))
                for key, value in stats.items():
                    result[f"{metric}_{key}"] = value
            fa = np.asanyarray(nib.load(base.with_name(f"{base.name}_FA.nii.gz")).dataobj)
            l1 = np.asanyarray(nib.load(base.with_name(f"{base.name}_L1.nii.gz")).dataobj)
            l2 = np.asanyarray(nib.load(base.with_name(f"{base.name}_L2.nii.gz")).dataobj)
            l3 = np.asanyarray(nib.load(base.with_name(f"{base.name}_L3.nii.gz")).dataobj)
            s0 = np.asanyarray(nib.load(base.with_name(f"{base.name}_S0.nii.gz")).dataobj)
            sse = np.asanyarray(nib.load(base.with_name(f"{base.name}_sse.nii.gz")).dataobj)
            v1 = np.asanyarray(nib.load(base.with_name(f"{base.name}_V1.nii.gz")).dataobj)
            finite_mask = mask & np.isfinite(fa)
            result["FA_roughness"] = f"{roughness(fa, finite_mask):.8g}"
            result["V1_local_coherence"] = f"{v1_coherence(v1, finite_mask):.8g}"
            result["negative_eigenvalue_fraction"] = f"{float(np.mean((l1[finite_mask] < 0) | (l2[finite_mask] < 0) | (l3[finite_mask] < 0))):.8g}"
            result["normalized_SSE_median"] = f"{float(np.median(sse[finite_mask] / (np.maximum(s0[finite_mask], 1e-6) ** 2))):.8g}"
            result.update(status="ok", note="")
        except Exception as exc:  # noqa: BLE001
            result.update(status="fail", note=f"qc_exception_{type(exc).__name__}:{exc}")
        rows.append(result)

    for metric in ("normalized_SSE_median", "FA_roughness", "V1_local_coherence", "negative_eigenvalue_fraction"):
        values = {
            row["subject_id"]: float(row.get(metric, "nan"))
            for row in rows
            if row.get("status") == "ok"
        }
        zscores = robust_z(values)
        for row in rows:
            if row.get("subject_id") in zscores:
                row[f"{metric}_robust_z"] = f"{zscores[row['subject_id']]:.8g}"

    for row in rows:
        if row.get("status") != "ok":
            continue
        outlier_notes = []
        checks = {
            "normalized_SSE_median_robust_z": "high_normalized_SSE",
            "FA_roughness_robust_z": "high_FA_roughness",
        }
        for key, label in checks.items():
            z = float(row.get(key, "nan"))
            if np.isfinite(z) and z > 4:
                outlier_notes.append(f"{label}_z={z:.3g}")
        z = float(row.get("V1_local_coherence_robust_z", "nan"))
        if np.isfinite(z) and z < -4:
            outlier_notes.append(f"low_V1_local_coherence_z={z:.3g}")
        z = float(row.get("negative_eigenvalue_fraction_robust_z", "nan"))
        fraction = float(row.get("negative_eigenvalue_fraction", "nan"))
        if np.isfinite(z) and np.isfinite(fraction) and z > 10 and fraction > 0.01:
            outlier_notes.append(f"high_negative_eigenvalue_fraction_z={z:.3g}")
        if outlier_notes:
            row["status"] = "warning"
            row["note"] = ";".join(outlier_notes)

    fields = sorted({key for row in rows for key in row})
    out_tsv = Path(args.out_tsv)
    out_tsv.parent.mkdir(parents=True, exist_ok=True)
    with out_tsv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
