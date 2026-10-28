---
name: FreeSurfer Structural Pipeline
description: >
  Use when the user asks for T1-weighted structural MRI reconstruction, brain
  segmentation, or related FreeSurfer workflows (full recon-all or partial
  stages such as skull stripping and segmentation). This protocol provides
  standardized FreeSurfer usage, environment setup, and output paths.
version: 1.0.0
type: pipeline
---

# FreeSurfer Structural MRI Pipeline Guide

## Scope

FreeSurfer reconstruction, segmentation, or user-specified partial stages for T1w structural images. The original sections below define the specific stage boundaries.

## Input Contract

Input T1w, subject identifier, output root, and runtime environment must satisfy “1. Data formats and input requirements” and “2. Environment and activation.”

## Output Contract

Subject directories, logs, and stage outputs under `SUBJECTS_DIR` follow the original protocol; keep inputs consistent with subject identifiers.

## Workflow

“3. Default strategy and stage rules,” “4. Main workflow and commands,” and subsequent sections define the authoritative workflow; this index does not change `recon-all` parameters or stage selection.

## Quality Control

Perform the original protocol's log, key-file, and segmentation/surface completion checks; a directory existing or a process exiting does not establish passed QC.

## Failure Boundaries

If a license, environment, input, or stage output is missing, stop the current subject and record recoverable commands and evidence; do not switch to another subject.

## Privacy Boundaries

Logs and reports must not include original headers or identifiable participant information; do not send structural images or FreeSurfer artifacts to unapproved external services.

## Test Contract

By default, acceptance tests check only the environment, command construction, and short stages for one small representative input; test a time-consuming full `recon-all` only when explicitly requested.

This guide defines a standard workflow for **structural (T1w) reconstruction and brain parcellation**, applicable to full recon-all or selected stages
(skull stripping, segmentation, surface reconstruction, etc.).

Separate instructions from scripts:
- This file: workflow rules and how to use templates at each step
- `code_templates/`: per-step code templates (loaded by the agent as needed)

---

## 0. General rules (mandatory constraints)

- Process only the subject specified by the user/plan; do not switch to another subject on your own.
- Use the input/output paths given in the plan; do not scan global directories to “pick available data.”
- If directory names and filename prefixes differ or inputs are unclear, ask the user for confirmation before execution.
- Stop and report the reason if critical inputs are missing; do not continue using another subject's data.

---

## 1. Data formats and input requirements

### 1.1 T1w input
- Recommended BIDS structure: `sub-*/anat/sub-*_T1w.nii.gz`
- For original DICOM, convert to NIfTI using dcm2niix first

```bash
dcm2niix -z y -b y -m y -f %p_%s -o /output/path /dicom/path
```

### 1.2 Output directory
- Suggested output directory: `/workspace/docker_data/processed/<subject>/FreeSurfer`
- Actual FreeSurfer subject directory: `/workspace/docker_data/processed/<subject>/FreeSurfer/<subject>`

---

## 2. Environment and activation

### 2.1 Installation location
- Path: `/opt/freesurfer`
- Activation script: `/opt/freesurfer/SetUpFreeSurfer.sh`
- license：`/opt/freesurfer/license.txt`

### 2.2 Required settings before execution

```bash
export FREESURFER_HOME=/opt/freesurfer
export FS_LICENSE=/opt/freesurfer/license.txt
export SUBJECTS_DIR=/workspace/docker_data/processed/<subject>/FreeSurfer
export PATH=$FREESURFER_HOME/bin:$PATH
source /opt/freesurfer/SetUpFreeSurfer.sh
```

### 2.3 Common checks
- Version check: `recon-all -version`
- Environment checks: `echo $FREESURFER_HOME`, `which recon-all`

---

## 3. Default strategy and stage rules

### 3.1 Default strategy
- If the user does not specify otherwise, run **full recon-all** by default
- For lightweight requests such as “segmentation/skull stripping,” use the stage-specific `recon-all` parameters

### 3.2 Suggested stage names
- `recon_all`
- `skullstrip`
- `segmentation`
- `autorecon1`
- `autorecon2`
- `autorecon3`
- `export`

---

## 4. Main workflow and commands

### 4.1 Full workflow (recommended)

```bash
recon-all -s <subject> -i <T1w_path> -all
```

Example:

```bash
recon-all -s sub-001 -i /workspace/docker_data/data/sub-001/anat/sub-001_T1w.nii.gz -all
```

### 4.2 Selected steps only (avoid the full workflow)

```bash
# Brain extraction only (skull strip)
recon-all -s <subject> -i <T1w_path> -skullstrip

# Through aseg+aparc segmentation (without full surface reconstruction)
recon-all -s <subject> -i <T1w_path> -autorecon1 -autorecon2

# Append subsequent steps only (using existing intermediate results)
recon-all -s <subject> -autorecon3
```

---

## 5. Common tools (standalone functions)

- Skull stripping: `mri_watershed <T1w_path> <brainmask.mgz>`
- Export segmentation statistics tables:
  - `aparcstats2table` (cortical parcellation statistics)
  - `asegstats2table` (subcortical segmentation statistics)
- Format conversion: `mri_convert <input> <output>` (mgz <-> nii/nii.gz)
- Volume registration/resampling: `mri_vol2vol --mov <src> --targ <ref> --o <out>`
- Surface file conversion: `mris_convert <surf> <out>`
- Hippocampus/amygdala subfield segmentation: `segmentHA_T1.sh` / `segmentHA_T2.sh`
- Brainstem parcellation: `segmentBS.sh`
- Cerebellar parcellation: `segmentCerebellum.sh`
- Longitudinal processing (multiple time points): `recon-all -long <tp> <base> -all`
- Volume registration: `mri_coreg`, `mri_robust_register`
- Basic information: `mri_info <input>`

---

## 6. Typical lightweight task examples

```bash
# Skull stripping (direct tool)
mri_watershed /workspace/docker_data/data/sub-001/anat/sub-001_T1w.nii.gz \
  /workspace/docker_data/processed/sub-001/FreeSurfer/sub-001/brainmask.mgz

# Segmentation only (without full recon-all)
recon-all -s sub-001 -i /workspace/docker_data/data/sub-001/anat/sub-001_T1w.nii.gz \
  -autorecon1 -autorecon2

# Convert MGZ to NIfTI
mri_convert /workspace/docker_data/processed/sub-001/FreeSurfer/sub-001/mri/aseg.mgz \
  /workspace/docker_data/processed/sub-001/FreeSurfer/sub-001/mri/aseg.nii.gz
```

---

## 7. Directory structure (suggested protocol bundle)

```text
protocols/FreeSurfer/
  PROTOCOL.md
  code_templates/
    recon_all.sh
    skullstrip.sh
    export_aseg.sh
    convert_mgz_to_nii.sh
```

---

## 6. Batch/parallel execution (shared conventions)

When processing multiple subjects in batch or in parallel, **follow the shared conventions**:
- Reference: [Runtime environment and batch-processing principles](../RUNTIME_ENV/PROTOCOL.md)
