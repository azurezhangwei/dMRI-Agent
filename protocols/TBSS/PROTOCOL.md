---
name: Tract-Based Spatial Statistics (TBSS) Pipeline
description: >
  Use this protocol when the user asks to perform FSL TBSS analysis on diffusion
  MRI data, including DWI input discovery, tensor-derived FA generation, TBSS
  group-space skeleton projection, optional non-FA metric projection, and
  optional voxelwise statistics when valid design files are provided.
version: 1.0.0
type: pipeline
---

# TBSS Diffusion Metric Skeleton Statistics Pipeline Guide

## Scope

For FSL TBSS FA skeleton analysis, optional non-FA projection, and statistics when valid design files are available. Specific algorithm boundaries are governed by the original sections.

## Input Contract

Dataset paths, processing units, DWI/gradients/masks, and optional design files must satisfy the original “General Rules” and input check sections; do not combine data across subjects.

## Output Contract

FA for each processing unit, the TBSS working directory, skeleton projections, optional statistical results, and summaries follow the original protocol's paths and naming rules.

## Workflow

The subsequent numbered sections are the authoritative stage order; this index does not change the existing choices of `dtifit`, `tbss_1_preproc` through `tbss_4_prestats`, or `randomise`.

## Quality Control

Perform the original protocol's input, FA, registration, skeleton, and statistical design QC; do not continue group-level analysis if too few valid processing units remain.

## Failure Boundaries

If input counts, software, design files, or key artifacts do not meet requirements, stop the affected stage and report evidence. Do not fabricate statistics or silently exclude samples.

## Privacy Boundaries

Keep only necessary paths, processing status, and aggregate QC in logs, memory, and reports, without identifiable metadata or raw image contents.

## Test Contract

Protocol acceptance uses a very small representative set to check input discovery, command construction, and lightweight QC; full registration, `randomise`, or large-batch testing requires an explicit user request.

This guide covers an **FSL TBSS (Tract-Based Spatial Statistics) pipeline**. Its goal is to generate an FA map for each processing unit from a batch of dMRI data, perform TBSS standard-space registration, construct the mean FA skeleton, perform FA skeleton projection, and run `randomise` when the user provides a valid statistical design.

This protocol describes only the workflow, algorithm choices, input/output constraints, and QC requirements; software installation locations, environment variable paths, and specific initialization methods are provided by the `RUNTIME_ENV` protocol. **This protocol must not depend on or write any fixed software installation paths**.

The main workflow based on official guidance is to generate FA for each subject from dMRI first, then run `tbss_1_preproc`, `tbss_2_reg`, `tbss_3_postreg`, and `tbss_4_prestats`, and finally use `randomise` for voxelwise statistics on skeletonised FA when a design matrix and contrast file are available.

Recommended execution granularity: this pipeline is a medium-length workflow with clear stage dependencies. To reduce unnecessary planner/executor round trips and repeated context, the planner is recommended to hand the complete processing task to the executor **in one pass**: the same executor should continuously complete input discovery, DWI QC, mask/`dtifit`, FA QC, the TBSS input list, `tbss_1_preproc`, `tbss_2_reg`, `tbss_3_postreg`, `tbss_4_prestats`, optional non-FA projection, optional statistics, and final QC/summary. The executor must record stage status and QC within each stage itself; if too few valid FA maps remain, inputs have widespread errors, or safe continuation is impossible, stop subsequent group-level processing and explain in the summary, instead of returning to the planner to repeatedly split tasks.

---

## 0. General Rules (Mandatory Constraints)

- Strictly follow this protocol's algorithm choices and stage order; do not switch to a non-TBSS voxelwise analysis workflow on your own.
- First identify the “dataset path” `<dataset_root>` from the user request.
- If the user, task description, or planner explicitly specifies `processed_root` / `code_root` / `qc_root` / `tbss_root`,
  use those explicit paths unconditionally.
