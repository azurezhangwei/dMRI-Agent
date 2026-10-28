#!/usr/bin/env python3
import argparse
import csv
import json
import math
from pathlib import Path

import nibabel as nib
import numpy as np
import vtk


def parse_args():
    parser = argparse.ArgumentParser(
        description="Reference template for building a structural connectome matrix from "
        "a tractography polydata file and a dMRI-space label image."
    )
    parser.add_argument("--tracts", required=True, help="Input tractography (.vtk or .vtp)")
    parser.add_argument("--labels", required=True, help="dMRI-space parcellation label image")
    parser.add_argument("--out-dir", required=True, help="Output directory")
    parser.add_argument(
        "--exclude-label",
        type=int,
        nargs="*",
        default=[0],
        help="Label ids to exclude from node definition (default: 0 only).",
    )
    parser.add_argument(
        "--node-table",
        help="Optional fixed node table TSV with columns including "
        "node_index, fs_label, node_name, hemisphere, category, include.",
    )
    parser.add_argument(
        "--radial-mm",
        type=float,
        default=3.0,
        help="Fallback radial search radius in millimeters",
    )
    parser.add_argument(
        "--reverse-steps",
        type=int,
        default=12,
        help="How many streamline points to search backward from each endpoint",
    )
    return parser.parse_args()


def read_polydata(path_str):
    path = Path(path_str)
    suffix = path.suffix.lower()
    if suffix == ".vtp":
        reader = vtk.vtkXMLPolyDataReader()
    elif suffix == ".vtk":
        reader = vtk.vtkPolyDataReader()
    else:
        raise ValueError(f"Unsupported tractography extension: {suffix}")
    reader.SetFileName(str(path))
    reader.Update()
    polydata = reader.GetOutput()
    if polydata is None or polydata.GetNumberOfLines() == 0:
        raise RuntimeError(f"No streamlines found in {path}")
    return polydata


def extract_streamlines(polydata):
    points = polydata.GetPoints()
    lines = polydata.GetLines()
    id_list = vtk.vtkIdList()
    streamlines = []
    lines.InitTraversal()
    while lines.GetNextCell(id_list):
        streamline = [points.GetPoint(id_list.GetId(i)) for i in range(id_list.GetNumberOfIds())]
        if len(streamline) >= 2:
            streamlines.append(np.asarray(streamline, dtype=float))
    return streamlines


def world_to_voxel(point_xyz, inv_affine):
    xyz1 = np.asarray([point_xyz[0], point_xyz[1], point_xyz[2], 1.0], dtype=float)
    ijk = inv_affine @ xyz1
    return ijk[:3]


def voxel_lookup(label_data, ijk):
    idx = np.rint(ijk).astype(int)
    if np.any(idx < 0) or np.any(idx >= np.asarray(label_data.shape)):
        return 0
    return int(label_data[idx[0], idx[1], idx[2]])


def radial_lookup(label_data, ijk, voxel_sizes, radius_mm, excluded_labels):
    radius_vox = np.maximum(1, np.ceil(radius_mm / voxel_sizes).astype(int))
    center = np.rint(ijk).astype(int)
    best = None
    best_dist = None
    for dx in range(-radius_vox[0], radius_vox[0] + 1):
        for dy in range(-radius_vox[1], radius_vox[1] + 1):
            for dz in range(-radius_vox[2], radius_vox[2] + 1):
                cand = center + np.asarray([dx, dy, dz], dtype=int)
                if np.any(cand < 0) or np.any(cand >= np.asarray(label_data.shape)):
                    continue
                label = int(label_data[cand[0], cand[1], cand[2]])
                if label in excluded_labels:
                    continue
                dist_mm = math.sqrt(
                    (dx * voxel_sizes[0]) ** 2
                    + (dy * voxel_sizes[1]) ** 2
                    + (dz * voxel_sizes[2]) ** 2
                )
                if dist_mm <= radius_mm and (best_dist is None or dist_mm < best_dist):
                    best = label
                    best_dist = dist_mm
    return best if best is not None else 0


def endpoint_label(streamline, label_data, inv_affine, voxel_sizes, radius_mm, excluded_labels, reverse_steps):
    endpoints = (0, -1)
    labels = []
    for endpoint_idx in endpoints:
        point = streamline[endpoint_idx]
        ijk = world_to_voxel(point, inv_affine)
        label = voxel_lookup(label_data, ijk)
        if label in excluded_labels:
            label = radial_lookup(label_data, ijk, voxel_sizes, radius_mm, excluded_labels)
        if label in excluded_labels:
            if endpoint_idx == 0:
                search_points = streamline[1 : min(len(streamline), reverse_steps + 1)]
            else:
                search_points = streamline[max(0, len(streamline) - reverse_steps - 1) : -1][::-1]
            for fallback_point in search_points:
                fallback_ijk = world_to_voxel(fallback_point, inv_affine)
                label = voxel_lookup(label_data, fallback_ijk)
                if label not in excluded_labels:
                    break
        labels.append(0 if label in excluded_labels else int(label))
    return labels[0], labels[1]


def streamline_length(streamline):
    diffs = np.diff(streamline, axis=0)
    return float(np.linalg.norm(diffs, axis=1).sum())


