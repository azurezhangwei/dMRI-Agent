---
name: HCP Structural + Diffusion Preprocessing Pipeline
description: >
  Use when the user asks to run the HCP-style structural and diffusion MRI
  preprocessing workflow on a subject with T1w/T2w/fieldmap/diffusion inputs.
  This protocol standardizes the full pipeline
  (PreFreeSurfer -> FreeSurfer -> DiffusionPreproc -> PostEddy), enforces a
  one-shot runner, and encodes the failure recoveries learned from prior
  real-world runs.
version: 1.0.0
type: pipeline
---

# HCP Pipeline Guide

## Scope

For the complete existing HCP-style structural MRI and dMRI preprocessing workflow. Input completeness, container conditions, and unified entry-point constraints remain governed by the original detailed sections.

## Input Contract

T1w, T2w, fieldmap, dMRI, subject identifiers, and the environment must satisfy “1. Input and Directory Conventions” and “2. Environment and Container Requirements”.

## Output Contract

Output directories, stage artifacts, logs, and completion markers follow the original protocol definitions and must remain traceable to the specified subject and unified runner.

## Workflow

The subsequent numbered sections and unified entry-point scripts are the authoritative execution method; this contract index does not change the HCP stage order, recovery rules, or parameters.

## Quality Control

Apply the original protocol's stage completion criteria and final QC. A single exit code must not replace checks of artifact and log evidence.

## Failure Boundaries

If GPU, container, license, input, or stage artifact requirements are not met, stop the current subject and report the recovery entry point. Do not bypass failures with separate commands that the protocol does not permit.

## Privacy Boundaries

Keep only necessary paths, stage status, and QC in logs and reports; do not send raw images or identifiable metadata to external services.

## Test Contract

Default acceptance checks only the input contract, environment, and unified runner dry-run/short path for one minimal representative subject; the full, lengthy HCP workflow requires explicit user approval.

This guide covers an **HCP-style structural MRI + dMRI preprocessing pipeline**, including
`PreFreeSurfer -> FreeSurfer -> DiffusionPreproc -> PostEddy`
as a complete workflow.

Instructions and scripts are separate:
- This file: workflow rules, input requirements, stage order, prohibitions, and completion criteria
- `code_templates/`: template scripts that can run directly inside the container

---

## 0. General Rules (Mandatory Constraints)

- Process only the subjects specified by the user/plan; do not switch to other subjects on your own.
- Use the input/output paths given in the plan; do not scan global directories to “select available data”.
- Do not reference, borrow, or compare intermediate results from other subjects to “infer how to run”.
- If directory names, subject IDs, and file prefixes are inconsistent, ask the user for confirmation before execution.
- Stop and report the reason if key inputs are missing. Do not continue using data from other subjects.
- **Do not split the HCP pipeline into a series of separate `run_shell` steps for manual execution.**
  The **unified entry-point script** must be used by default, unless the user explicitly requests debugging of a single stage.
- If the GPU container is not running, the NVIDIA runtime is abnormal, or the HCP/FSL/FreeSurfer environment is incomplete,
  stop and report first; do not continue by “switching to another workflow”.

---

## 1. Input and Directory Conventions

### 1.1 Required Inputs

Each subject must have the following HCP-style `unprocessed/3T/` inputs:

- T1w：`<subject>_3T_T1w_MPR1.nii.gz`
- T2w：`<subject>_3T_T2w_SPC1.nii.gz`
- FieldMap magnitude：`<subject>_3T_FieldMap_Magnitude.nii.gz`
- FieldMap phase：`<subject>_3T_FieldMap_Phase.nii.gz`
- Diffusion RL：
  - `<subject>_3T_DWI_dir95_RL.nii.gz`
  - `<subject>_3T_DWI_dir96_RL.nii.gz`
  - `<subject>_3T_DWI_dir97_RL.nii.gz`
- Diffusion LR：
  - `<subject>_3T_DWI_dir95_LR.nii.gz`
  - `<subject>_3T_DWI_dir96_LR.nii.gz`
  - `<subject>_3T_DWI_dir97_LR.nii.gz`

Default container input directory:

```text
/workspace/docker_data/data/<subject>/unprocessed/3T/
```

Mandatory input completeness constraints:
- For all key `.nii.gz` inputs, checking file existence and header readability alone is insufficient.
- Check compressed stream integrity, for example with `gzip -t <file>.nii.gz`.
- Perform at least one actual voxel-read test, for example `fslstats <file>.nii.gz -R` or equivalent Python/nibabel data reading.
- Evidence such as `unexpected end of file`, `short read`, or `file may be truncated` must be treated as a suspected incomplete input file caused by an interrupted download/copy.
- A file that exists but has truncated contents is a subject-level input error; report the affected subject, and do not replace the file with data from another run or subject.

