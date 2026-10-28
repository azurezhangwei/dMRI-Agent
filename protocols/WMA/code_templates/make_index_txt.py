#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate FSL eddy index.txt.

index.txt: one integer per DWI volume, indicating which row of acqp.txt applies.

Common cases:
1) Single DWI series (e.g., only AP used for eddy):
   - acqp.txt has 1 line
   - index is all 1's

2) Multiple concatenated series:
   - volume-to-series mapping must be known.

This helper supports the single-series default (most common in your pipeline),
where eddy runs on one DWI file and acqp.txt contains one matching line.

Usage:
  python make_index_txt.py --nvol 288 --out index.txt --row 1
"""

import argparse
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nvol", type=int, required=True, help="Number of volumes in DWI.")
    ap.add_argument("--out", required=True)
    ap.add_argument("--row", type=int, default=1, help="1-based row index into acqp.txt")
    args = ap.parse_args()

    if args.nvol <= 0:
        raise SystemExit("nvol must be > 0")
    if args.row <= 0:
        raise SystemExit("row must be >= 1")

    Path(args.out).write_text(" ".join([str(args.row)] * args.nvol) + "\n")
    print(f"[OK] wrote {args.out}: nvol={args.nvol}, row={args.row}")


if __name__ == "__main__":
    main()