- If the user explicitly specifies only `processed_root`, without `code_root` / `qc_root`, prefer placing
  `code_root` and `qc_root` in the current task workspace as siblings of `processed_root`;
  do not write code or QC outputs inside `dataset/` simply because the input path is under `dataset/`.
- Use the default rules only when no output root is explicitly specified:
  - `<data_folder> = dirname(<dataset_root>)`
  - `<dataset_name> = basename(<dataset_root>)`
  - `<processed_root> = <data_folder>/<dataset_name>_processed`
  - `<code_root> = <data_folder>/<dataset_name>_processing_code`
  - `<qc_root> = <data_folder>/<dataset_name>_processing_qc`
  - `<tbss_root> = <processed_root>/TBSS`
- Do not write outputs back inside the input `dataset/` unless the user explicitly requests it.
- Do not modify original DWI, bval, bvec, JSON, or existing manual results.
- Do not reorder, flip, replace, or “repair” bvec/bval on your own to make the workflow run; if gradient information appears abnormal, record evidence and report it in the summary.
- Because data may have unknown corruption or other errors, some data may fail due to their own issues once correct algorithm calls have been confirmed and other data in the same batch can be processed successfully; these abnormal data must not continue into TBSS group-level stages and must be reported in the final summary.
- TBSS is a group-level analysis: FA maps entering `tbss_1_preproc` must come from the same set of processing units intended for comparison; do not mix data outside this task into the same TBSS working directory.
- If the user does not provide statistical design files, do not invent groups, covariates, or contrasts; complete only through `tbss_4_prestats` and QC/summary.
- If the user provides `design.mat`, `design.con`, optional `design.grp`, or a grouping table, verify that row counts and order match the TBSS input FA order before running statistics.
- Do not use `nohup`, background processes, or methods detached from agent shell monitoring for key stages; run scripts or commands in the foreground so the shell returns the actual success/failure status.

Parallelism and resource use recommendations:

- Subject-level stages (input checks, brain masks, eddy/topup eligibility checks, `dtifit`, and non-FA metric preparation) should run in parallel by processing unit.
- TBSS group-level stages (`tbss_1_preproc`, `tbss_2_reg`, `tbss_3_postreg`, `tbss_4_prestats`) should each run once in the same TBSS working directory; do not launch multiple copies of the same TBSS script concurrently in that directory.
- `tbss_2_reg` is the main time-consuming stage. If the current FSL/cluster submission mechanism is available, let FSL schedule parallel registrations; otherwise keep a single group-level invocation and wait for completion. Do not manually create multiple processes that modify the same TBSS working directory simultaneously.
- For subject-level stages, start with `8-10` processing units in parallel by default; reduce this if CPU, memory, I/O, or GPU trial runs show bottlenecks.
- A failure for one processing unit must not block other units; record the failure reason and evidence paths, and exclude the failed unit from the TBSS input list.
- For shell parallelism, prefer bounded parallelism using `xargs -P`, GNU Parallel, or a `wait -n` style; for Python parallelism, prefer `joblib` or `concurrent.futures.ProcessPoolExecutor`.
- Do not let multiple processing units write to the same temporary filename; all subject-level outputs must include `unit_id` or be written to separate unit directories.

Example shell bounded-parallel scheduling:

```bash
MAX_JOBS=8
printf '%s\n' "${UNIT_IDS[@]}" | xargs -I{} -P "${MAX_JOBS}" bash -lc '
  unit_id="$1"
  run_one_unit "${unit_id}"
' _ {}
```

Example Python joblib scheduling:

```python
from joblib import Parallel, delayed

def run_one_unit(unit):
    return process_unit(unit)

results = Parallel(n_jobs=min(8, len(units)), backend="loky")(
    delayed(run_one_unit)(unit) for unit in units
)
```

---

## 1. Runtime Environment and Software Availability Checks

This protocol requires FSL diffusion/statistics tools. Load `RUNTIME_ENV` before processing and confirm that these commands are available:

