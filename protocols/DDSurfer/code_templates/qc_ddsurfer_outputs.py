#!/usr/bin/env python3
"""Check DDSurfer core outputs; anatomical and postprocessing QC remain separate."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

import nibabel as nib
import numpy as np
import trimesh


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check_outputs(subject, directory, dwi_path, predict_mode):
    report = {"subject": subject, "subject_dir": str(directory),
              "reference_dwi": str(dwi_path), "surfaces": {}, "scalars": {},
              "errors": [], "warnings": [],
              "validation_scope": "Core output integrity and geometry only; anatomy, "
                                  "registration, intersections and postprocessing not certified."}
    errors, warnings = report["errors"], report["warnings"]
    pipeline = json.loads((directory / "logs/pipeline.json").read_text())
    if pipeline.get("subject") != subject or pipeline.get("completed") is not True:
        errors.append("No matching completed reconstruction record")
    if pipeline.get("coordinate_space") != "native_scanner_RAS_mm":
        errors.append("Reconstruction record does not declare native scanner RAS mm")
    reference = nib.load(str(dwi_path))
    if len(reference.shape) != 4 or not np.isfinite(reference.affine).all():
        raise ValueError("Reference must be the actual 4D NIfTI DWI with finite geometry")
    inverse = np.linalg.inv(reference.affine)
    meshes = {}
    kinds = ("white", "pial") if predict_mode == "all" else ("white",)
    for hemi in ("lh", "rh"):
        for kind in kinds:
            name = f"{hemi}.{kind}"
            path = directory / "ddsurfer" / f"{name}.obj"
            try:
                record = pipeline["surfaces"][name]
                if record["file"] != f"ddsurfer/{name}.obj" or sha256(path) != record["sha256"]:
                    raise ValueError("Surface does not match its completion record")
                mesh = trimesh.load(str(path), process=False)
                if not isinstance(mesh, trimesh.Trimesh):
                    raise ValueError("Expected one triangular model")
                vertices, faces = np.asarray(mesh.vertices), np.asarray(mesh.faces)
                if (not len(vertices) or not len(faces) or not np.isfinite(vertices).all()
                        or faces.shape[1:] != (3,) or faces.min() < 0 or faces.max() >= len(vertices)):
                    raise ValueError("Empty, nonfinite or invalid triangular mesh")
                voxels = nib.affines.apply_affine(inverse, vertices)
                outside = np.any((voxels < -0.5) | (voxels > np.asarray(reference.shape[:3]) - 0.5), axis=1)
                report["surfaces"][name] = {
                    "path": str(path), "vertices": len(vertices), "triangles": len(faces),
                    "bounds_ras_mm": mesh.bounds.tolist(), "watertight": bool(mesh.is_watertight),
                    "winding_consistent": bool(mesh.is_winding_consistent),
                    "vertices_outside_dwi": int(outside.sum()),
                    "checkpoint": record.get("checkpoint"), "precision": record.get("precision"),
                }
                if outside.any() or not mesh.is_watertight or not mesh.is_winding_consistent:
                    warnings.append(f"{name}: review DWI bounds and surface topology")
                meshes[name] = mesh
            except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
                errors.append(f"{name}: {exc}")
        if predict_mode == "all" and all(f"{hemi}.{kind}" in meshes for kind in kinds):
            white, pial = meshes[f"{hemi}.white"], meshes[f"{hemi}.pial"]
            if white.vertices.shape != pial.vertices.shape or not np.array_equal(white.faces, pial.faces):
                errors.append(f"{hemi}: white/pial vertex correspondence or faces differ")

    dti = json.loads((directory / "logs/dti.json").read_text())
    geometry = dti["geometry"]
    if (tuple(geometry["shape"]) != reference.shape
            or not np.allclose(geometry["affine"], reference.affine, atol=1e-4, rtol=0)):
        errors.append("DTI export record does not match the supplied DWI geometry")
    if dti.get("normalized") is not False or dti.get("coordinate_space") != "native_scanner_RAS_mm":
        errors.append("DTI export record does not declare unnormalized native maps")
    for name in ("FA", "MD", "MinEigenvalue", "MidEigenvalue", "MaxEigenvalue", "Trace"):
        path = directory / "dti" / f"{subject}-{name}.nii.gz"
        if name == "Trace" and not path.exists() and name not in dti["scalar_maps"]:
            warnings.append("Trace absent; confirm this run intentionally reused a legacy cache")
            continue
        try:
            record = dti["scalar_maps"][name]
            if record["file"] != f"dti/{path.name}" or sha256(path) != record["sha256"]:
                raise ValueError("Scalar does not match its export record")
            image = nib.load(str(path))
            if image.shape != reference.shape[:3] or not np.allclose(image.affine, reference.affine, atol=1e-4, rtol=0):
                raise ValueError("Scalar grid/affine differs from native DWI")
            data = np.asarray(image.dataobj)
            if not np.isfinite(data).all():
                raise ValueError("Scalar contains nonfinite values")
            out_of_range = (data < 0) | (data > 1) if name == "FA" else data < 0
            report["scalars"][name] = {
                "path": str(path), "shape": list(image.shape), "min": float(data.min()),
                "max": float(data.max()), "nonzero_voxels": int(np.count_nonzero(data)),
                "out_of_range_voxels": int(out_of_range.sum()),
            }
            if out_of_range.any() or not np.any(data):
                warnings.append(f"{name}: review scalar range/content; counts include background voxels")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"{name}: {exc}")
    report["status"] = "failed" if errors else "warning" if warnings else "success"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--subject-dir", type=Path, required=True)
    parser.add_argument("--dwi", type=Path, required=True, help="Actual input DWI; only its header is read")
    parser.add_argument("--predict-mode", choices=("all", "wm"), default="all")
    parser.add_argument("--report", type=Path, required=True, help="Separate QC JSON output path")
    args = parser.parse_args()
    directory, dwi_path, destination = args.subject_dir.resolve(), args.dwi.resolve(), args.report.resolve()
    # A report must never overwrite an input, surface, scalar or pipeline receipt.
    if destination == dwi_path or directory == destination or directory in destination.parents:
        parser.error("Write --report outside --subject-dir and do not overwrite --dwi")
    try:
        report = check_outputs(args.subject, directory, dwi_path, args.predict_mode)
    except (OSError, ValueError, KeyError, TypeError, nib.filebasedimages.ImageFileError) as exc:
        report = {"subject": args.subject, "status": "failed", "errors": [str(exc)]}
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(json.dumps({"status": report["status"], "report": str(destination),
                      "errors": len(report.get("errors", [])), "warnings": len(report.get("warnings", []))}))
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
