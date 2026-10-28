---
name: Runtime Software Environment
description: >
  Supplementary runtime guidance for the deployed dMRI software stack. Load this
  when a task depends on what software is available, how the container runtime
  is initialized, or how command templates should be adapted to the active
  environment.
version: 1.0.0
type: supplement
---

# Runtime Software Environment

## Scope

This is a supplemental runtime-environment Protocol. It provides only software inventory, activation, and template-adaptation facts; it does not define or replace the main pipeline's algorithms or stage order.

## Input Contract

Inputs are verifiable environment states of the current Agent host, container, or Slicer session, and explicit software requirements of the main pipeline.

## Output Contract

Outputs identify available software, versions, paths, activation methods, and limitations with sources; do not describe “usually present” software as verified in the current environment.

## Workflow

First obtain current facts using software inventory, then interpret the environment and template adaptations according to the sections below; software installation or removal must use dedicated software-management capabilities and separate approval.

## Quality Control

Verify path existence, executable versions, and required environment variables separately; static documentation alone does not establish software availability.

## Failure Boundaries

If the environment cannot be verified, report unknowns and required checks. Do not guess installation locations or bypass the main Protocol's software-version or execution constraints.

## Privacy Boundaries

Environment inventory must not read or expose API keys, tokens, patient data, or unrelated user files; logs retain only necessary software facts.

## Test Contract

Acceptance tests run only version queries, path checks, and data-free smoke tests; do not install large software or launch time-consuming neuroimaging workflows merely for testing.

This protocol supplements the runtime and is not the main processing pipeline.

When to use it:

- To identify available dMRI software in the current container/runtime
- To determine typical activation methods for conda, FSL, FreeSurfer, ANTs, and related software
- To adapt container paths in protocol templates into executable forms for the current runtime
- To quickly decide whether to invoke a command directly, source initialization first, or activate an environment first

It does not define stage order. The user-specified main pipeline protocol determines the processing workflow, algorithm selection, and stage QC.

## 1. Example software layout (not evidence of the current installation)

- First call `software_inventory(refresh=true)` and load `RUNTIME_LOCAL` to confirm facts about the current host.
- The paths below are examples from an optional Linux layout; macOS, Windows, and user-built containers are not guaranteed to have these paths or software.
- Conda environment names depend on the actual installation; do not assume an environment named `wma` exists.
- Example directories under `/opt` in an optional Linux layout:
  - `DDParcel`
  - `ORG`
  - `Slicer-5.2.2-linux-amd64`
  - `ants-2.6.3`
  - `conda`
  - `conversion`
  - `dcm2niix`
  - `freesurfer`
  - `fsl`
  - `workbench`
  - `miniconda.sh`
- Common FSL activation script path:
  - `/opt/fsl/etc/fslconf/fsl.sh`
- Determine the environments containing MRtrix3 and DIPY from software inventory or user-provided installation information
- Software/commands commonly invoked directly or after activation include:
  - `dcm2niix`
  - `topup`
  - `eddy`
  - `eddy_openmp`
  - `eddy_cuda`
  - `dtifit`
  - `bet`
  - `fslroi`
  - `fslnvols`
  - `fslmaths`
  - `fslstats`
  - `dc`
  - `flirt`
  - `fnirt`
  - `applywarp`
  - `tbss_1_preproc`
  - `tbss_2_reg`
  - `tbss_3_postreg`
  - `tbss_4_prestats`
  - `tbss_non_FA`
  - `randomise`
  - `recon-all`
  - `mri_convert`
  - `wb_command`
  - `antsRegistration`
  - `antsApplyTransforms`
  - `UKFTractography`
  - `wm_apply_ORG_atlas_to_subject.sh`
  - `wm_apply_ORG_atlas_to_subject.py`
  - `wm_apply_ORG_atlas_to_subject`
  - `build_connectome_matrix.py`
  - `xvfb-run`

## 2. Basic principles

- Although software is commonly installed under `/opt`, not everything is sourced automatically by default.
- If a protocol step depends on a specific software environment, initialize it explicitly, or establish the software path or help information beforehand.
- Do not source a collection of environment scripts “just in case” unless the protocol explicitly requires it.
- Prefer variables and PATH already exported in the current environment, then fall back to common fixed container paths.
- Hardcoded container paths in templates may be adapted according to this protocol, without changing the required algorithms or I/O structure.

Third-party environment scripts are usually written for ordinary interactive shells and may not be compatible with
strict modes such as `set -u` / `set -o nounset`. While nounset is enabled, do not directly
`source` conda, FSL, FreeSurfer, ANTs, Workbench, or other software initialization scripts.
If your own script needs strict mode, temporarily disable nounset before sourcing third-party initialization scripts,
then restore it after initialization, for example:

