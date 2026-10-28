#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VLM-based visual QC for tract segmentation renders.

- Rule-based hard failures first: missing expected renders => FAIL
- Existing images are then sent in ONE VLM request
- Output: Markdown QC report saved into the image folder (default)

Usage:
  python vlm_tract_qc.py \
    --images /path/CST_posterior.png /path/AF_left_left.png /path/CC4_anterior.png

Optional:
  python vlm_tract_qc.py \
    --images /path/*.png \
    --expected_spec "CST:posterior,AF_left:left,CC4:anterior" \
    --out_md /path/qc_vlm_report.md
"""

import argparse
import base64
import os
from pathlib import Path
from typing import List, Dict, Tuple

from openai import OpenAI


# ------------------------- helpers -------------------------

def to_data_url(img_path: Path) -> str:
    suffix = img_path.suffix.lower()
    if suffix in [".png"]:
        mime = "image/png"
    elif suffix in [".jpg", ".jpeg"]:
        mime = "image/jpeg"
    elif suffix in [".webp"]:
        mime = "image/webp"
    else:
        # fallback
        mime = "application/octet-stream"

    data = img_path.read_bytes()
    b64 = base64.b64encode(data).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def infer_tract_and_view(img_path: Path) -> Tuple[str, str]:
    """
    Infer tract + view from filename, assuming your render naming:
      <TRACT>_<VIEW>.png  e.g. CST_posterior.png
      AF_left_left.png (tract=AF_left, view=left)
      CC4_anterior.png

    If cannot infer, returns ("UNKNOWN", "UNKNOWN").
    """
    stem = img_path.stem  # no extension
    parts = stem.split("_")
    if len(parts) < 2:
        return "UNKNOWN", "UNKNOWN"

    view = parts[-1].lower()
    tract = "_".join(parts[:-1])

    # normalize view tokens
    view_map = {
        "ant": "anterior",
        "post": "posterior",
        "sup": "superior",
        "inf": "inferior",
        "iso": "iso",
        "left": "left",
        "right": "right",
        "anterior": "anterior",
        "posterior": "posterior",
        "superior": "superior",
        "inferior": "inferior",
    }
    view = view_map.get(view, view)

    # light tract cleanup
    tract = tract.strip()
    if not tract:
        tract = "UNKNOWN"

    return tract, view


def parse_expected_spec(spec_str: str) -> List[Tuple[str, str]]:
    items: List[Tuple[str, str]] = []
    for chunk in [c.strip() for c in spec_str.split(",") if c.strip()]:
        if ":" not in chunk:
            raise ValueError(f"Bad expected_spec item '{chunk}'. Expected 'TRACT:VIEW'.")
        tract, view = [x.strip() for x in chunk.split(":", 1)]
        items.append((tract, view))
    return items


def build_vlm_prompt(items: List[Dict]) -> str:
    """
    Prompt tailored for tract visual QC, knowing tract name + view.
    """
    # concise per-image hints
    per_img = []
    for i, it in enumerate(items, start=1):
        per_img.append(
            f"- Image {i}: tract = {it['tract']} ; view = {it['view']} ; file = {it['path'].name}"
        )
    per_img_txt = "\n".join(per_img)

    prompt = f"""
You are a visual quality-control assistant for diffusion MRI tract renderings. You will receive several images, with tract names and views provided in advance.

This task has one simple and stable goal: distinguish “bundles with normal quantities” from “clearly sparse/nearly empty bundles”.

Remember these principles:
1. The most important criteria are streamline quantity and the image area occupied.
2. If many streamlines form a clear bundle and occupy a substantial part of the image, preferentially consider it normal.
3. Flag a problem only when there are clearly too few streamlines, only a few lines remain, only local fragments remain, or the overall occupied area is very small.
4. For bundles such as AF and CC4 that may naturally appear spread out, fan-shaped, or less compact, do not judge them incorrect merely because they are “widely spread” or “not very compact”.
5. For major bundles such as CST, if only one or very few lines remain, assign FAIL directly.
6. You do not need to determine fine anatomical correctness. Trajectory, smoothness, and local shape may be supplementary notes only and must not drive the conclusion.

Known information:
{per_img_txt}

Compare across images first, then assess each image separately.

Output only one of these three Quantity levels:
- NORMAL_BUNDLE: clearly many streamlines forming a visible bundle that occupies a large image area
- REDUCED_BUNDLE: fewer streamlines; the bundle remains visible but is clearly sparser than normal
- NEAR_EMPTY: very few lines, local fragments, nearly a single line, or almost no visible bundle overall

Strictly follow these Verdict rules:
- PASS: Quantity level = NORMAL_BUNDLE
- WARNING: Quantity level = REDUCED_BUNDLE
- FAIL: Quantity level = NEAR_EMPTY

When writing the output:
1. Cross-tract comparison must compare only quantity and occupied area; do not discuss anatomy excessively.
2. Reason must mainly address “whether quantity is clearly insufficient, whether the bundle is nearly empty, whether only a few lines remain, or whether the overall occupied area is very small”.
3. Optional notes may add supplementary observations such as “relatively divergent/spread out/a few outlier lines”, but these notes must not overturn the main quantity-based conclusion.

Required output format (must be Markdown):
# Tract Visual QC Report
## Cross-tract Comparison
- ...
- ...

## Summary Table
| Image | Tract | View | Quantity level | Verdict | Reason |
|---|---|---|---|---|---|
(Fill in a row for every provided image)

Then provide the following for each image:
## Image N: <tract> (<view>)
- Quantity level: NORMAL_BUNDLE / REDUCED_BUNDLE / NEAR_EMPTY
- Verdict: PASS / WARNING / FAIL
- Core reason:
  - ...
- Optional notes:
  - ...
- Suggestions:
  1. ...
  2. ...

Notes:
- Do not output “I cannot see the images”.
- Do not discuss the reasoning process.
""".strip()

    return prompt


def write_markdown_report(
    out_md: Path,
    items: List[Dict],
    vlm_md: str,
    expected_items: List[Tuple[str, str]],
    missing_items: List[Tuple[str, str]],
) -> Path:
    """
    Save a markdown report with embedded images + VLM response.
    Use relative paths to keep the report portable.
    """
    out_dir = out_md.parent
    lines = []
    lines.append("# VLM Tract Visual QC (Auto Report)")
    lines.append("")
    lines.append("## Inputs")
    for i, it in enumerate(items, start=1):
        rel = os.path.relpath(it["path"], out_dir)
        lines.append(f"### Image {i}: {it['tract']} ({it['view']})")
        lines.append(f"- File: `{rel}`")
        lines.append(f"![Image {i} - {it['tract']} ({it['view']})]({rel})")
        lines.append("")
    if expected_items:
        lines.append("## Expected Tracts")
        for tract, view in expected_items:
            lines.append(f"- `{tract}` ({view})")
        lines.append("")
    lines.append("## Rule-Based QC")
    if missing_items:
        lines.append("The following expected renders are missing and must be treated as **FAIL** before any VLM interpretation:")
        for tract, view in missing_items:
            lines.append(f"- FAIL: missing render for `{tract}` ({view})")
    else:
        lines.append("- No expected renders are missing.")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## VLM QC Report")
    lines.append("")
    if vlm_md.strip():
        lines.append(vlm_md.strip())
    else:
        lines.append("_No VLM evaluation was run because no valid images were available._")
    lines.append("")

    out_md.write_text("\n".join(lines), encoding="utf-8")
    return out_md


# ------------------------- main -------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", type=str, nargs="+", required=True,
                    help="Image paths (png/jpg/webp).")
    ap.add_argument("--expected_spec", type=str, default="CST:posterior,AF_left:left,CC4:anterior",
                    help="Expected tract/view list used for hard-fail checks.")
    ap.add_argument("--out_md", type=str, default="",
                    help="Output markdown path. If empty, saved to <image_folder>/qc_vlm_report.md")
    ap.add_argument("--model", type=str, default="qwen3-vl-plus",
                    help="VLM model name (default: qwen3-vl-plus)")
    ap.add_argument("--stream", action="store_true",
                    help="Use streaming output (prints chunks). Non-stream is simpler for pipelines.")
    args = ap.parse_args()

    img_paths = [Path(p).expanduser().resolve() for p in args.images]
    expected_items = parse_expected_spec(args.expected_spec)

    # Validate
    valid_img_paths: List[Path] = []
    for p in img_paths:
        if not p.exists():
            continue
        if p.suffix.lower() not in [".png", ".jpg", ".jpeg", ".webp"]:
            continue
        valid_img_paths.append(p)

    if not valid_img_paths and not expected_items:
        raise SystemExit("ERROR: No valid images and no expected_spec.")

    # Output md path
    out_dir = valid_img_paths[0].parent if valid_img_paths else Path.cwd()
    out_md = Path(args.out_md).expanduser().resolve() if args.out_md else (out_dir / "qc_vlm_report.md")

    # Build items
    items: List[Dict] = []
    seen_pairs = set()
    for p in valid_img_paths:
        tract, view = infer_tract_and_view(p)
        items.append({"path": p, "tract": tract, "view": view, "data_url": to_data_url(p)})
        seen_pairs.add((tract, view))

    missing_items = [(tract, view) for tract, view in expected_items if (tract, view) not in seen_pairs]

    answer_content = ""
    if items:
        prompt = build_vlm_prompt(items)
        user_content = []
        for it in items:
            user_content.append({
                "type": "image_url",
                "image_url": {"url": it["data_url"]}
            })
        user_content.append({"type": "text", "text": prompt})

        client = OpenAI(
            api_key=os.getenv("DASHSCOPE_API_KEY"),
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

        if args.stream:
            completion = client.chat.completions.create(
                model=args.model,
                messages=[{"role": "user", "content": user_content}],
                stream=True,
                extra_body={
                    "enable_thinking": False
                },
            )
            for chunk in completion:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if getattr(delta, "content", None):
                    print(delta.content, end="", flush=True)
                    answer_content += delta.content
            print("")
        else:
            completion = client.chat.completions.create(
                model=args.model,
                messages=[{"role": "user", "content": user_content}],
                stream=False,
                extra_body={
                    "enable_thinking": False
                },
            )
            answer_content = completion.choices[0].message.content or ""

    # Save report
    out_md = write_markdown_report(out_md, items, answer_content, expected_items, missing_items)

    # Return path (for pipeline)
    print(str(out_md))


if __name__ == "__main__":
    main()
