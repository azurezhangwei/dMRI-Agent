#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cohort-level tract QC for WMA outputs.

Goal:
- scan all subjects under a processed root
- collect per-tract file size and NumberOfLines from VTP files
- flag subject/tract pairs that are markedly lower than cohort level

This is a rule-based cohort QC complement to visual QC.
"""

import argparse
import csv
import statistics
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def read_num_lines(vtp_path: Path) -> Optional[int]:
    try:
        root = ET.parse(vtp_path).getroot()
    except Exception:
        return None

    piece = None
    for elem in root.iter():
        if elem.tag.endswith("Piece"):
            piece = elem
            break
    if piece is None:
        return None

    value = piece.attrib.get("NumberOfLines")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def collect_rows(processed_root: Path) -> List[Dict]:
    rows: List[Dict] = []
    for anat_dir in sorted(processed_root.glob("sub*/dwi/WMA/*/AnatomicalTracts")):
        subject = anat_dir.parts[-5]
        for vtp in sorted(anat_dir.glob("*.vtp")):
            rows.append(
                {
                    "subject": subject,
                    "tract_file": vtp.name,
                    "tract_name": vtp.stem,
                    "file_path": str(vtp),
                    "file_size_bytes": vtp.stat().st_size,
                    "num_lines": read_num_lines(vtp),
                }
            )
    return rows


def summarize(rows: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    by_tract: Dict[str, List[Dict]] = {}
    for row in rows:
        by_tract.setdefault(row["tract_file"], []).append(row)

    flags: List[Dict] = []
    stats_rows: List[Dict] = []
    for tract_file, tract_rows in sorted(by_tract.items()):
        sizes = [r["file_size_bytes"] for r in tract_rows]
        lines = [r["num_lines"] for r in tract_rows if r["num_lines"] is not None]
        size_median = statistics.median(sizes) if sizes else 0
        line_median = statistics.median(lines) if lines else None
        size_p10 = statistics.quantiles(sizes, n=10)[0] if len(sizes) >= 10 else min(sizes)
        line_p10 = statistics.quantiles(lines, n=10)[0] if len(lines) >= 10 else (min(lines) if lines else None)

        stats_rows.append(
            {
                "tract_file": tract_file,
                "n_subjects": len(tract_rows),
                "size_median_bytes": int(size_median),
                "size_p10_bytes": int(size_p10),
                "line_median": "" if line_median is None else int(line_median),
                "line_p10": "" if line_p10 is None else int(line_p10),
            }
        )

        for row in tract_rows:
            low_size = row["file_size_bytes"] <= max(size_p10, size_median * 0.25)
            low_lines = (
                row["num_lines"] is not None
                and line_median is not None
                and line_p10 is not None
                and row["num_lines"] <= max(line_p10, line_median * 0.25)
            )
            if low_size and low_lines:
                flags.append(
                    {
                        **row,
                        "flag_reason": "both file size and streamline count are markedly below cohort level",
                        "cohort_size_median": int(size_median),
                        "cohort_line_median": "" if line_median is None else int(line_median),
                    }
                )
    return stats_rows, flags


def write_csv(path: Path, rows: List[Dict], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_md(path: Path, stats_rows: List[Dict], flags: List[Dict]) -> None:
    lines: List[str] = []
    lines.append("# WMA Cohort Tract QC")
    lines.append("")
    lines.append("This report flags subject/tract pairs whose VTP file size and streamline count are both markedly below cohort level.")
    lines.append("")
    lines.append(f"- Tracts summarized: {len(stats_rows)}")
    lines.append(f"- Flagged subject/tract pairs: {len(flags)}")
    lines.append("")
    lines.append("## Flagged Items")
    if not flags:
        lines.append("- None")
    else:
        for row in flags:
            lines.append(
                f"- `{row['subject']}` / `{row['tract_file']}`: "
                f"size={row['file_size_bytes']} bytes, lines={row['num_lines']}, "
                f"cohort medians size={row['cohort_size_median']} bytes, lines={row['cohort_line_median']} "
                f"-> {row['flag_reason']}"
            )
    lines.append("")
    lines.append("## Interpretation")
    lines.append("- A flagged item is not automatically wrong, but if a tract is both much smaller on disk and contains far fewer streamlines than the cohort, it is a strong failure signal.")
    lines.append("- These flags should be reviewed together with visual QC and missing-render checks.")
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--processed_root", required=True, help="Processed subject root, e.g. /path/SMU_processed")
    ap.add_argument("--out_dir", required=True, help="Output directory for cohort QC artifacts")
    args = ap.parse_args()

    processed_root = Path(args.processed_root).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    rows = collect_rows(processed_root)
    stats_rows, flags = summarize(rows)

    write_csv(
        out_dir / "tract_stats.csv",
        stats_rows,
        ["tract_file", "n_subjects", "size_median_bytes", "size_p10_bytes", "line_median", "line_p10"],
    )
    write_csv(
        out_dir / "tract_flags.csv",
        flags,
        [
            "subject",
            "tract_file",
            "tract_name",
            "file_path",
            "file_size_bytes",
            "num_lines",
            "cohort_size_median",
            "cohort_line_median",
            "flag_reason",
        ],
    )
    write_md(out_dir / "tract_cohort_qc.md", stats_rows, flags)
    print(str(out_dir / "tract_cohort_qc.md"))


if __name__ == "__main__":
    main()
