#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate brain mask from DWI using DIPY median_otsu (b0-driven).

Typical usage:
  python brain_mask_median_otsu.py \
    --dwi 100206_dwi.nii.gz \
    --bval 100206_dwi.bval \
    --bvec 100206_dwi.bvec \
    --out /workspace/processed/100206/dwi/100206_dwi_mask.nii.gz

Notes:
- b0 threshold default: b < 50
- Output is uint8 {0,1}
"""

import argparse
import numpy as np
from dipy.io.image import load_nifti, save_nifti
from dipy.io.gradients import read_bvals_bvecs
from dipy.segment.mask import median_otsu


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dwi", required=True)
    ap.add_argument("--bval", required=True)
    ap.add_argument("--bvec", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--b0-thr", type=float, default=50.0)
    ap.add_argument("--median-radius", type=int, default=3)
    ap.add_argument("--numpass", type=int, default=1)
    ap.add_argument("--dilate", type=int, default=1)
    args = ap.parse_args()

    data, affine = load_nifti(args.dwi)
    if data.ndim != 4:
        raise SystemExit(f"Expected 4D DWI, got {data.shape}")

    bvals, bvecs = read_bvals_bvecs(args.bval, args.bvec)
    if bvals.shape[0] != data.shape[-1]:
        raise SystemExit(f"bvals length {bvals.shape[0]} != n_vols {data.shape[-1]}")

    b0_idx = bvals < args.b0_thr
    if not np.any(b0_idx):
        raise SystemExit(f"No b0 found with b<{args.b0_thr}. Check bvals.")

    _masked, mask = median_otsu(
        data,
        vol_idx=np.where(b0_idx)[0],
        median_radius=args.median_radius,
        numpass=args.numpass,
        autocrop=False,
        dilate=args.dilate,
    )

    save_nifti(args.out, mask.astype(np.uint8), affine)
    print(f"[OK] Brain mask saved: {args.out}")
    print(f"     shape={mask.shape}, foreground={int(mask.sum())}")


if __name__ == "__main__":
    main()