### 1.2 Output Directory

Default container output directory:

```text
/workspace/docker_data/processed/HCPpipeline_Agent_results/<subject>/
```

Log directory:

```text
/workspace/docker_data/processed/HCPpipeline_Agent_results/logs/
```

---

## 2. Environment and Container Requirements

### 2.1 Execution Location

- This protocol assumes **execution is already inside the processing container**
- It does not depend on `docker exec` or an external launcher on the host
- If the current environment is not a container with HCP/FSL/FreeSurfer available, stop and report

### 2.2 Required Inside the Container

- HCP pipelines：`/workspace/HCPpipelines`
- FSL：`/opt/fsl`
- FreeSurfer：`/opt/freesurfer`
- Connectome Workbench：`/opt/workbench/bin_linux64/wb_command`

### 2.3 Key Environment Variables

```bash
export HCPPIPEDIR=/workspace/HCPpipelines
export HCPPIPEDIR_Global=/workspace/HCPpipelines/global/scripts
export HCPPIPEDIR_Config=/workspace/HCPpipelines/global/config
export HCPPIPEDIR_Templates=/workspace/HCPpipelines/global/templates
export FSLDIR=/opt/fsl
export FREESURFER_HOME=/opt/freesurfer
export CARET7DIR=/opt/workbench/bin_linux64
```

Mandatory constraints:
- `HCPPIPEDIR_Global` must point to `global/scripts`, not just `global/`
- If `CARET7DIR` is not set, stop and report
- Third-party environment initialization scripts such as HCP/FSL/FreeSurfer are not guaranteed to support `set -u`
  or `set -o nounset`. If a custom check script uses `set -euo pipefail`, temporarily use
  `set +u` before `source` of third-party initialization scripts, then restore it afterward; do not directly classify
  undefined-variable errors triggered by strict nounset mode as runtime fatal blockers.
- Verify runtime availability using actual commands and protocol entry-point scripts, such as `command -v recon-all`,
  `recon-all -version`、`command -v wb_command`、`wb_command -version`，
  and the startup results of `code_templates/run_hcp_batch.sh` / `run_hcp_subject.sh`.

---

## 3. Recommended Execution Entry Point (Only Recommended Method)

### 3.1 Single Subject: Unified One-Shot Script

Use the following by default:

```bash
bash code_templates/run_hcp_subject.sh --subject <subject>
```

This is the **only recommended execution path**. It will:

- Automatically check input file completeness
- Automatically prepare `/workspace/docker_data/processed/HCPpipeline_Agent_results/<subject>/`
- Execute sequentially:
  - `PreFreeSurfer`
  - `FreeSurfer`
  - `DiffusionPreproc`
  - `PostEddy`
- Automatically apply known fixes
- Write an artifact handoff report for each stage so the planner can explicitly hand the previous stage's artifacts to the next stage

### 3.2 Batch Parallel Execution

If the user requests batch execution for multiple subjects, use the batch entry point provided by this protocol; do not rely on other protocols or external batch instructions.

Current recommended batch entry point:

```bash
bash code_templates/run_hcp_batch.sh --max-jobs 10 <subject1> <subject2> ...
```

Notes:
- Subject-level parallelism is the default; for 10 or fewer valid subjects, start all subjects together when resources permit.
- **If the user does not explicitly specify concurrency, use the smaller of `--max-jobs 10` and the “number of valid subjects” by default; do not reduce it to 3, 2, or 1 on your own.**
- **Concurrency may be reduced only after current machine/container CPU, memory, I/O, or GPU resource shortages have been verified and evidence recorded.**
- To avoid falling back to script defaults during execution, explicitly specify the actual concurrency in the batch command, such as `--max-jobs 10` or the number of valid subjects.
- Do not use `nohup`, background `&`, `disown`, or similar methods to put batch jobs in the background; run the script in the shell foreground and wait for it to return, then determine completion from the exit code, stage logs, and key artifacts.
- Overall status must be based on actual process exit codes, stage logs, and key artifact verification; do not use status marker files to control completion/failure decisions.

### 3.3 Optional Output Collection

To copy key results separately into a more compact export directory, use:

```bash
bash code_templates/collect_hcp_outputs.sh --subject <subject>
```

Notes:
- This is optional and is not required by the main workflow
- The main workflow already writes its results directly into `study-root`

---