```bash
set -euo pipefail
set +u
source /opt/conda/etc/profile.d/conda.sh
source /opt/fsl/etc/fslconf/fsl.sh
source /opt/freesurfer/SetUpFreeSurfer.sh
set -u
```

Judge environment availability by whether actual commands run and protocol entry scripts start,
for example `command -v recon-all`, `recon-all -version`, `command -v eddy_cuda`, and
`wb_command -version`. Do not directly interpret undefined-variable errors raised by third-party
initialization scripts under strict shell mode as a missing software environment or an unusable container.

## 3. Recommended initialization order

### 3.1 Conda

In a noninteractive shell, source conda initialization first, then activate `wma`:

```bash
source /opt/conda/etc/profile.d/conda.sh
conda activate wma
```

Notes:

- MRtrix3, DIPY, and some Python tools commonly depend on `wma`
- Do not reactivate `wma` if the current shell is already in that environment

### 3.2 FSL

If a step depends on FSL, ensure `FSLDIR` is correct before sourcing:

```bash
export FSLDIR=/opt/fsl
source "$FSLDIR/etc/fslconf/fsl.sh"
export PATH="$FSLDIR/bin:$FSLDIR/share/fsl/bin:$PATH"
```

Tools commonly requiring this initialization:

- `topup`
- `eddy`
- `eddy_openmp`
- `eddy_cuda`
- `fslroi`
- `fslmerge`
- `fslnvols`

### 3.3 FreeSurfer

If a step depends on FreeSurfer:

```bash
export FREESURFER_HOME=/opt/freesurfer
export FS_LICENSE=/opt/freesurfer/license.txt
source "$FREESURFER_HOME/SetUpFreeSurfer.sh"
```

Common tools:

- `recon-all`
- `mri_convert`

### 3.4 ANTs

If a step depends on ANTs:

```bash
export ANTSPATH=/opt/ants-2.6.3/bin/
export PATH="$ANTSPATH:$PATH"
```

Common tools:

- `antsRegistration`
- `antsApplyTransforms`

### 3.5 Connectome Workbench

If a step depends on HCP's Workbench:

```bash
export CARET7DIR=/opt/workbench/bin_linux64
export PATH="$CARET7DIR:$PATH"
```

Verify:

```bash
command -v wb_command
wb_command -version
```

Mandatory constraints:

- The HCP pipeline must use the actual Workbench `wb_command`.
- Do not use old `wb_command` compatibility/replacement scripts.
- If the actual `wb_command` is unavailable, switch to a test image containing Workbench, such as `dmri-agent-freesurfer:mrtrix-workbench`.


## 4. Environment adaptation

This protocol provides runtime software-environment information.

If a pipeline protocol's template already contains fixed environment initialization or software paths, equivalent adaptations to the current runtime are allowed, but only for:

- Conda activation methods
- Environment-variable initialization
- Software executable paths

Adaptation must preserve the original protocol's:

- Algorithms
- Stage order
- Input/output structure
- Parameter meanings

For example, if a template directly contains:

```bash
source /opt/conda/etc/profile.d/conda.sh
conda activate wma
export FSLDIR=/opt/fsl
source "$FSLDIR/etc/fslconf/fsl.sh"
eddy_cuda ...
```

It may remain unchanged if it accurately reflects the current runtime.

If the current environment is already activated, a shorter form is allowed; do not omit necessary initialization before confirming that the environment is ready.

## 5. When this protocol must be loaded

Do not rely on recollection in the following cases; load this protocol first:

- Determining whether a tool comes from `wma` or the system PATH
- Running commands related to FSL / FreeSurfer / ANTs / UKF / WMA / connectomes
- Adapting a protocol's code templates to the current container
- Seeing `/opt/...` paths without knowing whether to source initialization or invoke them directly

Loading this protocol is optional for tasks limited to reading workflow instructions, organizing files, writing QC, or reading logs without software initialization.

## 6. Slicer execution constraints

When a task explicitly requires execution within the current Slicer session, follow these rules:

- Do not launch a new Slicer from the shell (for example `Slicer --launch`, `--python-script`, or `--python-code`), or directly invoke Slicer CLI binaries from the shell. Prefer `run_slicer_module(action='inspect'|'run')` to keep operations in the current session/window.
- Prefer structured module calls: `run_slicer_module(action='inspect', module=...)` before `run_slicer_module(action='run', module=...)`. Reserve `mcp_slicer_execute_python_code` for small scene-glue operations (scene inspection, display control, and node I/O).
- If the same module/intent fails repeatedly (for example, 2 consecutive failures), stop and return an explicit error instead of a long probing loop.
- For diffusion MRI stored as `NIfTI + bval + bvec`, prefer conversion to `NHDR/NRRD` before loading it into the Slicer scene.
- Verified primary conversion route: `dcm2niix` (DICOM -> NIfTI+bval+bvec), then `DWIConvert` with `FSLToNrrd`. Do not default to `DicomToNrrd` unless the user requests debugging.
- Import only actual `.dcm` files; ignore sidecars such as `SOURCE_PATH.txt`, `.json`, and `.csv`.
- The DICOM import module name is `DICOM`; do not try nonexistent module names such as `DICOMWidget`, `DICOMUtils`, or `DICOMInstance`.
- After DWI conversion, verify that `.nhdr/.nrrd` is readable and nonempty before passing it to SlicerDMRI modules.
- Use `DWIToDTIEstimation` and `DiffusionTensorScalarMeasurements` for tensor and scalar computation. Map natural-language module names to these two verified module IDs.
- When `run_slicer_module` returns `outputNodes`, subsequent calls must reuse those exact node IDs rather than fuzzy-matching similar scene names.
- DWI and mask inputs for `DWIToDTIEstimation` should come from the same current scene; avoid external mask file paths. Report a blocker directly for geometry mismatches instead of prolonged ad hoc diagnostics.
- Reuse the current scene by default; do not clear/reload the entire scene unless the user explicitly requests it.
- If the user explicitly requests “use Slicer/SlicerDMRI,” do not silently fall back to FSL, DIPY, MRtrix, or a pure shell route unless fallback is explicitly allowed.

### 6.1 Fail-fast rules for common failures (soft constraints, applied by default)

- By default, stop the branch after 2 consecutive failures of the same module with the same parameter semantics; return a structured failure reason and next-step suggestions rather than a long loop of rewriting calls by trial and error.
- Do not rewrite `_processing_code/*.sh|*.py` as a debugging technique in Slicer tasks; return to parameter validation via `run_slicer_module(action='inspect'|'run')`.
- Prefer an explicit subject directory for DICOM import; avoid passing a dataset root or global directory directly to the import step.
- If DICOM import reports failure but the scene already contains a valid DWI node, prefer reusing that node and recording a warning; do not immediately fail the entire stage.
- For `DWIConvert(FSLToNrrd)` parameter/output-type errors, enter the parameter-normalization branch (`inputVolume/outputVolume/outputDirectory`); do not retry the same call indefinitely.
- For `DWIToDTIEstimation` geometry mismatches, prefer terminating the Slicer branch and reporting “DWI/mask spatial mismatch”; avoid further loops of ad hoc diagnostic scripts.
- Launching a new Slicer or directly invoking CLI binaries through `run_shell` remains prohibited in Slicer tasks; however, after repeated structured-route failures, one controlled fallback (for example FSL) is allowed, with its reason and evidence stated explicitly in the summary.

### 6.2 Protocol selection for Slicer tasks (soft constraints, default policy)

- When the task targets “import/display/module calls in the current Slicer session,” use `RUNTIME_ENV` as the default execution protocol context.
- Prefer using pipeline protocols such as `DMRI_METRICS`, `WMA`, and `DDParcel` as algorithm background for these tasks; explicitly record the reason and input evidence before switching to a non-Slicer primary route.

### 6.3 General execution workflow for Slicer tasks (generalized template)

This section gives a general workflow for “complete the task in the current Slicer session.” It aims to improve stability and reuse without hardcoding one specific task.

Scope (examples):
- DICOM/NIfTI import and scene visualization
- DTI scalar computation (FA/MD/Trace)
- Tractography (for example UKFTractography)
- Joint analysis of ROI/segmentation results and tractography

#### Stage A: Confirm inputs and scene baseline (required)
- Identify authoritative inputs: subject root, target DWI, bval/bvec, mask, and output directory.
- Inspect the current scene context first, returning only a summary (node counts, key node types, and key names); exhaustive large enumerations are prohibited.
- Prefer reusing eligible scene nodes that match the current subject; unconditional whole-scene clearing/reloading is prohibited.

#### Stage B: Standardize data entry (default)
- DICOM inputs: import only `.dcm` and filter sidecars such as `SOURCE_PATH.txt`, `.json`, and `.csv`.
- NIfTI + bval + bvec inputs: prefer conversion to NHDR/NRRD (FSLToNrrd) before SlicerDMRI.
- Immediately after conversion/import, verify “nonempty + readable + reasonable dimensions + gradient information present (for DWI).”
- If entry validation fails, prefer reporting the blocker and failing fast; one controlled alternative route may be tried if needed, without repeated retry loops.

#### Stage C: Module inspection and execution (default)
- Inspect before execution: confirm module existence, an available parameter schema, and all required parameters.
- Use the shortest complete execution chain: necessary preprocessing nodes, then core modules, then derived modules.
- Parameter mismatches, missing modules, and format errors are generally non-retryable: prefer ending the branch with next-step suggestions; one controlled fallback may be used if needed for the task.
- Only failures such as connection errors or temporary I/O locks are retryable, with a small retry limit (recommended <=2).

