---
name: DDParcel Brain Parcellation Pipeline
description: >
  This protocol should be used when the user asks for DWI-space brain parcel/region
  segmentation with DDParcel (DTI scalars -> atlas registration -> DDSurfer ->
  resample back to DWI space). It is designed for agent-driven execution of a
  standardized DDParcel pipeline rather than general-purpose segmentation.
version: 1.0.1
type: pipeline
---

# DDParcel Brain Parcellation Pipeline

## Scope

Use the existing DDParcel workflow to generate DWI-space brain parcellations from dMRI data that have completed the necessary preprocessing. Applicability requirements and prohibitions are defined in “0. General rules” and the detailed sections below.

## Input Contract

Inputs must satisfy the DWI/NHDR, gradient, mask, atlas, and runtime requirements in “1. Inputs and environment”; do not use data from another subject to fill missing inputs.

## Output Contract

Output locations, names, and required intermediate results follow “2. Output layout” and the existing definitions for each stage, while preserving traceability between each processing unit and its inputs.

## Workflow

“3. Pipeline steps (DDParcel)” and its subsequent numbered sections define the authoritative execution order; this contract index does not change their algorithms, parameters, or template invocation.

## Quality Control

Perform the existing QC and completion checks for every stage. Do not mark a processing unit as successful if any critical QC check fails.

## Failure Boundaries

If critical inputs, software, or stage outputs are missing, stop the current processing unit and report completed steps, failure evidence, and a recoverable entry point. Do not silently substitute data or algorithms.

## Privacy Boundaries

Logs, memory, and reports record only necessary paths, processing status, and QC summaries. Do not copy image contents or identifiable participant information into Protocols, memory, or external services.

## Test Contract

Protocol acceptance testing uses one minimal representative processing unit by default. Check templates, paths, and software first, then execute the shortest verifiable workflow; do not test the full dataset in batch unless the user explicitly requests it.

# DDParcel Brain Parcellation Pipeline

This protocol defines a **DDParcel brain parcellation pipeline** using DWI-derived
DTI scalars. It follows the DDParcel workflow from DTI estimation to DDSurfer
prediction, then resamples the result back to DWI space.

Documentation vs scripts:
- This file: workflow rules and when/why to use each step
- `code_templates/`: runnable templates for the pipeline

---

## 0. General rules (mandatory constraints)

- Process only the subject specified by the user/plan; do not switch to another subject on your own.
- Use the input/output paths given in the plan; do not scan global directories to “pick available data.”
- Treat the user-specified directory as the processing target; copied data may have different filename prefixes. Match inputs using the data and gradients in that directory first, and ask only if the actual inputs remain ambiguous.
- First distinguish missing original inputs from derivatives that have yet to be generated. Supported conversion, generation, or registration may use data from this processing unit; report affected steps if recovery is impossible, and do not borrow data from another subject. See RUNTIME_ENV §7 for general QC, reporting, and error-recovery guidance.
- DDParcel requires preprocessed DWI and a brain mask in the same space. Complete necessary conversions or mask preparation using the available data; if full preprocessing is needed, select the appropriate protocol for the user's task and explain the additional computation scope.

---

## 1. Inputs and environment

### 1.1 Required inputs
- Preprocessed DWI in NHDR format (DWI + bval/bvec, with NHDR header)
- Brain mask (NIfTI) in DWI space

### 1.2 Environment
- Conda environment: `conda activate wma`
- DDParcel root: `/opt/DDParcel`
- Slicer: `/opt/Slicer-5.2.2-linux-amd64/Slicer`
- `nifti_write.py` comes from the conda environment (use `nifti_write.py` on PATH).
- `OMP_NUM_THREADS` must be a valid integer (set a safe default if missing).

DDSurfer supports GPU inference. Check CUDA availability in the actual execution
environment before a fresh run; host GPU availability alone is insufficient.
If CPU is used, report that choice and its runtime implications. Keep a live log
path available during long commands rather than piping all progress through tail.

For a task using existing segmentation, inspect the relevant derivative and its
space before deciding to rerun DDParcel. A request to regenerate the segmentation
takes precedence over reuse.

### 1.3 Atlas
- Atlas T2: `/opt/DDParcel/100HCP-population-mean-T2-1mm.nii.gz`