- `dtifit`
- `bet`
- `fslroi`
- `fslmaths`
- `fslstats`
- `fslnvols`
- `tbss_1_preproc`
- `tbss_2_reg`
- `tbss_3_postreg`
- `tbss_4_prestats`
- `tbss_non_FA` (only when non-FA skeleton projection such as MD/AD/RD is needed)
- `randomise` (only when voxelwise statistics are needed)

Prefer `command -v <command>` for checks. Do not check FSL `tbss_*` scripts with `--help` in the task workspace, because some shell scripts may create side-effect files such as `FA/`, `origdata/`, and `.tbsslog` even for help/invalid-argument cases. Record only command availability; do not write resolved absolute installation paths into protocol artifacts or use them as subsequent hardcoded paths.

**Stage verification (required)**

- Confirm that all main FSL TBSS workflow commands can be invoked.
- If `randomise` is missing, TBSS prestats can still be completed, but statistics cannot run.
- If any main workflow `tbss_*` command is missing, stop the TBSS stage and report the missing environment component.
- Write the verification report to `<qc_root>/environment/`.

---

## 2. Input Discovery and Processing Unit List

### 2.1 Expected Inputs

Prefer support for BIDS or BIDS-like dMRI:

```text
sub-*/dwi/*_dwi.nii.gz
sub-*/dwi/*_dwi.bval
sub-*/dwi/*_dwi.bvec
sub-*/dwi/*_dwi.json
```

Or sessioned BIDS:

```text
sub-*/ses-*/dwi/*_dwi.nii.gz
sub-*/ses-*/dwi/*_dwi.bval
sub-*/ses-*/dwi/*_dwi.bvec
sub-*/ses-*/dwi/*_dwi.json
```

Also support common subject-level single-shell directories:

```text
<subject_id>/single_shell/<subject_id>.nii.gz
<subject_id>/single_shell/<subject_id>.bval
<subject_id>/single_shell/<subject_id>.bvec
<subject_id>/single_shell/<subject_id>_bse-multi_BrainMask.nii.gz
```

For this input type, treat `<subject_id>/single_shell` as one processing unit. An existing reliable brain mask can be used directly for `dtifit`; if the mask is missing or clearly abnormal, regenerate it from b0/nodif.

Inputs may also already contain an FA map for each subject; if the user explicitly identifies the input as an FA-only dataset, DWI tensor fitting may be skipped and TBSS working directory preparation started directly.

### 2.2 Processing Unit Rules

- If `sub-*/ses-*/dwi` exists, the processing unit is `subject-session`.
- If only `sub-*/dwi` exists, the processing unit is `subject`.
- For `<subject_id>/single_shell`, the processing unit is `subject`, and `unit_id` is `<subject_id>`.
- Unified placeholders:
  - `<unit_root>` = `sub-<id>` or `sub-<id>/ses-<id>`
  - `<unit_id>` = `sub-<id>` or `sub-<id>_ses-<id>`
- All stages must use the same saved list, such as `<qc_root>/processing_units.tsv`.

The list must contain at least:

- `unit_id`
- `unit_root`
- `subject_id`
- `session_id`
- `dwi_path` or `fa_input_path`
- `bval_path`
- `bvec_path`
- `json_path`
- `mask_path`
- `include_in_tbss`
- `exclude_reason`

Implementation constraints:

- Prefer Python standard-library `csv` / `json` or plain shell to generate manifests and QC tables; do not depend on extra packages such as `pandas` or `jq` that may be unavailable.
- TSV manifests may have empty fields such as `session_id`, `json_path`, and `exclude_reason`. Parse TSV using a method that preserves empty columns; do not use a simple `read` form that misaligns columns when fields are empty.
- If a Python script needs to call FSL commands, explicitly pass an `env` containing the FSL bin directory, or initialize `FSLDIR` in a shell script before calling Python. Do not call `source` as an ordinary executable in Python `subprocess.run()`.

**Stage verification (required)**

