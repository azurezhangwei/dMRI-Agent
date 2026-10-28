---
name: White Matter Analysis (WMA) Pipeline
description: >
  This protocol should be used when the user asks to perform diffusion MRI white matter fiber
  tractography analysis, including DWI preprocessing, distortion and motion
  correction, fiber tract reconstruction (UKF), and fiber bundle segmentation
  with quantitative measurements. It is designed for agent-driven execution of
  a standardized tractography pipeline rather than general-purpose diffusion MRI analysis.
version: 1.0.1
type: pipeline
---

# Diffusion MRI Tract Analysis Pipeline Guide

## Scope

For the dMRI preprocessing, UKF tractography, bundle segmentation, and metric measurement workflow defined in the original protocol. Specific applicability is governed by “0. General Rules” and subsequent sections.

## Input Contract

Dataset paths, processing units, DWI, gradients, reverse phase-encoding data, and other required inputs must satisfy the original input and preprocessing sections.

## Output Contract

Processed, processing code, QC, tractography, bundle, and measurement outputs follow the original directory and naming rules and must be traceable to a unique processing unit.

## Workflow

The subsequent numbered sections and their `code_templates/` and `script/` resources are the authoritative execution method; this index does not change algorithms, parameters, or stage order.

## Quality Control

This revision uses the installed UKF version's default tracking thresholds unless the task explicitly overrides them. QC/reports must record the UKF version, actual default thresholds obtained from module XML or help, explicit overrides, mask/seed sources, and grid adaptation results; clearly mark unavailable values as unverified instead of guessing.

Perform the original protocol's stage QC for preprocessing, tractography, segmentation, and measurement; do not continue using an artifact that has failed key QC.

## Failure Boundaries

If key inputs, software, or artifacts are missing, stop the affected subject/stage, retain evidence and the recovery entry point, and do not use other data or silently change algorithms.

## Privacy Boundaries

Keep only necessary paths, stage status, and QC summaries in logs, memory, and reports; do not send raw images, tractography, or identifiable metadata to unapproved services.

## Test Contract

Default acceptance uses only one small representative processing unit to verify inputs, template connections, and lightweight QC; do not run full batch tractography/segmentation.

# Diffusion MRI Tract Analysis Pipeline Guide

This guide covers a **diffusion MRI tract analysis pipeline** (preprocessing → tractography → bundle segmentation and metric measurement). Select the necessary steps according to the user's goal; if eligible preprocessed, segmented, or tractography inputs already exist, start directly from the relevant stage. Templates provide invocation references; the full preprocessing workflow is not required for every execution.