## 4. Stage Order and Completion Criteria

### 4.1 Stage 1: PreFreeSurfer

Goal: generate structural normalization and bias-corrected outputs.

Before this stage starts, complete structural image and fieldmap input integrity checks, including `.nii.gz` compressed stream integrity and actual voxel reading.

Parallel execution requirements:

- PreFreeSurfer should run with subject-level parallelism; do not run all valid subjects sequentially.
- For 10 or fewer valid subjects, start all of them together by default; reduce concurrency only when CPU, memory, or I/O resource bottlenecks have actually been observed.
- When using the batch entry point, explicitly use the smaller of `--max-jobs 10` and the number of valid subjects.
- Skip and record an individual subject's input error; it must not block PreFreeSurfer for other valid subjects.

Required artifacts:

- `T1w/T1w_acpc_dc_restore.nii.gz`
- `T1w/T1w_acpc_dc_restore_brain.nii.gz`
- `T1w/T2w_acpc_dc_restore.nii.gz`

If the files above already exist, this stage may be skipped; otherwise it must run.

### 4.2 Stage 2: FreeSurfer

Goal: complete `recon-all` and the downstream structural artifacts required by HCP.

Parallel execution requirements:

- FreeSurfer/recon-all is a long-running stage; prioritize subject-level parallelism for batches of subjects.
- For 10 or fewer valid subjects, start all of them in parallel by default; do not limit parallelism to 3 or run sequentially without clear evidence of a resource bottleneck.
- If concurrency is reduced because of a resource bottleneck, record the planned concurrency, actual concurrency, and reason for the reduction in stage QC and the final summary.

Completion criteria:

- `T1w/<subject>/scripts/recon-all.log` contains `finished without error`

Also verify the following:

- `T1w/<subject>/mri/transforms/eye.dat`
- `T1w/<subject>/mri/brainmask.mgz`
- `T1w/<subject>/surf/lh.orig`
- `T1w/<subject>/surf/rh.orig`

### 4.3 Stage 3: DiffusionPreproc

Goal: complete PreEddy / topup / eddy.

Before this stage starts, complete input integrity checks for all DWI runs, including the `LR/RL` `dir95/96/97` NIfTI, bval, and bvec files. Check DWI `.nii.gz` compressed stream integrity and perform actual voxel reading to prevent a partially downloaded `.nii.gz` file from passing simply because its header can still be read.

Parallel execution requirements:

- DiffusionPreproc should use subject-level parallelism; neither topup nor eddy should be reduced to sequential subject processing without evidence.
- For 10 or fewer valid subjects, start all of them together by default; reduce concurrency only if GPU memory, system memory, I/O, or actual topup/eddy trial execution fails.
- Prefer the GPU path for eddy, such as `--gpu=true` in the HCP wrapper or an equivalent parameter; do not proactively switch to CPU eddy when a GPU is available.
- If concurrency must be reduced or CPU eddy used as a fallback, record actual concurrency, GPU visibility, failure evidence, and the reason for the fallback in stage QC and the final summary.

Completion criteria:

- `Diffusion/eddy/eddy_unwarped_images.nii.gz`
- `Diffusion/data/data.nii.gz`
- `Diffusion/nodif_brain_mask.nii.gz`

### 4.4 Stage 4: PostEddy

Goal: complete `DiffusionToStructural` and diffusion outputs in T1w space.

Parallel execution requirements:

- PostEddy should run with subject-level parallelism for subjects whose eddy stage is complete; do not wait for all subjects to finish eddy and then run PostEddy sequentially.
- For subjects in the same batch that already have eddy outputs, start all of them together by default; reduce concurrency only when memory, I/O, or FSL/Workbench calls actually fail.
- A PostEddy failure for one subject must not block other subjects; the final summary must list success, failure, and reasons for each subject.

Completion criteria:

- `Diffusion/reg/EPItoT1w.dat`
- `T1w/Diffusion/data.nii.gz`
- `T1w/Diffusion/nodif_brain_mask.nii.gz`

### 4.5 Full Workflow Completion Criteria

All of the following must be satisfied:

- The main log contains `HCP pipeline for <subject> complete`
- Key artifacts from Stages 1–4 are complete and readable
- Each stage's artifact handoff report identifies the source of inputs passed from the previous stage to the next

---

## 5. Mandatory Parameters and Execution Rules

### 5.1 DiffusionPreproc

Use the following:

- `--gpu=true`
- `--topup-config-file=/opt/fsl/etc/flirtsch/b02b0_1.cnf`

Reason:
- The default topup configuration is known to fail with odd slices