### 1.4 Label reference and ROI provenance
The deployed label mapping is in
`/opt/DDParcel/data_loader/load_neuroimaging_data.py`, function
`map_label2aparc_aseg`; anatomical names are in
`/opt/freesurfer/FreeSurferColorLUT.txt`. The final `*-DDSurfer-wmparc.nii.gz`
contains mapped anatomical IDs, not network class indices. Look up the requested
region in the LUT, then check that ID in the actual output.

In this deployment, `192 = Corpus_Callosum`, `2 = Left-Cerebral-White-Matter`,
and `41 = Right-Cerebral-White-Matter`. The training mapping merges FreeSurfer
CC subdivisions 251–255 into 192, so their absence alone does not mean CC is
missing. Confirm mappings against the installed version when using another model.

For a requested region, derive `label_ids` from the installed mapping and use
`np.isin(wmparc, label_ids)`. The CC example above is deployment-specific, not a
label rule for other atlases, model versions or requested regions. Binarizing all nonzero wmparc values
selects many anatomical regions, not the requested ROI. Save the source affine
and header geometry with the mask.

Existing ROI filenames and old QC success flags are hints, not anatomical
evidence. A focused check of the candidate mask against its source wmparc is
usually sufficient: confirm physical alignment, count selected voxels, and
inspect which source labels occur inside it. For an unchanged direct extraction,
the mask should match the selected label voxels. For erosion, sparse seeding or
other derived masks, check the documented relationship to the target region;
compare in the same physical space (nearest-neighbor resampling for labels when
needed). If inconsistent, derive a fresh ROI from the valid segmentation instead
of rerunning DDParcel or searching unrelated subjects.

A large or slow seed is a reason to review its provenance and seeding strategy;
reducing its size does not establish its anatomical identity. If the requested
label is genuinely absent, explain the evidence and consider an anatomically
justified method; another nonempty label is not an equivalent fallback.

When passing an extracted ROI to UKF, also compare it to the **actual DWI
NHDR/NRRD voxel grid**, not only to the wmparc NIfTI affine. Equivalent world
coverage can still use reversed voxel axes. UKF indexes seed/mask arrays in the
DWI grid; use nearest-neighbor resampling when the index-to-world matrices differ.
See WMA's short input check and reference-grid conversion template. Keep the
source parcellation unchanged. For a displayed ROI task, load the source
parcellation and selected ROI with a same-space anatomical/scalar reference as
useful context for the requested bundle, and describe the actual overlay checked.

Record the source segmentation, selected IDs/names and any seed transformation
in the brief result summary. Keep the whole-brain tracking mask distinct from
the seed; ROI-seeded streamlines may extend outside the seed region.

---

## 2. Output layout

Prefer the task’s explicit output directory or the existing subject workspace. The following is a default for a new workspace, not a reason to duplicate existing derivatives:

```
/workspace/docker_data/processed/<subject>/dwi/ddparcel/
```

Key outputs:
- `*-DDSurfer-wmparc.nii.gz` (final parcellation in DWI space)
- DTI scalar maps and normalized atlas-space maps

---

## 3. Pipeline steps (DDParcel)

### 3.0 NIfTI -> NHDR (required)
DDParcel requires NHDR/NHDR+raw DWI inputs. If you only have NIfTI + bval/bvec,
convert first using the standard template:

- `code_templates/convert_to_nhdr.sh`

Inputs:
- eddy-corrected DWI NIfTI
- matching bval/bvec

Output:
- `<sub-id>_dwi.nhdr` (and .raw alongside it)

### 3.1 DTI estimation (NRRD)
Compute DTI and b0 from the NHDR DWI.

### 3.2 Scalar maps (NRRD -> NIfTI)
Compute FA, Trace, MinEigenvalue, MidEigenvalue and convert to NIfTI.

### 3.3 Register to atlas (BRAINSFit)
Rigid + affine registration of b0 to atlas T2. The template writes the forward
transform with BRAINSFit, then uses Slicer’s bundled SimpleITK to invert it;
BRAINSFit in this runtime does not provide an --inverseTransform option.
Notes:
- Use `.tfm` for the inverse transform filename (ITK text transform). If a `.h5`
  is produced but contains `#Insight Transform File`, copy/rename it to `.tfm`
  before Slicer resampling to avoid mis-detection as HDF5.
- If inverse transform exists only as HDF5 `.h5`, convert to `.tfm` using
  `BRAINSTransformConvert` and **require** a `.tfm` for downstream steps.
- If the inverse transform is missing, **stop** and report the error.