Instructions and scripts are separate:
- This file: workflow rules + how to use templates at each step
- `code_templates/`: code templates for each step (loaded by the agent as needed)
- `script\`: existing utility code available for invocation
---

## Brief Pre-Run Checks (Apply to the Current Task)

- ROI/brain masks must share the same voxel grid as the **actual DWI file passed to UKF**. Convert NRRD space directions/origin and NIfTI affine to the same RAS or LPS convention, then compare spatial axis order, orientation, spacing, origin, and dimensions; equal dimensions, equal physical coverage, or a mask affine matching only wmparc do not establish direct compatibility with UKF.
- UKF reads seeds/masks using DWI voxel indices and does not automatically resample based on their respective headers. If grids differ, resample discrete ROIs/masks to the actual DWI grid with nearest-neighbor interpolation, then save as uint8 NRRD; do not merely change headers or blindly flip arrays, and there is no need to rerun DDParcel, modify DWI gradients, or repeat preprocessing for this issue.
- As needed, use `code_templates/labelmap_to_reference_grid.py --input <labelmap> --reference <actual_dwi.nhdr> --output <new_mask.nrrd>`. `--labels <IDs...>` selects regions from an anatomical parcellation; `--binary` is only for confirmed brain/ROI masks. The template reads only the DWI header, computes the transformation from affines, preserves original inputs, and assumes neither a particular subject nor fixed axis flips.
- Use the currently installed module's `--help`/schema defaults as the tracking parameter baseline, then adjust for the data and goal. Lowering stoppingFA/mean-signal thresholds allows longer tracking through low-anisotropy regions and requires an explanation; it cannot fix seed spatial misalignment. Increasing seedsPerVoxel changes density and likewise cannot fix input geometry.
- `ukf_tractography.sh` supports separate `SEEDS_NRRD` (using the tracking mask when unspecified), separating ROI seeding from the tracking mask. By default it does not override UKF stopping/seeding thresholds; existing environment variables may be used to pass explicit values.
- When the user requests display of a target bundle, usually load a reference image in the same space (such as b0/FA), the source parcellation, and the ROI/seeds used together, and perform a local overlay/trajectory check; choose display layers, opacity, and views as needed instead of showing isolated fibers only. Report technical completion, spatial verification, and anatomical assessment separately. Direction coloring and solid-color display are both allowed; report the actual settings.

References: [UKF seed/mask reading implementation](https://github.com/pnlbwh/ukftractography/blob/master/ukf/NrrdData.cc), [UKF parameter documentation](https://www.slicer.org/wiki/Documentation/Nightly/Modules/UKFTractography).

## 0. General Rules (Mandatory Constraints)

- Process only the subjects specified by the user/plan; do not switch to other subjects on your own.
- Use the input/output paths given in the plan; do not scan global directories to “select available data”.
- If directory names and file prefixes are inconsistent or inputs are ambiguous, first disambiguate automatically using the user-specified data scope, actual file structure, and this protocol's rules; if reliable disambiguation remains impossible, stop the current subject/stage and report evidence in the summary.
- First distinguish missing original inputs from derivatives yet to be generated. Supported conversions, generation, or registration may use this processing unit's own data; if recovery is impossible, report the affected steps without borrowing data from other subjects. General QC, reporting, and error recovery recommendations are in RUNTIME_ENV §7.
- First identify the “dataset path” `<dataset_root>` from the user request.
- Use the dataset path's parent directory as `data_folder`: `<data_folder> = dirname(<dataset_root>)`.
- Output directories must be inside `data_folder`, with names based on the dataset directory name `basename(<dataset_root>)`:
  - `<processed_root> = <data_folder>/<dataset_name>_processed`
  - `<code_root> = <data_folder>/<dataset_name>_processing_code`
  - `<qc_root> = <data_folder>/<dataset_name>_processing_qc`
- Do not replace path separators with underscores and then concatenate directory names.
- Because data may have unknown corruption or other errors, some data may fail due to their own issues once correct algorithm calls have been confirmed and other data in the same batch can be processed successfully; these highly abnormal or damaged data must not participate in subsequent computation and must be recorded and reported in the final summary.
- Do not “repair” gradient directions or reorder, flip, or replace bvec/bval input gradient information on your own to make algorithms run; if gradient information appears abnormal, record evidence and report it in the summary.

Parallelism and resource use recommendations:
- Independent subjects/sessions may run in parallel; a single subject can run directly. Choose concurrency based on CPU threads per task, memory, GPU memory, I/O, and existing tasks; serial execution is allowed when resources are limited, without first triggering a failure.
- Keep each processing unit's directories and logs separate. Check whether the same task is already running before launch to avoid duplicate scheduling; allocate GPUs according to actual processes and remaining resources.
- Duration varies with volume count, resolution, seed extent, parameters, and hardware; do not determine success or failure from a fixed number of hours. Use process status, log progress, and output update times together; avoid restarting computation during polling.
- Record actual concurrency and thread configuration for batch tasks. Commands below are interface examples; resource parameters depend on the current assessment.

---

## 1. Basic Preprocessing

### 2.1 Shadow Directory Rules (Mandatory Constraints)
- Create new directories only under `<processed_root>/<subject>/dwi/`
- Create file-level symlinks only for **original DWI files**; do not symlink entire original subject directories
- Each stage's output directory is `<processed_root>/<subject>/dwi/<stage>/`
- Create the shadow directory only once at the start of the entire workflow; subsequent stages read existing upstream output paths directly
- Do not create symlinks inside stage subdirectories; original input symlinks are allowed only in the shadow DWI root
- Unless this protocol states otherwise, stage outputs must go to `<processed_root>/<subject>/dwi/<stage>/`, except brain masks (in `<processed_root>/<subject>/dwi/`)
- If a stage outputs only DWI NIfTI without accompanying bval/bvec, **symlink** the upstream bval/bvec into that stage's directory
- Stage names follow this protocol: `brain_mask`, `denoise`, `gibbs`, `topup`, `eddy`, `tractography`, `WMA`

Example structure (placeholders):
```
<processed_root>/<subject>/dwi/
├── <raw_dwi_symlinks>            # only raw inputs (nii/bval/bvec/json)
├── <subject>_dwi_mask.nii.gz     # brain mask at shadow DWI root
├── brain_mask/
├── denoise/
├── gibbs/
├── topup/
├── eddy/
├── tractography/
└── WMA/
```

### 2.2 Default Strategy (When the User Specifies Nothing Further)
- Run only “necessary steps”: **brain mask + topup (depending on data) + eddy**
- Run `denoise` and `gibbs unring` only when explicitly requested by the user or required by data quality; do not proactively add them

### 2.3 Default Algorithm Choices (When Unspecified by the User)
- brain mask：DIPY `median_otsu`
- denoise：MRtrix `dwidenoise`
- gibbs：DIPY `gibbs_removal`
- topup：FSL `topup`
- eddy: FSL `eddy` (prefer the GPU version)

### 2.4 Brain Mask (Required)
Purpose: masks are needed for eddy, tensor fitting, tractography, and other steps.

Template: `code_templates/brain_mask_median_otsu.py`
- Input: DWI + bval/bvec
- Output: preferably in the shadow DWI root: `<processed_root>/<subject>/dwi/<subject>_dwi_mask.nii.gz`

Example invocation (in the shadow DWI root):
```bash
python3 code_templates/brain_mask_median_otsu.py
```

Parallel execution requirements:
- Brain masking may process independent subjects in parallel as resources permit.
- Choose subject concurrency according to current available CPU, memory, and I/O.
- Batches may use `xargs -P`, GNU Parallel, or joblib; direct serial execution is allowed for limited resources or a single subject.
- Each subject must have a separate mask output path; an individual subject's failure must not block other subjects.

Scheduling recommendation: reuse this stage's single-subject template, using a scheduler with a configurable concurrency limit for batches; keep inputs, outputs, and logs separate by processing unit and avoid unlimited background launches.

Stage verification (required):
- Produce a nonempty brain mask covering the main brain parenchyma in the same space as DWI; a mask limited to a tiny region or clearly shifted outside the brain must be treated as abnormal.
- Confirm that the mask file exists, matches input DWI dimensions, and is nonempty.
- Record output paths and key parameters (such as the number of b0 volumes used).

### 2.5 Denoise (Optional, Requires an Explicit Request)
Template: `code_templates/denoise_mrtrix_dwidenoise.sh`
- Input: DWI + bval/bvec + mask
- Output: denoised results and QC residuals under `denoise/`

Example invocation:
```bash
bash code_templates/denoise_mrtrix_dwidenoise.sh
```

Parallel execution requirements:
- If enabled, this stage should also use subject-level parallelism by default, instead of denoising all subjects serially before moving to the next step.
- Choose concurrency according to DWI size and `dwidenoise` memory requirements.
- `denoise` is optional, but once enabled, specify clear batch scheduling as for other stages instead of providing only a single-subject template.

Scheduling recommendation: reuse this stage's single-subject template, using a scheduler with a configurable concurrency limit for batches; keep inputs, outputs, and logs separate by processing unit and avoid unlimited background launches.

Stage verification (required):
- Produce denoised DWI with the same volume count as the input, still usable for subsequent geometric correction and tractography.
- Confirm that denoised outputs exist and the volume count is unchanged.
- If residual/noise maps are generated, record their paths and sizes.

### 2.6 Gibbs Ringing Correction (Optional, Requires an Explicit Request)
Template: `code_templates/gibbs_removal_dipy.py`
- Input: DWI + bval/bvec
- Output: Gibbs-corrected DWI in the current directory

Example invocation:
```bash
python3 code_templates/gibbs_removal_dipy.py
```

Parallel execution requirements:
- If applicable, this stage may process multiple independent subjects in parallel as resources permit.
- Consider subject concurrency together with internal tool parallelism to avoid overallocating CPU/memory.
- Each subject must have a separate output filename to avoid different subjects writing to the same target path.

Scheduling recommendation: reuse this stage's single-subject template, using a scheduler with a configurable concurrency limit for batches; keep inputs, outputs, and logs separate by processing unit and avoid unlimited background launches.

Stage verification (required):
- Produce Gibbs-corrected DWI with unchanged volume count and the original matching gradient information, without losing additional volumes or breaking subsequent input consistency.
- Confirm that Gibbs-corrected DWI exists and the volume count is unchanged.
- Record input/output paths and main parameters.

### 2.7 Topup (Only With a Standard Reverse Phase-Encoding Pair)
Trigger: a standard paired reverse phase-encoding dataset exists and the corresponding JSON contains:
- `PhaseEncodingDirection`
- `TotalReadoutTime`

Here, “standard paired reverse phase encoding” must satisfy:
- Both datasets' `PhaseEncodingDirection` values are on the same axis with opposite polarities, such as `j/j-`, `i/i-`, or `k/k-`.
- Common anatomical-direction examples are `AP/PA`, `PA/AP`, `LR/RL`, and `RL/LR`.
- Cross-axis combinations such as `i- + j` and `j + k-` are not standard reverse-PE pairs and must not enter `topup` by default.

Mandatory constraint: without reverse phase encoding, skip topup; do not fabricate it or substitute concatenated unidirectional data.
Mandatory constraint: if the JSON lacks the fields above, skip topup and report the reason.
Mandatory constraint: if the two datasets do not have opposite polarities on the same axis, record only `SKIP/unsupported`; do not run `topup` simply because “directions differ”.
Before execution, check the original DWI directory for compliant JSON; if it is noncompliant, do not include it in the plan.
Complete eligibility checks at the planner stage before including topup in the Plan.

Templates:
- `code_templates/topup_fsl.sh`
- `code_templates/make_acqp_from_bids_json.py` (generate `acqp.txt` from JSON)

Execution points:
- Refer to `make_acqp_from_bids_json.py` to generate `acqp.txt`
- `topup_fsl.sh` extracts b0 volumes, merges them, runs topup, and outputs `topup_results`

Parallel execution requirements:
- Subjects eligible for topup should be scheduled with subject-level parallelism instead of sequentially.
- Choose topup concurrency according to reference volume size, memory, and I/O load.
- Record ineligible subjects separately as `SKIP`; they must not delay other eligible subjects.

Scheduling recommendation: reuse this stage's single-subject template, using a scheduler with a configurable concurrency limit for batches; keep inputs, outputs, and logs separate by processing unit and avoid unlimited background launches.

Stage verification (required):
- Produce field estimates and reference b0 outputs that support subsequent distortion correction; if only intermediate fragments remain without usable topup results, treat the stage as incomplete.
- Confirm that `acqp.txt` was generated successfully and contains two rows for opposite directions.
- Confirm that `topup_results` outputs exist and record key file paths.

### 2.8 Eddy (Usually Required)
Prerequisite: `acqp.txt` and `index.txt` can be generated from actual JSON.

Mandatory constraints:
- Do not generate `acqp/index` with hardcoded fixed defaults
- If missing required JSON fields prevent generation, skip eddy and report the reason
  - Do not guess `PhaseEncodingDirection` or `TotalReadoutTime`
- If the plan includes eddy but checks fail, stop this step and report the reason
  - Complete eligibility checks at the planner stage before including eddy in the Plan
- After generating `acqp.txt`, perform eddy preflight/plausibility checks; if FSL eddy reports `Unrealistic read-out time`, do not simply classify the subject as a data failure and abandon it. Apply the compatibility rules in this protocol's templates and record the original JSON value and actual written value.

Templates:
- `code_templates/eddy_fsl.sh`
- `code_templates/make_acqp_from_bids_json.py`
- `code_templates/make_index_txt.py`

Parallel execution requirements:
- For multiple subjects, eddy uses subject-level parallelism by default; do not reduce it to sequential subject processing without a reason.
- Choose eddy concurrency according to the number of independent processing units and available resources; the entire batch need not start together.
- When GPU eddy is available, prefer it and allocate GPUs based on free GPU memory, free system memory, other task usage, and actual trial results; do not treat GPU count as the subject concurrency limit or equate total memory usage on a GPU with the current single eddy process's usage.
- Before launching GPU eddy, check existing GPU tasks and availability (such as with `nvidia-smi`), placing new tasks on idle or least-loaded GPUs where possible.
- For GPU parallelism, explicitly control each eddy process's visible GPU (such as `CUDA_VISIBLE_DEVICES` or equivalent) to avoid all processes competing for the same card; multiple subjects may run together on one GPU if resource monitoring indicates sufficient capacity.
- If resources are insufficient, the GPU version is unavailable, trial execution fails, or OOM/GPU memory contention occurs, concurrency may be reduced or CPU eddy used; explain the evidence in logs and the summary.
- For CPU eddy fallback, still prefer subject-level parallelism; control concurrency together with `OMP_NUM_THREADS` and total CPU/memory load, avoiding `parallel subject count * threads per subject` exceeding resource limits.
- A subject's eddy failure must not block other subjects; record the failure reason, continue processing the remaining subjects, and report failed subjects in the final summary.
- For multiple units, wrap `code_templates/eddy_fsl.sh` in a batch scheduler; run directly for a single unit.

Scheduling recommendation: reuse this stage's single-subject template, using a scheduler with a configurable concurrency limit for batches; keep inputs, outputs, and logs separate by processing unit and avoid unlimited background launches.

Stage QC verification (required):
- Produce eddy-corrected results usable for subsequent tractography, including corrected DWI, corresponding rotated gradient information, and logs explaining execution behavior.
- Confirm that `acqp.txt` and `index.txt` were generated successfully and volume counts match.
- Confirm that eddy outputs (corrected DWI, rotated bvec, logs) exist.

---

## 2. Tractography and Segmentation

### 3.1 Tractography (UKF Tractography)
Templates and tools:
- `code_templates/ukf_tractography.sh`
- `code_templates/ukf_tractography_parallel_units.sh`
- `code_templates/nhdr_write_help.txt` (help for the NHDR conversion script)
- `code_templates/mask_cast_to_uint8_nrrd.py` (convert masks to uint8 to meet UKF mask type requirements)

Key requirements:
- UKF input DWI requires an nhdr header (place nhdr in the same directory as the original nii.gz)
- Masks must use a data type accepted by UKF (conversion to uint8 is recommended)

Parallel execution requirements:
- Multi-subject tractography may use batch scheduling, choosing concurrency and threads per task together.
- Tractography memory and time depend on seed extent, sampling density, and tracking parameters; assess concurrency for the current task.
- For multiple subjects with completed eddy, prefer calling the batch template `code_templates/ukf_tractography_parallel_units.sh` directly; it schedules from a `unit_root` list and independently prepares NHDR, mask, and output directories for each subject.
- `code_templates/ukf_tractography.sh` may be reused; a single processing unit needs no additional batch wrapper.
- A subject's tractography failure must not block other subjects; record failed subjects and continue processing the remainder.

Scheduling recommendation: reuse this stage's single-subject template, using a scheduler with a configurable concurrency limit for batches; keep inputs, outputs, and logs separate by processing unit and avoid unlimited background launches.

Example invocation:
```bash
bash code_templates/ukf_tractography.sh
```

Example batch invocation:
```bash
cat > units.txt <<'EOF'
sub2211001
sub2211002
sub2211003
EOF