### 5.2 FreeSurfer Session Determination

Mandatory constraints:
- `existing-session=TRUE` is allowed only when `T1w/<subject>/mri/orig.mgz` already exists
- If `orig.mgz` does not exist, treat it as a new session

For a new session with a leftover old directory:
- Back up or clean up the old `T1w/<subject>` directory first
- Do not start `recon-all` directly when an “empty subject directory already exists”

### 5.3 Single-Script Principle

- By default, let the one-shot script handle retries and fixes itself
- Do not finish `FreeSurfer` and then manually connect `PostEddy`, unless the user explicitly requests debugging

---

## 6. Known Failure Modes and Required Built-In Fixes

This section contains protocol-level **known mandatory safeguards**, not “optional tips”.

### 6.1 Missing `tkregister`

If the container has only `tkregister2`:

```bash
ln -sf /opt/freesurfer/bin/tkregister2 /opt/freesurfer/bin/tkregister
```

### 6.2 Missing `white.preaparc.rawavg`

A `mri_surf2surf` wrapper must be allowed to rewrite:

- `white.preaparc.rawavg`

as:

- `../surf/lh.white.preaparc.rawavg`
- `../surf/rh.white.preaparc.rawavg`

Otherwise, FreeSurfer conf2hires/vertexvol is prone to failure.

### 6.3 Dangling `lh.white` / `rh.white` / `lh.pial` / `rh.pial` Links

Normalize surf symlinks after FreeSurfer completes:

- Prefer falling back to `*.rawavg`
- Otherwise fall back to `*.preaparc`

### 6.4 Missing `autodet.gw.stats.*`

If absent, generate the following:

- `autodet.gw.stats.lh.dat`
- `autodet.gw.stats.rh.dat`

### 6.5 Missing `brainmask_fs`

If `brainmask.mgz` exists but `brainmask_fs` does not, generate:

- `T1w/brainmask_fs.nii.gz`
- And the `T1w/brainmask_fs` symlink

### 6.6 Missing `white.deformed`

If `lh.white.rawavg` / `rh.white.rawavg` exists, add:

- `lh.white`
- `rh.white`
- `lh.white.deformed`
- `rh.white.deformed`

### 6.7 `fslmaths -thr 0` Crash in FreeSurfer

Use a wrapper strategy:

- Fix environment variables for single-threaded execution
- Provide a Python/nibabel fallback for `fslmaths <in> -thr 0 <out>`

### 6.8 PostEddy / DiffusionToStructural Geometry Mismatch

If the following files in `T1w/Diffusion/` have a shape/affine inconsistent with `data.nii.gz`:

- `nodif_brain_mask_temp.nii.gz`
- `fov_mask.nii.gz`

Register them back to `data.nii.gz` first, then retry:

```bash
flirt -in <mask> -ref data.nii.gz -applyxfm -usesqform -interp nearestneighbour -out <mask>
```

### 6.9 Recovery Order After PostEddy Failure

Recommended recovery order:

1. First fix FreeSurfer-side dependencies: `eye.dat`, `brainmask_fs`, surf links
2. Then fix T1w/Diffusion geometry consistency
3. Prefer trying a direct rerun of `DiffusionToStructural.sh`
4. If it still fails, rerun the entire `DiffPreprocPipeline_PostEddy.sh` once

---

## 7. Output Structure (Suggested Reference)

Typical directory:

```text
/workspace/docker_data/processed/HCPpipeline_Agent_results/<subject>/
├── unprocessed -> /workspace/docker_data/data/<subject>/unprocessed
├── T1w/
│   ├── T1w_acpc_dc_restore.nii.gz
│   ├── T1w_acpc_dc_restore_brain.nii.gz
│   ├── T2w_acpc_dc_restore.nii.gz
│   ├── brainmask_fs.nii.gz
│   ├── Diffusion/
│   │   ├── data.nii.gz
│   │   └── nodif_brain_mask.nii.gz
│   └── <subject>/
│       ├── mri/
│       ├── surf/
│       └── scripts/
├── Diffusion/
│   ├── eddy/
│   ├── reg/
│   └── data/
```

---

## 8. Scheduling, Logs, and Stage Handoff

Unified logs are located at:

```text
/workspace/docker_data/processed/HCPpipeline_Agent_results/logs/
```

For each subject:

- `run_<subject>_full.log`
- `stages/<subject>/<stage>.log`
- `stages/<subject>/<stage>_artifacts.tsv`