- Produce a complete, deduplicated, traceable processing unit list.
- Every unit entering tensor fitting must have a matching DWI, bval, and bvec set; `dtifit` may proceed without JSON, but the metadata limitation must be recorded.
- Every unit entering FA-only TBSS must have a nonempty FA map.
- Explicitly list units with missing gradient files, mismatched DWI/gradient volume counts, duplicate units, empty files, damaged NIfTI files, or unreadable data.
- Write the list and verification report to `<qc_root>/input_discovery/`.

---

## 3. Basic DWI QC and Optional Preprocessing

The official TBSS workflow requires generating FA from corrected dMRI. If inputs are explicitly preprocessed DWI, proceed directly to brain masking and `dtifit`; if inputs are raw DWI, at least determine whether motion/eddy-current correction is needed.

This protocol's default strategy:

- If data are explicitly eddy-corrected/preprocessed: do not rerun eddy.
- If data are unprocessed and this task requires full processing: perform motion/eddy-current correction before tensor fitting.
- If eligible reverse phase-encoding data are available, `topup` + `eddy` may be used; if no eligible reverse-PE pair exists, do not fabricate topup inputs.
- For a TBSS benchmark whose task already provides preprocessed inputs, do not add denoise/gibbs or other steps beyond protocol requirements.

Gradient and volume QC:

- Confirm the DWI 4D volume count with `fslnvols` or an equivalent method.
- The bval count and bvec direction count must match the DWI volume count.
- bvec must have three rows or be unambiguously transposable to three rows; if the format is abnormal, record and exclude the unit instead of reordering or repairing it yourself.
- There should be enough b0 volumes for mask/tensor fitting; if no identifiable b0 is present, record it as an error.

Recommended DWI QC script template (using the Python standard library, avoiding pandas/jq):

```python
#!/usr/bin/env python3
import csv
import json
import os
import subprocess

env = os.environ.copy()
if os.environ.get("FSLDIR"):
    fsl_dir = os.environ["FSLDIR"]
    env["PATH"] = f"{fsl_dir}/bin:{fsl_dir}/share/fsl/bin:" + env.get("PATH", "")

manifest_tsv = "<qc_root>/input_discovery/processing_units.tsv"
out_csv = "<qc_root>/dwi_qc/dwi_qc_summary.csv"
os.makedirs(os.path.dirname(out_csv), exist_ok=True)

def count_words(path):
    with open(path, "r", encoding="utf-8") as f:
        return len(f.read().split())

def count_b0(path):
    with open(path, "r", encoding="utf-8") as f:
        return sum(1 for x in f.read().split() if float(x) == 0)

with open(manifest_tsv, "r", encoding="utf-8", newline="") as f:
    units = list(csv.DictReader(f, delimiter="\t"))

with open(out_csv, "w", encoding="utf-8", newline="") as f:
    writer = csv.DictWriter(
        f,
        fieldnames=[
            "unit_id", "dwi_volumes", "bval_count", "bvec_rows",
            "b0_count", "include_in_dtifit", "exclude_reason"
        ],
    )
    writer.writeheader()
    for unit in units:
        unit_id = unit["unit_id"]
        dwi = unit["dwi_path"]
        bval = unit["bval_path"]
        bvec = unit["bvec_path"]
        reasons = []

        try:
            dwi_volumes = int(subprocess.check_output(
                ["fslnvols", dwi], env=env, text=True
            ).strip())
        except Exception as exc:
            dwi_volumes = 0
            reasons.append(f"fslnvols_failed:{exc}")

        bval_count = count_words(bval) if os.path.exists(bval) else 0
        bvec_rows = sum(1 for _ in open(bvec, "r", encoding="utf-8")) if os.path.exists(bvec) else 0
        b0_count = count_b0(bval) if os.path.exists(bval) else 0

        if dwi_volumes != bval_count:
            reasons.append(f"dwi_volumes_{dwi_volumes}_neq_bval_count_{bval_count}")
        if bvec_rows != 3:
            reasons.append(f"bvec_rows_{bvec_rows}_neq_3")
        if b0_count < 1:
            reasons.append("no_b0")

        writer.writerow({
            "unit_id": unit_id,
            "dwi_volumes": dwi_volumes,
            "bval_count": bval_count,
            "bvec_rows": bvec_rows,
            "b0_count": b0_count,
            "include_in_dtifit": not reasons,
            "exclude_reason": ";".join(reasons),
        })
```