PROCESSED_ROOT=/path/to/SMU_processed \
UNITS_FILE=./units.txt \
MAX_JOBS=6 \
UKF_NUM_THREADS=4 \
bash code_templates/ukf_tractography_parallel_units.sh
```

Stage verification (required):
- Produce a clearly nonempty whole-brain tractogram; in common adult whole-brain tractography, outputs are usually large VTK/VTP files with hundreds of thousands to millions of streamlines, rather than small files containing nearly empty bundles.
- Confirm that tractography outputs exist (vtk/vtp or the specified format) and have nonzero file sizes.
- Record main parameters (seed count, step size, thresholds, maximum length, etc.) and output paths.

### 3.2 Bundle Segmentation and Metric Measurement (whitematteranalysis + ORG)
Template:
- `code_templates/wma_org_apply.sh`
- `code_templates/wma_org_apply_parallel_units.sh`

Key requirements:
- The current environment is headless; run with `xvfb-run -a` to avoid GUI errors

Example invocation:
```bash
xvfb-run -a bash code_templates/wma_org_apply.sh
```

Parallel execution requirements:
- WMA bundle segmentation and measurement may run in parallel by subject, retaining each task's status and logs.
- Choose concurrency according to tractogram size, available memory, and tool thread counts; do not require the entire batch to run together.
- Prefer the batch template `code_templates/wma_org_apply_parallel_units.sh`; it independently calls `xvfb-run -a` and `wma_org_apply.sh` for each subject from the `unit_root` list.
- If this batch template is not used, explicitly specify subject-level concurrent scheduling anyway; do not provide only single-subject commands without batch implementation guidance.
- A subject's WMA failure must not block other subjects; failed subjects must retain their own logs and be listed separately in the summary.

Scheduling recommendation: reuse this stage's single-subject template, using a scheduler with a configurable concurrency limit for batches; keep inputs, outputs, and logs separate by processing unit and avoid unlimited background launches.

Stage QC checks (required):
- Produce WMA results suitable for bundle-level checks: at least a set of anatomical bundle files and measurement/QC artifacts readable downstream; if only scattered intermediate files or many nearly empty bundles remain, flag the abnormality at this stage.
- By default, this step performs statistical abnormality screening only; visual inspection of bundle morphology is not required.
- Core criteria are whether each tract's output file is actually nonempty and whether file size and streamline count are of a normal order of magnitude.
- Commonly, a single bundle's `.vtk/.vtp` file ranges from a few MB to tens of MB; if many tracts are only a few KB or contain clearly too few streamlines, this usually indicates a problem with that individual's segmentation.
- Assessment criteria:
  - `PASS`: most tract file sizes and streamline counts are within normal ranges, without widespread nearly empty bundles.
  - `WARNING`: some tracts are clearly small or have few streamlines, without widespread abnormalities.
  - `FAIL`: many tract files are only a few KB and nearly empty, or several major bundles have clearly abnormally low streamline counts.
- Apply stricter checks to major bundles (such as CST): if a major bundle is itself nearly empty, or several major bundles are abnormally small together, treat this directly as `FAIL`.

Script location (protocol scripts):
- `protocols/WMA/scripts/tract_cohort_qc.py`

Suggested workflow:
1) First check whether the `AnatomicalTracts` directory actually contains a set of generated tract files instead of only scattered intermediate files.
2) Calculate each tract's file size and streamline count.
3) Then run cohort-level numerical QC, comparing tract file sizes and streamline counts across all individuals to identify subjects/tracts clearly below the overall level.
4) If an individual has widespread nearly empty bundles, flag the abnormality at this stage and include it in the QC report.

Example commands (run from any working directory):
```bash
python3 protocols/WMA/scripts/tract_cohort_qc.py \
  --processed_root <processed_root> \
  --out_dir <processing_qc_root>/WMA_cohort