### 3.4 Resample to atlas space
Resample scalar maps and mask to atlas space using the registration transform.

### 3.5 Normalize
Intensity normalization on atlas-space scalars (masked).

### 3.6 DDSurfer prediction
Run DDSurfer to produce atlas-space `wmparc` (MGZ).
Notes:
- DDSurfer output naming follows the output directory basename. If it produces
  `<outdir_basename>-DDSurfer-wmparc-Reg.mgz`, normalize/copy it to
  `<sub-id>-DDSurfer-wmparc-Reg.mgz` for downstream steps.

### 3.7 Resample back to DWI space
Resample atlas-space `wmparc` back to DWI space using the inverse transform.
Notes:
- Prefer the `.tfm` inverse transform path for Slicer.

---

## 4. Template script

Use the **one-shot DDParcel script** (recommended, single command end-to-end):

- `code_templates/run_ddparcel_all.sh`

This wrapper resolves common paths under the subject's processed DWI directory
and then calls `ddparcel_process.sh` once. It is **the only recommended execution
path**; do **not** split steps across multiple run_shell calls.

Example (from working directory):

```bash
bash code_templates/run_ddparcel_all.sh \
  --sub-id 100206 \
  --dwi-root /workspace/docker_data/processed/100206/dwi
```

If you need to override inputs explicitly:

```bash
bash code_templates/run_ddparcel_all.sh \
  --sub-id 100206 \
  --dwi-root /workspace/docker_data/processed/100206/dwi \
  --dwi-nifti /workspace/docker_data/processed/100206/dwi/eddy/eddy_corrected.nii.gz \
  --bval /workspace/docker_data/processed/100206/dwi/eddy/eddy_corrected.bval \
  --bvec /workspace/docker_data/processed/100206/dwi/eddy/eddy_corrected.bvec \
  --mask-nii /workspace/docker_data/processed/100206/dwi/100206_dwi_mask.nii.gz
```

---

Alternative (low-level, use only if needed):

Use the DDParcel template script:

- `code_templates/ddparcel_process.sh`

Example (from working directory):

```bash
bash code_templates/ddparcel_process.sh \
  --sub-id 100206 \
  --dwi-nhdr /workspace/docker_data/processed/100206/dwi/100206_dwi.nhdr \
  --mask-nii /workspace/docker_data/processed/100206/dwi/100206_dwi_mask.nii.gz \
  --output-dir /workspace/docker_data/processed/100206/dwi/ddparcel
```

If only NIfTI + bval/bvec are available, convert first:

```bash
bash code_templates/convert_to_nhdr.sh \
  --dwi-nifti /workspace/docker_data/processed/100206/dwi/eddy/eddy_corrected.nii.gz \
  --bval /workspace/docker_data/processed/100206/dwi/eddy/eddy_corrected.bval \
  --bvec /workspace/docker_data/processed/100206/dwi/eddy/eddy_corrected.bvec \
  --out-nhdr /workspace/docker_data/processed/100206/dwi/100206_dwi.nhdr
```

---

## 5. Template inventory

```
protocols/DDParcel/
  PROTOCOL.md
  code_templates/
    run_ddparcel_all.sh
    convert_to_nhdr.sh
    ddparcel_process.sh
```

---

## 6. Batch/parallel execution (shared conventions)

When processing multiple subjects in batch or in parallel, **follow the shared conventions**:
- Reference: [Runtime environment and batch-processing principles](../RUNTIME_ENV/PROTOCOL.md)

---

## 7. Alignment with Slicer-session tasks (reference conventions)

When the user explicitly requests “complete DDParcel-related steps (import, inspection, module calls, visualization) in the current Slicer session,” this protocol refers to the following shared guidance instead of redefining the workflow details:

- `protocols/RUNTIME_ENV/PROTOCOL.md`
  - `6.3 General execution workflow for Slicer tasks (generalized template)`
  - `6.1 Fail-fast rules for common failures`
  - `6.2 Protocol selection for Slicer tasks`

Execution essentials (short version):
- Establish authoritative inputs first, then reuse the scene, then inspect/run modules.
- Global directory probing and trial-and-error exploration (large-scale list/read/shell probing) are strictly prohibited in Slicer tasks.
- For NIfTI+bval+bvec inputs, prefer conversion to NHDR/NRRD before entering the SlicerDMRI workflow.
- Returned results must include the key `output_paths`, and the corresponding result nodes must be visible in the scene.