Mandatory constraints:
- Do not use status marker files to control completion or failure decisions; these files may be stale when resuming a long workflow.
- Batch scheduling must use the actual exit codes of the processes it starts as the first level of control, and verify key artifacts to confirm whether a stage/subject is ready for handoff.
- Before entering the next stage, the planner must read the previous stage's artifact handoff report or recheck actual artifacts itself, and explicitly list the inputs handed to the next stage.
- `ERROR` in a log is only a diagnostic signal and cannot alone determine failure; failure decisions require the exit code, fatal log context, and missing key artifacts together.

---

## 9. Protocol Execution Recommendations

When the user requests “run the HCP pipeline”, the Plan is recommended to use these stage names:

- `check_inputs`
- `prefreesurfer`
- `freesurfer`
- `diffpreproc`
- `posteddy`
- `verify_outputs`

Suggested default Task description:

> Run the HCP structural + diffusion preprocessing workflow for subject `<subject>`
> using the one-shot robust runner, validate stage outputs, and stop on missing inputs
> or container/runtime failures.

---

## 10. Template Script List

Executable templates currently included in this protocol:

- `code_templates/run_hcp_subject.sh`
- `code_templates/run_hcp_batch.sh`
- `code_templates/collect_hcp_outputs.sh`

---

## 11. Stage QC Checks

Each subject's stage logs, actual key artifacts, and artifact handoff reports must be used in the final summary. Stage QC must describe whether key outputs exist, are nonempty, and meet basic requirements for continuing downstream processing; do not replace actual artifact verification with status marker files.

- `PreFreeSurfer`: expected to generate ACPC/bias-corrected T1w/T2w structural images and brain-extracted structural images usable as FreeSurfer inputs.
- `FreeSurfer`: expected to complete `recon-all` and generate the `mri/`, `surf/`, and `scripts/` structure and key surface/brainmask files needed by subsequent HCP scripts.
- `DiffusionPreproc`: expected to complete topup/eddy-related outputs and produce eddy-corrected diffusion data and a nodif brain mask usable for structural registration.
- `PostEddy`: expected to generate diffusion-to-structural registration and diffusion outputs in T1w space usable for subsequent HCP-style analysis.

Batch QC must summarize success, failure, and reasons for skipping by subject. An input error or processing failure for one subject must not stop other subjects.

---

## 12. Planner Stage Handoff Requirements

The planner cannot assume the executor will strictly follow the status-file design. After each stage, the planner must explicitly include the previous stage's verified key artifacts in the next stage's instructions:

- `PreFreeSurfer -> FreeSurfer`: hand off `T1w/T1w_acpc_dc_restore.nii.gz`, `T1w/T1w_acpc_dc_restore_brain.nii.gz`, and `T1w/T2w_acpc_dc_restore.nii.gz`, stating that existence/readability checks have passed.
- `FreeSurfer -> DiffusionPreproc/PostEddy`: hand off the `finished without error` evidence from `T1w/<subject>/scripts/recon-all.log`, and key structural artifacts such as `mri/transforms/eye.dat`, `mri/brainmask.mgz`, `surf/lh.orig`, and `surf/rh.orig`.
- `DiffusionPreproc -> PostEddy`: hand off `Diffusion/eddy/eddy_unwarped_images.nii.gz`, `Diffusion/data/data.nii.gz`, and `Diffusion/nodif_brain_mask.nii.gz`, and record the topup/eddy log locations.
- `PostEddy -> verify_outputs`: hand off `Diffusion/reg/EPItoT1w.dat`, `T1w/Diffusion/data.nii.gz`, and `T1w/Diffusion/nodif_brain_mask.nii.gz`.

Handoff instructions must include the subject list, completed subjects, skipped/failed subjects, and evidence. An input error or actual fatal failure for one subject may affect only that subject and must not implicitly block other valid subjects.

---

## 13. Protocol Directory Structure

```text
protocols/HCPPipeline/
  PROTOCOL.md
  code_templates/
    run_hcp_subject.sh
    run_hcp_batch.sh
    collect_hcp_outputs.sh
```

---

## 14. Batch/Parallel Execution (Unified Rules)

When processing multiple subjects in batches or in parallel, **follow this protocol's internal rules**:

- Use `code_templates/run_hcp_batch.sh` as the batch entry point.
- Default to the smaller of `--max-jobs 10` and the number of valid subjects, and specify it explicitly in the command.
- Each subject independently completes the entire HCP workflow through `code_templates/run_hcp_subject.sh`.
- Batch scheduling must wait for the subject processes it starts to exit and verify final key artifacts; do not rely on external status marker files.
- A failure for one subject must not block other subjects; the final summary must list success, failure, and reasons for each subject.
