#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gibbs ringing correction (volume-wise) using DIPY gibbs_removal.

Usage:
  python gibbs_removal_dipy.py --in 100206_dwi.nii.gz --out 100206_dwi_gibbs.nii.gz --slice-axis 2

Notes:
- slice_axis typically corresponds to the acquisition slice direction (often z => 2).
- This is NOT a mandatory step; run only when requested or needed.
"""

import argparse
import numpy as np
from dipy.io.image import load_nifti, save_nifti
from dipy.denoise.gibbs import gibbs_removal


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_nii", required=True)
    ap.add_argument("--out", dest="out_nii", required=True)
    ap.add_argument("--slice-axis", type=int, default=2)
    ap.add_argument("--n-points", type=int, default=3)
    args = ap.parse_args()

    data, affine = load_nifti(args.in_nii)
    if data.ndim != 4:
        raise SystemExit(f"Expected 4D DWI, got shape {data.shape}")

    corrected = np.zeros_like(data, dtype=np.float32)
    n_vols = data.shape[-1]

    for i in range(n_vols):
        vol = data[..., i]
        vol_corr = gibbs_removal(
            vol,
            slice_axis=args.slice_axis,
            n_points=args.n_points,
            inplace=False,
        )
        corrected[..., i] = vol_corr.astype(np.float32)

    save_nifti(args.out_nii, corrected, affine)
    print(f"[OK] Gibbs-corrected DWI saved: {args.out_nii}")


if __name__ == "__main__":
    main()
