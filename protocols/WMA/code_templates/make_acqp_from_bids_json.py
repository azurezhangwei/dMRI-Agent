#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate FSL acqp.txt from BIDS JSON files.

FSL acqp format: one line per series used in topup/eddy
  dx dy dz TotalReadoutTime

Mapping from BIDS PhaseEncodingDirection:
  i  ->  1 0 0
  i- -> -1 0 0
  j  ->  0 1 0
  j- ->  0 -1 0
  k  ->  0 0 1
  k- ->  0 0 -1

Usage:
  python make_acqp_from_bids_json.py --json sub-01_dir-AP_dwi.json sub-01_dir-PA_dwi.json --out acqp.txt

Hard rule:
- MUST read PhaseEncodingDirection + TotalReadoutTime from JSON.
- If missing, exit with error (do not guess).
- For FSL eddy compatibility, TotalReadoutTime values above 0.2 s are written
  as 0.2 s and the original value is reported. This matches the reference WMA
  workflow for GE DWI datasets where dcm2niix may report a value that FSL eddy
  rejects with "Unrealistic read-out time".
"""

import argparse
import json
from pathlib import Path


def ped_to_vec(ped: str):
    axis = ped[0]
    sign = -1 if ped.endswith("-") else 1
    base = {
        "i": (1, 0, 0),
        "j": (0, 1, 0),
        "k": (0, 0, 1),
    }.get(axis)
    if base is None:
        raise ValueError(f"Invalid PhaseEncodingDirection: {ped}")
    return tuple(sign * v for v in base)


def make_line(json_path: Path, max_readout_time: float | None = 0.2) -> str:
    obj = json.loads(json_path.read_text())
    ped = obj.get("PhaseEncodingDirection")
    trt = obj.get("TotalReadoutTime")
    if ped is None or trt is None:
        raise SystemExit(f"Missing PhaseEncodingDirection or TotalReadoutTime in {json_path}")
    dx, dy, dz = ped_to_vec(str(ped))
    trt = float(trt)
    if max_readout_time is not None and trt > max_readout_time:
        print(
            f"[WARN] {json_path}: TotalReadoutTime={trt:.6f} exceeds "
            f"FSL eddy compatibility cap {max_readout_time:.6f}; writing capped value."
        )
        trt = max_readout_time
    return f"{dx} {dy} {dz} {trt:.6f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", nargs="+", required=True, help="One or more BIDS JSON files.")
    ap.add_argument("--out", required=True, help="Output acqp.txt")
    ap.add_argument(
        "--max-readout-time",
        type=float,
        default=0.2,
        help="Cap TotalReadoutTime for FSL eddy compatibility; use 0 to disable.",
    )
    args = ap.parse_args()

    max_readout_time = None if args.max_readout_time == 0 else args.max_readout_time
    lines = [make_line(Path(p), max_readout_time=max_readout_time) for p in args.json]
    Path(args.out).write_text("\n".join(lines) + "\n")

    print(f"[OK] wrote {args.out} with {len(lines)} line(s):")
    for ln in lines:
        print("  " + ln)


if __name__ == "__main__":
    main()