#### Stage D: Node binding and geometric consistency (default)
- Subsequent steps must preferentially reuse explicit output node IDs from the preceding step, without fuzzy name matching.
- DWI and mask must come from the same current scene and share space; prefer terminating and reporting a geometry mismatch.
- Do not inject long loops of ad hoc diagnostic scripts after failures; converge on structured errors.

#### Stage E: Finalize artifacts and visualization (required)
- After module success, complete both:
  - Visible result nodes in the scene (for immediate user inspection)
  - Saved output file paths (for reproducibility)
- `output_paths` must include key artifacts: primary results, supporting results, and logs/intermediate artifacts (when needed).
- Perform lightweight sanity checks (existence, file size>0, matching node types) to avoid false success with empty results.

#### Stage F: Task-level behavioral constraints (default)
- If the user explicitly requests “use Slicer/SlicerDMRI,” prefer the Slicer route by default, without an unexplained switch to FSL/DIPY/MRtrix.
- For a fallback, explicitly record at least its reason, triggering evidence, alternative route, and saved output locations; seek agreement first for interactive scene tasks.
- Do not repeatedly combine exploratory global scans with many read_file/run_shell trials within a stage; prefer confirmed inputs and verified module routes.

#### Stage G: Context and token control (required)
- Summarize tool returns by default: preserve key fields (status/error/output_paths/outputNodes) and limit verbose stdout.
- Deduplicate repeated errors and diagnostics rather than injecting the same failure repeatedly into context.
- Retain only incremental intermediate state for long tasks, without repeatedly reinserting the full history.

#### Recommended minimal execution outline (abstract)
1. Locate authoritative inputs and output roots
2. Inspect and reuse eligible nodes in the current scene
3. Convert if needed: DICOM->DWI or NIfTI+bval+bvec->NHDR/NRRD
4. Inspect the target module and validate parameters
5. Run the core module (optionally chaining subsequent modules)
6. Bind output nodes and save results
7. Return summarized results (status, key outputs, next-step suggestions)

### 6.4 General constraints for ROI segmentation and tractography

This section covers common tasks involving “specified brain-region segmentation + region-related tractography,” emphasizing generalizable constraints without hardcoding a single subject or region.

#### A. ROI semantics and sources
- First identify ROI semantics: a single-label ROI (such as corpus callosum) or a multilabel parcellation (wmparc/parcellation).
- Multilabel parcellations cannot directly serve as tractography seeds. Extract the target label and convert it into a binary seed mask first.
- For an external ROI file, verify spatial consistency with the current DWI before module computation.
- Prefer FreeSurfer LUT definitions for label semantics (common container path: `/opt/freesurfer/FreeSurferColorLUT.txt`), avoiding confusion between an unknown label and the target region.

#### A.1 LUT reference guidance (ROI tasks)
- Before extracting brain-region labels, check the target label's definition in the LUT (for example, common corpus callosum labels).
- If actual unique parcellation labels differ from LUT expectations, report a label-system mismatch and candidate mappings before continuing ROI extraction.
- Do not assume labels are correct from filenames alone (such as `*corpus-callosum*`); validate using the LUT and unique-label statistics.

#### B. Seed/mask quality gate (default)
- Before tractography, check that the seed mask is nonempty, binary, has a reasonable voxel count, and matches the DWI grid.
- If the seed mask contains multiple label values rather than 0/1, binarize the target ROI before continuing.
- If seed voxel counts are abnormally large (suggesting whole-brain or large-region misuse), prefer failing fast with an explicit “ROI selection too broad/incorrect label mapping” message; if needed, use one controlled ROI-reconstruction branch.

#### C. Suggested module-call order (general)
- Recommended order: import/convert DWI -> generate/confirm brain mask -> DTI tensor -> FA/MD and other scalars -> ROI extraction -> tractography.
- If ROI extraction fails, do not continue tractography; stop first and provide reproducible repair suggestions (label values, input paths, coordinate system).

#### D. Naming and node-binding conventions
- Result node names must include the subject and semantic suffix (for example `*_tensor`, `*_FA`, `*_MD`, `*_roi_seed`, `*_tracts`), avoiding reuse of old node names.
- Subsequent steps may reference only node IDs from the most recent successful step in this workflow; do not retrieve identically named nodes from historical sessions.

#### E. Success criteria (required)
- A module returning success alone is insufficient; all of the following must hold:
  - Corresponding result nodes exist in the scene with the correct types;
  - Key output files have been saved and are nonempty;
  - Key intermediate artifacts (seed mask/tensor) pass lightweight sanity checks.
- If any criterion fails, return a structured failure and do not continue chaining later stages.
