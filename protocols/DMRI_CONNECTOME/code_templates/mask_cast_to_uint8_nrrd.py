#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cast a mask NRRD/NHDR to uint8 (unsigned char) for tools that require it.

Usage:
  python mask_cast_to_uint8_nrrd.py in_mask.nrrd out_mask_uint8.nrrd

Notes:
- This reads with pynrrd (import nrrd).
- For NHDR, the header references the raw data; keep file co-location consistent.
"""

import sys
import numpy as np
import nrrd


def main():
    if len(sys.argv) != 3:
        raise SystemExit("Usage: mask_cast_to_uint8_nrrd.py <in.nrrd/nhdr> <out.nrrd/nhdr>")

    in_file, out_file = sys.argv[1], sys.argv[2]
    data, header = nrrd.read(in_file)

    data_u8 = (data > 0).astype(np.uint8)
    header["type"] = "uint8"

    nrrd.write(out_file, data_u8, header)
    print("[OK] Saved:", out_file)


if __name__ == "__main__":
    main()