**Stage verification (required)**

- Determine explicitly whether each unit is suitable for `dtifit`.
- Record each unit's DWI volume count, bval/bvec counts, b0 count, and whether it is preprocessed or needs preprocessing.
- Provide evidence paths and failure reasons for failed units.
- Write the verification report to `<qc_root>/dwi_qc/`.

---

## 4. Brain Masking and Tensor Fitting to Generate FA

The goal of this stage is to generate one FA map per valid processing unit as TBSS input.

### 4.1 Brain mask

Default method:

- Generate a brain mask from a b0 or nodif image.
- FSL `bet` or an existing reliable mask may be used.
- The mask must be in the same space as DWI, nonempty, and dimensionally matched.

Suggested outputs:

```text
<processed_root>/<unit_root>/dwi/dtifit/<unit_id>_nodif.nii.gz
<processed_root>/<unit_root>/dwi/dtifit/<unit_id>_nodif_brain_mask.nii.gz
```

### 4.2 dtifit

Use FSL `dtifit`; inputs must include:

- 4D DWI
- brain mask
- bvec
- bval
- Output basename

Suggested output directory:

```text
<processed_root>/<unit_root>/dwi/dtifit/
```

Typical outputs should include:

- FA
- MD
- L1
- L2
- L3
- V1/V2/V3
- S0 or other standard `dtifit` outputs

RD can be calculated from `L2` and `L3`; AD corresponds to `L1`. If subsequent non-FA TBSS is needed, prepare MD, AD, and RD maps.

### 4.3 Parallel Execution Requirements

- `dtifit` is a subject-level stage and should run multiple units in parallel.
- Do not use a simple serial loop to process all subjects.
- Each unit must have a separate output directory and basename.
- Record individual unit failures without interrupting the entire batch.

Shell implementation tip (pseudocode illustrating scheduling only). The example assumes the worker receives
`unit_id dwi_path bval_path bvec_path mask_path outbase`; do not parse the original
manifest with empty columns using fragile `read` parsing:

```bash
run_dtifit_unit() {
  unit_id="$1"
  dwi="$2"
  bval="$3"
  bvec="$4"
  mask="$5"
  outbase="$6"

  if [ -n "${FSLDIR:-}" ] && [ -f "$FSLDIR/etc/fslconf/fsl.sh" ]; then
    source "$FSLDIR/etc/fslconf/fsl.sh"
  fi

  dtifit \
    --data="$dwi" \
    --mask="$mask" \
    --bvecs="$bvec" \
    --bvals="$bval" \
    --out="$outbase"
}

export -f run_dtifit_unit
printf '%s\n' "${JOB_TSV_ROWS[@]}" | xargs -I{} -P 8 bash -lc '
  IFS=$'"'"'\t'"'"' read -r unit_id dwi bval bvec mask outbase <<< "$1"
  run_dtifit_unit "$unit_id" "$dwi" "$bval" "$bvec" "$mask" "$outbase"
' _ {}
```

Python joblib implementation tip (pseudocode illustrating scheduling only). When calling FSL from Python,
explicitly pass the FSL environment:

```python
from joblib import Parallel, delayed
import os
import subprocess

env = os.environ.copy()
if os.environ.get("FSLDIR"):
    fsl_dir = os.environ["FSLDIR"]
    env["PATH"] = f"{fsl_dir}/bin:{fsl_dir}/share/fsl/bin:" + env.get("PATH", "")

def run_dtifit_unit(unit):
    cmd = [
        "dtifit",
        f"--data={unit['dwi_path']}",
        f"--mask={unit['mask_path']}",
        f"--bvecs={unit['bvec_path']}",
        f"--bvals={unit['bval_path']}",
        f"--out={unit['outbase']}",
    ]
    completed = subprocess.run(cmd, env=env, text=True, capture_output=True, check=False)
    return {
        "unit_id": unit["unit_id"],
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }

results = Parallel(n_jobs=min(8, len(units)), backend="loky")(
    delayed(run_dtifit_unit)(unit) for unit in units
)
```

**Stage verification (required)**

- Each valid unit should produce one nonempty FA map with a reasonable value range.
- FA maps should be in the same space as the mask, with values usually around 0 to 1; extreme all-zero, all-NaN, clearly out-of-range maps or maps with almost no nonzero intracranial voxels must be treated as abnormal.
- If MD/AD/RD or other maps are generated, they must be nonempty and in the same space as FA.
- For each unit's FA, MD, AD, and RD within the brain mask, count nonzero voxels, min/mean/max, and NaN/Inf values, and save a cohort QC table.
- Perform cohort-relative QC: calculate batch medians for brain-mask means and nonzero voxel counts of FA, MD, AD, and RD, and compare each unit with subjects in the same batch. Do not check only individual image existence and value ranges.
- For small cohorts, do not directly exclude subjects using overly sensitive IQR/MAD rules. Prefer robust comparisons relative to the median: if two or more diffusion metric means for a unit clearly deviate from batch medians (for example, below about 70% or above about 130%), further inspect the parameter maps for global abnormalities, abnormal spatial structure, or clear inconsistency with other units in the same batch.
- Record differences in nonzero voxel count, volume count, or acquisition direction count relative to the same batch as warnings first; do not exclude a unit based only on these differences unless there are also substantial abnormalities in multiple FA/MD/AD/RD metrics or abnormal image spatial structure.
- If there is sufficient evidence of an abnormality while other units in the batch are normal, exclude the unit from the TBSS input list and record the cohort statistics, outlier metrics, and reasons used for the decision.
- Copy or link eligible FA maps to `<tbss_root>/input_fa/`; filenames must stably reflect `unit_id`.
- Write the verification report to `<qc_root>/dtifit/`.

---

## 5. TBSS Working Directory Preparation

The TBSS working directory must be a separate directory that is newly created or safe to clean:

```text
<tbss_root>/
├── input_fa/
├── logs/
└── manifest/
```

Preparation rules:

- FA files entering TBSS must come from units that passed Stage 4 QC.
- Each FA filename must be unique, stable, and traceable to `unit_id`.
- Filenames copied to the `<tbss_root>` top level for `tbss_1_preproc` should use stable `unit_id` values,
  without an `_FA` suffix added beforehand; FSL TBSS scripts generate `*_FA` artifacts themselves. In `input_fa/`,
  original FA naming may be retained, but top-level working copies should avoid producing `*_FA_FA`.
- FA filename sorting affects subject order in the subsequent 4D `all_FA_skeletonised`, so save the sorted list to disk.
- If `randomise` will run later, the design matrix row order must match this sorted list.
- If too few valid FA maps remain for the group-level analysis requested by the user, stop the statistics stage and report; generated FA maps and QC may still be retained.

Suggested files to save:

```text
<tbss_root>/manifest/tbss_input_order.tsv
<tbss_root>/manifest/excluded_units.tsv
<tbss_root>/manifest/tbss_run_config.json
```

**Stage verification (required)**

- Produce a TBSS input directory containing only eligible FA maps.
- Record the counts of included and excluded units.
- Verify that FA files are unique, nonempty, and readable.
- Save the sorted FA list and report its path in the summary.
- Write the verification report to `<qc_root>/tbss_prepare/`.

---

## 6. Standard TBSS Workflow

Run the following steps in order in the same `<tbss_root>`. Record stdout/stderr for each command in `<tbss_root>/logs/`.

### 6.1 `tbss_1_preproc`

Purpose:

- Perform TBSS preprocessing on input FA maps.
- Generate standard TBSS directories such as `FA/` and `origdata/`.

Execution points:

- Copy or link eligible FA maps to the `<tbss_root>` top level, or place them as required by FSL TBSS scripts.
- Do not add an `_FA` suffix to top-level input filenames beforehand, to avoid FSL generating `*_FA_FA` and subsequent steps failing to find expected files.
- Run only on FA maps in the current list; do not use broad wildcards that match historical or unrelated files.
- If wildcards are used, first print and save the actual expanded file list.

**Stage verification (required)**

- Standard TBSS `FA/` and `origdata/` directories should be produced.
- The count of preprocessed FA maps in `FA/` should match the number of units included in TBSS.
- `origdata/` should retain original FA copies or equivalent traceable inputs.
- If `slicesdir` generates HTML or image overviews, record their paths; VLM visual assessment is not required.
- Write the verification report to `<qc_root>/tbss_1_preproc/`.

### 6.2 `tbss_2_reg`

Purpose:

- Nonlinearly register all FA maps to standard space or the specified target.

Default choice:

- For conventional adult data, use the FSL standard FA target by default, namely `tbss_2_reg -T`.
- For children, animals, severe lesions, or an explicit user request for a study-specific target, consider `-n` or the user-specified target, but record the reason in QC.
- Do not change the target strategy without a reason.

Parallelism notes:

- This is the main time-consuming TBSS stage, but it should still run only once for the same TBSS working directory.
- If the FSL runtime supports internal/cluster scheduling, its mechanism may be used; otherwise run in the foreground and wait for completion.

**Stage verification (required)**

- Each FA map should produce standard-space registration-related outputs.
- Check whether the number of registration artifacts under `FA/` matches the number of included units.
- Record failed subjects, missing warps/registered FA maps, and abnormal logs.
- For each unit, `fslstats` may be used to measure registered FA nonzero voxels and intensity ranges to screen for empty maps or obvious abnormalities.
- Write the verification report to `<qc_root>/tbss_2_reg/`.

### 6.3 `tbss_3_postreg`

Purpose:

- Apply nonlinear transformations to standard space.
- Merge all FA maps into 4D `all_FA`.
- Generate `mean_FA` and `mean_FA_skeleton`.

Default choice:

- When `tbss_2_reg -T` uses the standard target, typically use `tbss_3_postreg -T`.
- When `tbss_2_reg -n` uses a study-specific target, typically use `tbss_3_postreg -S`.
- The target strategies of `tbss_2_reg` and `tbss_3_postreg` must be consistent.

**Stage verification (required)**

- Core outputs such as `all_FA`, `mean_FA`, and `mean_FA_skeleton` should be generated in `stats/`.
- The 4D volume count of `all_FA` should match the number of units included in TBSS.
- `mean_FA` and `mean_FA_skeleton` should be nonempty and have matching spatial dimensions.
- Record the nonzero voxel count and intensity range of the mean FA skeleton.
- Write the verification report to `<qc_root>/tbss_3_postreg/`.

### 6.4 `tbss_4_prestats`

Purpose:

- Generate a binary skeleton mask from the skeleton threshold.
- Project each subject's FA onto the mean FA skeleton.
- Generate 4D skeletonised FA data for statistics.

Default threshold:

- Use `0.2` as the starting point by default.
- If data type, age group, lesions, or QC evidence indicates that `0.2` is unsuitable, it may be adjusted, but record the reason.

**Stage verification (required)**

- `mean_FA_skeleton_mask` and `all_FA_skeletonised` should be generated in `stats/`.
- The 4D volume count of `all_FA_skeletonised` must match the TBSS input order list.
- The skeleton mask should be nonempty; record a QC abnormality if the skeleton voxel count is extremely small or abnormally large.
- Calculate each subject's skeletonised FA nonzero voxel count and mean to identify obvious outliers.
- Write the verification report to `<qc_root>/tbss_4_prestats/`.