def write_matrix(path, matrix):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(matrix)


def load_node_table(path):
    rows = []
    with open(path, "r", newline="") as f:
        lines = [line.rstrip("\n") for line in f]
    nonempty = [line for line in lines if line.strip()]
    if nonempty and nonempty[0].lstrip().startswith("node_index"):
        delimiter = "," if "," in nonempty[0] else "\t"
        reader = csv.DictReader(nonempty, delimiter=delimiter)
        for row in reader:
            include_raw = str(row.get("include", "1")).strip().lower()
            include = include_raw not in {"0", "false", "no", "n"}
            if not include:
                continue
            rows.append(
                {
                    "node_index": int(row["node_index"]),
                    "fs_label": int(row["fs_label"]),
                    "node_name": row.get("node_name", ""),
                    "hemisphere": row.get("hemisphere", ""),
                    "category": row.get("category", ""),
                    "include": 1,
                }
            )
    else:
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if len(parts) < 5:
                raise ValueError(f"Invalid node table line: {line}")
            include = 1
            if len(parts) >= 6:
                include_raw = parts[5].strip().lower()
                include = 0 if include_raw in {"0", "false", "no", "n"} else 1
            if not include:
                continue
            rows.append(
                {
                    "node_index": int(parts[0]),
                    "fs_label": int(parts[1]),
                    "node_name": parts[2],
                    "hemisphere": parts[3],
                    "category": parts[4],
                    "include": 1,
                }
            )
    rows.sort(key=lambda x: x["node_index"])
    return rows


def main():
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    label_img = nib.load(args.labels)
    label_data = np.asarray(label_img.dataobj, dtype=np.int32)
    affine = label_img.affine
    inv_affine = np.linalg.inv(affine)
    voxel_sizes = np.asarray(label_img.header.get_zooms()[:3], dtype=float)
    excluded_labels = set(args.exclude_label)

    if args.node_table:
        node_rows = load_node_table(args.node_table)
        node_map = {row["fs_label"]: row["node_index"] - 1 for row in node_rows}
    else:
        labels_present = sorted(int(x) for x in np.unique(label_data) if int(x) not in excluded_labels)
        node_rows = [
            {
                "node_index": idx + 1,
                "fs_label": label,
                "node_name": f"label_{label}",
                "hemisphere": "",
                "category": "",
                "include": 1,
            }
            for idx, label in enumerate(labels_present)
        ]
        node_map = {row["fs_label"]: row["node_index"] - 1 for row in node_rows}
    node_count = len(node_rows)

    count_matrix = np.zeros((node_count, node_count), dtype=np.int64)
    length_sum_matrix = np.zeros((node_count, node_count), dtype=np.float64)

    streamlines = extract_streamlines(read_polydata(args.tracts))
    assigned = 0
    unassigned = 0

    for streamline in streamlines:
        label_a, label_b = endpoint_label(
            streamline,
            label_data,
            inv_affine,
            voxel_sizes,
            args.radial_mm,
            excluded_labels,
            args.reverse_steps,
        )
        if label_a == 0 or label_b == 0 or label_a not in node_map or label_b not in node_map:
            unassigned += 1
            continue

        idx_a = node_map[label_a]
        idx_b = node_map[label_b]
        length = streamline_length(streamline)

        count_matrix[idx_a, idx_b] += 1
        length_sum_matrix[idx_a, idx_b] += length
        if idx_a != idx_b:
            count_matrix[idx_b, idx_a] += 1
            length_sum_matrix[idx_b, idx_a] += length
        assigned += 1

    with np.errstate(divide="ignore", invalid="ignore"):
        length_mean_matrix = np.divide(
            length_sum_matrix,
            count_matrix,
            out=np.zeros_like(length_sum_matrix),
            where=count_matrix > 0,
        )

    write_matrix(out_dir / "connectome_count.csv", count_matrix)
    write_matrix(out_dir / "connectome_length_mean.csv", np.round(length_mean_matrix, 6))

    with open(out_dir / "connectome_node_table.tsv", "w", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(["node_index", "fs_label", "node_name", "hemisphere", "category", "include"])
        for row in node_rows:
            writer.writerow(
                [
                    row["node_index"],
                    row["fs_label"],
                    row["node_name"],
                    row["hemisphere"],
                    row["category"],
                    row["include"],
                ]
            )

    summary = {
        "tracts": str(Path(args.tracts).resolve()),
        "labels": str(Path(args.labels).resolve()),
        "node_table": str(Path(args.node_table).resolve()) if args.node_table else None,
        "total_streamlines": len(streamlines),
        "assigned_streamlines": assigned,
        "unassigned_streamlines": unassigned,
        "assigned_rate": (assigned / len(streamlines)) if streamlines else 0.0,
        "node_count": node_count,
        "nonzero_edges": int(np.count_nonzero(np.triu(count_matrix, k=1))),
        "symmetric": bool(np.array_equal(count_matrix, count_matrix.T)),
        "radial_mm": args.radial_mm,
        "reverse_steps": args.reverse_steps,
    }
    with open(out_dir / "assignment_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