```

Default output location (this protocol):
- Cohort numerical QC directory: `<processing_qc_root>/WMA_cohort/`
- After completion, report statistical abnormalities for each subject/tract and the corresponding evidence paths.

Example batch invocation:
```bash
cat > units.txt <<'EOF'
sub2211001
sub2211002
sub2211003
EOF

PROCESSED_ROOT=/path/to/SMU_processed \
UNITS_FILE=./units.txt \
MAX_JOBS=6 \
WMA_THREADS=4 \
bash code_templates/wma_org_apply_parallel_units.sh
```

---

## 3. Directory Structure

```text
protocols/WMA/
  PROTOCOL.md
  code_templates/
    brain_mask_median_otsu.py
    denoise_mrtrix_dwidenoise.sh
    gibbs_removal_dipy.py
    make_acqp_from_bids_json.py
    make_index_txt.py
    topup_fsl.sh
    eddy_fsl.sh
    nhdr_write_help.txt
    mask_cast_to_uint8_nrrd.py
    ukf_tractography.sh
    ukf_tractography_parallel_units.sh
    wma_org_apply.sh
    wma_org_apply_parallel_units.sh
  scripts/
    tract_cohort_qc.py
```

---

## 4. Slicer Session Task Alignment (Reference Rules)

When the user explicitly requests “complete WMA-related steps in the current Slicer session (import, inspection, module invocation, visualization)”, this protocol does not define a separate set of execution behaviors; it refers uniformly to:

- `protocols/RUNTIME_ENV/PROTOCOL.md`
  - `6.3 General Slicer Task Execution Workflow (Generalized Template)`
  - `6.1 Frequent Failure Fail-Fast Rules`
  - `6.2 Slicer Task Protocol Selection`

Execution points (short version):
- Authoritative inputs first, then scene reuse, then module inspect/run.
- Do not use trial and error by probing global directories; prefer confirmed inputs and output node IDs from the previous step.
- When the user specifies Slicer/SlicerDMRI, do not silently switch to FSL/DIPY/MRtrix (unless the user agrees to a fallback).
- Results must include both scene-visible nodes and key `output_paths`.


## Scope and Acceptance for Single-Bundle Tasks
For targets such as CST, AF, and CC, choose supported ROI tracking or bundle extraction from an existing tractogram
according to the user request. Seeds determine starting points, the tracking mask determines the allowed propagation area, and waypoint /
exclusion ROIs filter paths; these are distinct concepts. Combining large white-matter regions with cortex and brainstem
as seeds does not alone establish that the output is CST. DDParcel region labels are not bundle labels either.
Assess results using label semantics, spatial alignment, and the target bundle's trajectory checks; nonempty files and fiber counts establish only
successful technical output. Report the evidence actually checked and the aspects still unconfirmed.
Single-bundle tasks need not include whole-cohort QC; reuse of existing intermediate artifacts depends on task requirements and input suitability.

When reusing seeds named after a bundle, perform a targeted label-overlap check against the source parcellation if appropriate.
Success in old QC and filenames do not establish region identity; shrinking incorrect seeds will not produce the correct region either.
See DDParcel §1.4 for DDParcel label lookup and validation of reused ROIs.