---

## 7. TBSS Projection of Non-FA Metrics (Optional)

If the user requests TBSS skeleton projection for MD, AD, RD, or other diffusion metrics, use registration and projection information from the FA workflow instead of re-estimating a skeleton for each metric.

Supported by default:

- MD: from `dtifit` outputs.
- AD: usually L1.
- RD: usually calculated as the mean of L2 and L3.

Execution rules:

- Complete the entire FA TBSS workflow first.
- Create a separate directory for each non-FA metric, with filenames strictly corresponding to the original FA input order.
- Use `tbss_non_FA <metric_name>` or an equivalent FSL TBSS mechanism for projection.
- Do not directly rerun `tbss_2_reg` on MD/AD/RD as the main registration basis.

**Stage verification (required)**

- Each metric should produce `stats/all_<metric>` and `stats/all_<metric>_skeletonised`.
- Each metric's 4D volume count should match the FA TBSS input count.
- Each subject's skeletonised metric should be nonempty, with no large numbers of NaN/Inf values.
- Write the verification report to `<qc_root>/tbss_non_fa/`.

---

## 8. Voxelwise Statistics (Optional)

Run this stage only when the user or task explicitly provides a statistical design.

Required inputs:

- Skeletonised 4D input, such as `all_FA_skeletonised`
- Skeleton mask, such as `mean_FA_skeleton_mask`
- `design.mat`
- `design.con`
- Optional `design.grp`

Pre-execution checks:

- The design matrix row count must equal the number of TBSS input subjects.
- The design matrix row order must match `<tbss_root>/manifest/tbss_input_order.tsv`.
- For repeated measures or multiple sessions from the same subject, use appropriate exchangeability blocks; do not treat non-independent observations as independent subjects.
- For single-group description only, or when the task does not request between-group statistics, do not force a group comparison.

Default statistical tool:

- Use FSL `randomise`.
- For TBSS skeleton data, prefer TFCE-related options by default; set the permutation count according to user requirements or task resource constraints.

**Stage verification (required)**

- `randomise` tstat/p-value or corrected p-value outputs should be generated.
- Record the permutation count, design/contrast paths, input skeletonised maps, and mask.
- If statistics were not run, explicitly state whether this was due to missing design files, insufficient sample size, design mismatch, or no user request for statistics.
- Write the verification report to `<qc_root>/randomise/`.

---

## 9. Final Outputs and Summary Requirements

The final summary must report:

- Input data paths and output root.
- Total processing units, number included in TBSS, number excluded, and reasons for exclusion.
- Whether each stage completed:
  - environment check
  - input discovery
  - DWI QC/preprocessing status
  - brain mask / dtifit
  - TBSS prepare
  - `tbss_1_preproc`
  - `tbss_2_reg`
  - `tbss_3_postreg`
  - `tbss_4_prestats`
  - Non-FA projection (if run)
  - `randomise` (if run)
- Key output paths:
  - `<tbss_root>/FA/`
  - `<tbss_root>/origdata/`
  - `<tbss_root>/stats/all_FA`
  - `<tbss_root>/stats/mean_FA`
  - `<tbss_root>/stats/mean_FA_skeleton`
  - `<tbss_root>/stats/mean_FA_skeleton_mask`
  - `<tbss_root>/stats/all_FA_skeletonised`
  - Non-FA skeletonised outputs (if any)
  - Randomise outputs (if any)
- QC abnormalities:
  - Damaged or unreadable inputs
  - Gradient/volume count mismatches
  - Empty FA/MD/AD/RD maps, NaN/Inf, abnormal value ranges, or obvious outliers relative to the batch
  - Missing registration artifacts
  - Empty or extremely abnormal skeleton mask
  - Obvious skeletonised FA outliers
- Whether protocol deviations exist; if so, explain their reasons and impact.

If the task requires comparison with reference processed results, the agent may compare only after processing is complete, using visible references provided by the user; do not assume or access hidden answers during processing.
