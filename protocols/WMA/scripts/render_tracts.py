#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Render selected *.vtp tracts to 2D PNGs with standardized camera views.
Supports Slicer-like direction-based coloring (RGB = abs(normalized tangent)).

Example:
  python render_tracts.py \
    --in_dir /workspace/processed/sub_03296/dwi/wma/sub_03296_dwi_tractography/AnatomicalTracts \
    --out_dir ./visual/sub_03296 \
    --spec "CST:posterior,AF_left:left,CC4:anterior" \
    --img_size 1000 1000 --bg 0 0 0 --line_width 4 \
    --color_mode direction

Notes:
- Default spec matches your request.
- View presets assume RAS-like axes: +X=right, +Y=anterior, +Z=superior.
- Headless: uses offscreen rendering. To suppress VTK warnings, use --quiet.
"""

import argparse
import json
import math
import os
from pathlib import Path
from typing import List, Tuple

# ------------------------- Spec parsing & normalization -------------------------

VIEW_PRESETS = {"anterior", "posterior", "left", "right", "superior", "inferior", "iso"}

# Tract name normalization -> filename patterns in your folder
TRACT_PATTERNS = {
    "CST": ["T_CST_left.vtp", "T_CST_right.vtp"],   # default: both sides if present
    "AF_left": ["T_AF_left.vtp"],
    "AF_right": ["T_AF_right.vtp"],
    "AF": ["T_AF_left.vtp", "T_AF_right.vtp"],      # optional: both
    "CC4": ["T_CC4.vtp"],
}

DEFAULT_SPEC = "CST:posterior,AF_left:left,CC4:anterior"


def parse_spec(spec_str: str) -> List[Tuple[str, str]]:
    """
    spec format: "CST:posterior,AF_left:left,CC4:anterior"
    returns list of (tract_key, view_key)
    """
    items: List[Tuple[str, str]] = []
    for chunk in [c.strip() for c in spec_str.split(",") if c.strip()]:
        if ":" not in chunk:
            raise ValueError(f"Bad spec item '{chunk}'. Expected 'TRACT:VIEW'.")
        tract, view = [x.strip() for x in chunk.split(":", 1)]
        view_l = view.lower()
        if view_l not in VIEW_PRESETS:
            raise ValueError(f"Unknown view '{view}'. Allowed: {sorted(VIEW_PRESETS)}")
        items.append((tract, view_l))
    return items


def resolve_files(in_dir: Path, tract_key: str) -> List[Path]:
    if tract_key not in TRACT_PATTERNS:
        raise KeyError(
            f"Unknown tract key '{tract_key}'. "
            f"Known keys: {sorted(TRACT_PATTERNS.keys())}\n"
            f"Tip: add it in TRACT_PATTERNS."
        )
    files: List[Path] = []
    for fname in TRACT_PATTERNS[tract_key]:
        p = in_dir / fname
        if p.exists():
            files.append(p)
    return files


# ------------------------- VTK rendering (no pyvista dependency) -------------------------

def _vtk_imports():
    import vtk  # type: ignore
    return vtk


def read_polydata(vtk, filename: str):
    reader = vtk.vtkXMLPolyDataReader()
    reader.SetFileName(filename)
    reader.Update()
    return reader.GetOutput()


def polydata_bounds(polydata) -> Tuple[float, float, float, float, float, float]:
    b = polydata.GetBounds()  # (xmin,xmax,ymin,ymax,zmin,zmax)
    return float(b[0]), float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5])


def combine_bounds(bounds_list: List[Tuple[float, float, float, float, float, float]]):
    xs = [b[0] for b in bounds_list] + [b[1] for b in bounds_list]
    ys = [b[2] for b in bounds_list] + [b[3] for b in bounds_list]
    zs = [b[4] for b in bounds_list] + [b[5] for b in bounds_list]
    return (min(xs), max(xs), min(ys), max(ys), min(zs), max(zs))


def add_direction_coloring_rgb(vtk, polydata, array_name="RGB"):
    """
    Add per-point RGB (unsigned char) based on local tangent direction.
    RGB = abs(normalized tangent) * 255, tangent estimated along each polyline.

    This matches common tract direction coloring (Slicer-like):
      X (L-R) -> Red, Y (P-A) -> Green, Z (I-S) -> Blue.
    """
    npts = polydata.GetNumberOfPoints()
    rgb = vtk.vtkUnsignedCharArray()
    rgb.SetName(array_name)
    rgb.SetNumberOfComponents(3)
    rgb.SetNumberOfTuples(npts)

    # default white (in case some points aren't visited)
    for pid in range(npts):
        rgb.SetTuple3(pid, 255, 255, 255)

    pts = polydata.GetPoints()
    lines = polydata.GetLines()

    if pts is None or lines is None:
        polydata.GetPointData().SetScalars(rgb)
        polydata.GetPointData().SetActiveScalars(array_name)
        return polydata

    id_list = vtk.vtkIdList()
    lines.InitTraversal()

    while lines.GetNextCell(id_list):
        n = id_list.GetNumberOfIds()
        if n < 2:
            continue

        for i in range(n):
            pid = id_list.GetId(i)

            # pick neighbors to compute tangent
            if i == 0:
                pid0, pid1 = id_list.GetId(0), id_list.GetId(1)
            elif i == n - 1:
                pid0, pid1 = id_list.GetId(n - 2), id_list.GetId(n - 1)
            else:
                pid0, pid1 = id_list.GetId(i - 1), id_list.GetId(i + 1)

            x0, y0, z0 = pts.GetPoint(pid0)
            x1, y1, z1 = pts.GetPoint(pid1)

            dx, dy, dz = (x1 - x0), (y1 - y0), (z1 - z0)
            norm = (dx * dx + dy * dy + dz * dz) ** 0.5

            if norm < 1e-12:
                r, g, b = 255, 255, 255
            else:
                dx, dy, dz = dx / norm, dy / norm, dz / norm
                r = int(min(255, max(0, abs(dx) * 255)))
                g = int(min(255, max(0, abs(dy) * 255)))
                b = int(min(255, max(0, abs(dz) * 255)))

            rgb.SetTuple3(pid, r, g, b)

    polydata.GetPointData().SetScalars(rgb)
    polydata.GetPointData().SetActiveScalars(array_name)
    return polydata


def make_actor(
    vtk,
    polydata,
    color_rgb: Tuple[float, float, float],
    line_width: float,
    render_as_tubes: bool,
    color_mode: str,
):
    """
    color_mode:
      - "solid": use actor property color
      - "direction": use per-point RGB scalars (Slicer-like)
    """
    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputData(polydata)

    actor = vtk.vtkActor()
    actor.SetMapper(mapper)

    prop = actor.GetProperty()
    prop.SetLineWidth(line_width)
    prop.SetOpacity(1.0)
    prop.SetRenderLinesAsTubes(bool(render_as_tubes))

    if color_mode == "direction":
        polydata = add_direction_coloring_rgb(vtk, polydata, array_name="RGB")
        mapper.SetInputData(polydata)
        mapper.SetColorModeToDirectScalars()
        mapper.ScalarVisibilityOn()
    else:
        mapper.ScalarVisibilityOff()
        prop.SetColor(*color_rgb)

    return actor


def set_camera_for_view(camera, view: str, center: Tuple[float, float, float], radius: float):
    """
    Standardized anatomical camera presets (RAS-like axes):
      +X = right, +Y = anterior, +Z = superior
    """
    cx, cy, cz = center
    dist = radius * 2.6

    camera.SetFocalPoint(cx, cy, cz)

    if view == "anterior":
        camera.SetPosition(cx, cy + dist, cz)
        camera.SetViewUp(0, 0, 1)
    elif view == "posterior":
        camera.SetPosition(cx, cy - dist, cz)
        camera.SetViewUp(0, 0, 1)
    elif view == "left":
        camera.SetPosition(cx - dist, cy, cz)
        camera.SetViewUp(0, 0, 1)
    elif view == "right":
        camera.SetPosition(cx + dist, cy, cz)
        camera.SetViewUp(0, 0, 1)
    elif view == "superior":
        camera.SetPosition(cx, cy, cz + dist)
        camera.SetViewUp(0, 1, 0)
    elif view == "inferior":
        camera.SetPosition(cx, cy, cz - dist)
        camera.SetViewUp(0, 1, 0)
    elif view == "iso":
        camera.SetPosition(cx + dist, cy + dist, cz + dist)
        camera.SetViewUp(0, 0, 1)
    else:
        raise ValueError(f"Unknown view: {view}")

    camera.SetParallelProjection(True)


def render_to_png(
    vtp_files: List[Path],
    out_png: Path,
    view: str,
    img_size: Tuple[int, int],
    bg_rgb: Tuple[float, float, float],
    line_width: float,
    colors: List[Tuple[float, float, float]],
    add_axes: bool,
    render_as_tubes: bool,
    color_mode: str,
):
    vtk = _vtk_imports()

    renderer = vtk.vtkRenderer()
    renderer.SetBackground(*bg_rgb)

    renwin = vtk.vtkRenderWindow()
    renwin.AddRenderer(renderer)
    renwin.SetSize(img_size[0], img_size[1])

    # Offscreen + avoid alpha (prevents "transparent background" surprises)
    renwin.OffScreenRenderingOn()
    renwin.SetAlphaBitPlanes(0)
    renwin.SetMultiSamples(8)

    # Add actors & compute combined bounds
    bounds_list: List[Tuple[float, float, float, float, float, float]] = []
    for i, f in enumerate(vtp_files):
        poly = read_polydata(vtk, str(f))
        if poly is None or poly.GetNumberOfPoints() == 0:
            continue

        bounds_list.append(polydata_bounds(poly))
        color = colors[i % len(colors)]
        actor = make_actor(
            vtk=vtk,
            polydata=poly,
            color_rgb=color,
            line_width=line_width,
            render_as_tubes=render_as_tubes,
            color_mode=color_mode,
        )
        renderer.AddActor(actor)

    if not bounds_list:
        raise RuntimeError(f"No valid polydata to render for: {vtp_files}")

    xmin, xmax, ymin, ymax, zmin, zmax = combine_bounds(bounds_list)
    cx, cy, cz = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0, (zmin + zmax) / 2.0
    dx, dy, dz = (xmax - xmin), (ymax - ymin), (zmax - zmin)
    radius = 0.5 * math.sqrt(dx * dx + dy * dy + dz * dz)
    radius = max(radius, 1e-6)

    camera = renderer.GetActiveCamera()
    set_camera_for_view(camera, view, (cx, cy, cz), radius)

    # Parallel scale: fit the largest extent
    max_extent = max(dx, dy, dz)
    camera.SetParallelScale(max_extent * 0.55)

    if add_axes:
        axes = vtk.vtkAxesActor()
        axes.SetTotalLength(radius * 0.6, radius * 0.6, radius * 0.6)
        interactor = vtk.vtkRenderWindowInteractor()
        interactor.SetRenderWindow(renwin)
        interactor.Initialize()
        widget = vtk.vtkOrientationMarkerWidget()
        widget.SetOrientationMarker(axes)
        widget.SetInteractor(interactor)
        widget.SetViewport(0.0, 0.0, 0.2, 0.2)
        widget.SetEnabled(True)
        widget.InteractiveOff()

    renderer.ResetCameraClippingRange()
    renwin.Render()

    # Capture as RGB (no alpha => no transparency)
    w2i = vtk.vtkWindowToImageFilter()
    w2i.SetInput(renwin)
    w2i.SetScale(1)
    w2i.SetInputBufferTypeToRGB()
    w2i.ReadFrontBufferOff()
    w2i.Update()

    writer = vtk.vtkPNGWriter()
    writer.SetFileName(str(out_png))
    writer.SetInputConnection(w2i.GetOutputPort())
    writer.Write()


# ------------------------- CLI -------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--in_dir",
        type=str,
        required=True,
        help="Input folder containing *.vtp (e.g., .../AnatomicalTracts)",
    )
    ap.add_argument("--out_dir", type=str, required=True, help="Output folder for PNGs")
    ap.add_argument("--spec", type=str, default=DEFAULT_SPEC,
                    help=f"Render spec: 'TRACT:VIEW,...' (default: {DEFAULT_SPEC})")
    ap.add_argument("--img_size", type=int, nargs=2, default=[1400, 1000], help="PNG size: W H")
    ap.add_argument("--bg", type=float, nargs=3, default=[0.0, 0.0, 0.0],
                    help="Background RGB in [0,1]")
    ap.add_argument("--line_width", type=float, default=4.0, help="Line width")
    ap.add_argument("--render_as_tubes", action="store_true",
                    help="Render lines as tubes (visual nicer; not required)")
    ap.add_argument("--add_axes", action="store_true",
                    help="Add small orientation axes widget")
    ap.add_argument("--color_mode", type=str, default="direction",
                    choices=["solid", "direction"],
                    help="Coloring: solid or direction (Slicer-like direction coloring)")
    ap.add_argument("--colors", type=str, default="[[1,1,1],[0.2,0.8,1],[1,0.6,0.2]]",
                    help="(solid mode) JSON list of RGB colors, e.g. '[[1,1,1],[1,0,0]]'")
    ap.add_argument("--quiet", action="store_true",
                    help="Suppress VTK warnings (useful for headless logs)")
    ap.add_argument("--dry_run", action="store_true",
                    help="Only print resolved files and planned outputs")
    args = ap.parse_args()

    # Headless-friendly env
    os.environ.setdefault("DISPLAY", "")

    vtk = _vtk_imports()
    if args.quiet:
        vtk.vtkObject.GlobalWarningDisplayOff()

    in_dir = Path(args.in_dir).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    colors = json.loads(args.colors)
    colors_t = [tuple(map(float, c)) for c in colors]

    spec = parse_spec(args.spec)

    planned = []
    for tract_key, view in spec:
        files = resolve_files(in_dir, tract_key)
        if not files:
            print(f"[WARN] No files found for tract '{tract_key}' in {in_dir}")
            continue
        out_png = out_dir / f"{tract_key}_{view}.png"
        planned.append((tract_key, view, files, out_png))

    if args.dry_run:
        for tract_key, view, files, out_png in planned:
            print(f"{tract_key}:{view}")
            for f in files:
                print(f"  - {f.name}")
            print(f"  -> {out_png}")
        return

    for tract_key, view, files, out_png in planned:
        print(f"[INFO] Rendering {tract_key}:{view} -> {out_png.name}")
        render_to_png(
            vtp_files=files,
            out_png=out_png,
            view=view,
            img_size=(int(args.img_size[0]), int(args.img_size[1])),
            bg_rgb=(float(args.bg[0]), float(args.bg[1]), float(args.bg[2])),
            line_width=float(args.line_width),
            colors=colors_t,
            add_axes=bool(args.add_axes),
            render_as_tubes=bool(args.render_as_tubes),
            color_mode=str(args.color_mode),
        )

    print(f"[DONE] Saved {len(planned)} PNG(s) to: {out_dir}")


if __name__ == "__main__":
    main()
