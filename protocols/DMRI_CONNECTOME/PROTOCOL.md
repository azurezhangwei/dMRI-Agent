---
name: dMRI Structural Connectome Pipeline
description: >
  Use this protocol when the user asks to construct structural connectivity
  matrices from diffusion MRI. It keeps the WMA-style diffusion preprocessing
  and UKF tractography, adds FreeSurfer T1w parcellation, ANTs-based T1-to-dMRI
  label mapping, and parcel-to-parcel connectome construction in native
  diffusion space.
version: 1.0.1
type: pipeline
---

# dMRI Structural Connectivity Matrix Pipeline Guide

## Scope

Construct structural connectivity matrices from dMRI and T1w inputs. Specific applicability requirements, spatial constraints, and prohibitions are defined in “0. General rules” and the detailed sections below.

## Input Contract

Input data, processing-unit manifests, and required companion files must satisfy “1. Inputs and directory organization”; do not combine inputs across subjects or sessions.

## Output Contract

Output directories, registration artifacts, tractography, and matrix files follow the original output sections and stage definitions; preserve traceable evidence from inputs to matrices.

## Workflow

The numbered sections below define the authoritative stage order; this contract index does not change preprocessing, UKF, FreeSurfer, registration, or connectome-construction algorithms.

## Quality Control

Perform the original protocol's QC for every stage and the final matrix. Do not mark success when spatial mapping, streamline, or matrix QC fails.

## Failure Boundaries

Stop affected units if critical inputs, software, or stage outputs are missing; preserve verified artifacts and report a recovery entry point without silently replacing algorithms or data.

## Privacy Boundaries

Logs, memory, and reports retain only necessary paths, stage status, and aggregate QC. Do not send original images, surfaces, or identifiable metadata to external services.

## Test Contract

Protocol acceptance testing uses one minimal representative unit to verify input discovery, stage handoffs, and lightweight QC; full tractography or multicenter batch processing is not a default test.

# dMRI Structural Connectivity Matrix Pipeline Guide

This guide defines a **diffusion MRI structural connectivity matrix pipeline**. Starting with BIDS-formatted dMRI + T1w inputs, it performs:

1. Basic dMRI preprocessing  
2. Whole-brain UKF tractography  
3. FreeSurfer brain parcellation of T1w  
4. Registration of T1w parcellation labels to dMRI space  
5. Construction of a region × region connectivity matrix from streamline–parcel associations

Separate instructions from scripts:
- This file: workflow rules and how to use templates at each step
- `code_templates/`: reference templates bundled with this protocol; dMRI preprocessing, UKF tractography, FreeSurfer, registration, and connectome templates required here are all maintained in this directory without cross-protocol references

---

## 0. General rules (mandatory constraints)

- Process only the subject specified by the user/plan; do not switch to another subject on your own.
- Use input/output paths given in the plan; do not scan global directories to “pick available data.”
- If directory names and filename prefixes differ or inputs are unclear, first resolve ambiguity using the user-specified data scope, actual file structure, and this protocol's rules; if reliable disambiguation remains impossible, stop the current processing unit/stage and report evidence in the summary.
- Distinguish missing original inputs from derivatives not yet generated. Supported conversion, generation, or registration may use this unit's data; report affected steps if recovery is impossible, without borrowing other subjects' data. See RUNTIME_ENV §7 for general QC, reporting, and recovery guidance.
- First identify the dataset path `<dataset_root>` from the user's request.
- If the user, task, or planner explicitly specifies `processed_root` / `code_root` / `qc_root`, use those paths unconditionally; do not rederive other locations from default rules.
- Use defaults only if no output root is explicitly specified:
  - Use the dataset path's parent as `data_folder`: `<data_folder> = dirname(<dataset_root>)`
  - Place outputs within `data_folder`, named from the dataset directory `basename(<dataset_root>)`:
    - `<processed_root> = <data_folder>/<dataset_name>_processed`
    - `<code_root> = <data_folder>/<dataset_name>_processing_code`
    - `<qc_root> = <data_folder>/<dataset_name>_processing_qc`
- Do not replace path separators with underscores before assembling directories.
- The layouts below use BIDS. For other existing image layouts, record the actual relationships among dMRI, gradients, anatomical images, and metadata; do not reorganize data merely for naming consistency. For DICOM input, refer to `DICOM_BIDS` for necessary conversion.
- The default FreeSurfer node scheme requires the corresponding anatomical parcellation: generating it from scratch requires T1w; an existing traceable parcellation matching the target node definitions and spatial transforms may be checked for suitability first. Report a connectome blocker if these prerequisites are absent; other independent steps may still finish. If the user explicitly chooses another atlas, plan according to its input and spatial requirements without silently changing the node scheme.
- Default nodes come from **FreeSurfer `aparc+aseg`**; do not change node definitions unless the user explicitly specifies another atlas.
- Identify this batch's **processing units** first:
  - For `sub-*/dwi`, the unit is `subject`
  - For `sub-*/ses-*/dwi`, the unit is `subject-session`
- Shared placeholder conventions:
  - `<unit_root>` = `sub-<id>` or `sub-<id>/ses-<id>`
  - `<unit_id>` = `sub-<id>` or `sub-<id>_ses-<id>`
- Once inputs use a `ses-*` layout, every subsequent stage must treat `sub-*/ses-*` as an independent unit; do not silently merge multiple sessions into one subject or ignore `ses-*` and process only the `sub-*` root.
- For batch tasks, create the subject/session manifest first and reuse it while updating status, without rescanning it at every stage. A single unit may use explicit input mappings; added targets must remain within the user's scope.

Parallelization and resource guidance:
- Independent units may run in parallel; select concurrency and per-task threads according to CPU, memory, GPU, I/O, and existing tasks. Single-unit or resource-limited tasks may run serially without first causing OOM or parallel failures.
- Keep independent outputs and logs per unit and avoid duplicate launches; report actual scheduling and unit status in batch reports.
- Runtime varies with input size, tracking parameters, registration, and hardware. Assess progress from processes, logs, and output updates rather than inferring failure from a fixed elapsed time.

---

## 1. Inputs and directory organization

### 1.1 Expected inputs

- dMRI：
  - `sub-*/dwi/sub-*_dwi.nii.gz` + `.bval` + `.bvec` + `.json`
  - Or `sub-*/ses-*/dwi/sub-*_ses-*_dwi.nii.gz` + `.bval` + `.bvec` + `.json`
- T1w：
  - `sub-*/anat/sub-*_T1w.nii.gz`
  - Or `sub-*/ses-*/anat/sub-*_ses-*_T1w.nii.gz`
- If `ses-*` is present, treat every `sub-*/ses-*` as an independent unit; do not revert to subject-only processing later.

### 1.2 Processing-unit manifest (required)

- Before any preprocessing, enumerate and save all valid units, for example:
  - `<qc_root>/processing_units.tsv`
  - Or `<qc_root>/processing_units.json`
- The manifest must include at least:
  - `unit_id`
  - `unit_root`
  - `subject_id`
  - `session_id` (may be empty if there is no session)
  - `dwi_path`
  - `t1w_path`
- From this stage onward, the `processing_units` manifest is the sole basis for downstream processing, parallel scheduling, and QC reconciliation.

### 1.3 Processed directory structure (suggested)

```text
<processed_root>/<unit_root>/
├── anat/
│   └── FreeSurfer/
│       └── <unit_id>/
│           ├── mri/
│           ├── label/
│           ├── stats/
│           └── scripts/
├── dwi/
│   ├── <raw_dwi_symlinks>
│   ├── <subject>_dwi_mask.nii.gz
│   ├── brain_mask/
│   ├── denoise/
│   ├── gibbs/
│   ├── topup/
│   ├── eddy/
│   └── tractography/
├── registration/
│   ├── mean_b0.nii.gz
│   ├── T1w_in_dwi.nii.gz
│   ├── aparc+aseg_in_dwi.nii.gz
│   ├── T1w_to_dwi_0GenericAffine.mat
│   ├── T1w_to_dwi_1Warp.nii.gz
│   ├── T1w_to_dwi_1InverseWarp.nii.gz
└── connectome/
    ├── connectome_count.csv
    ├── connectome_length_mean.csv
    ├── connectome_node_table.tsv
    ├── assignment_summary.json
    └── connectome_qc.md
```

### 1.4 Stage names

- `brain_mask`
- `denoise`
- `gibbs`
- `topup`
- `eddy`
- `tractography`
- `freesurfer`
- `registration`
- `connectome`

---

## 2. Basic dMRI preprocessing and UKF tractography

Default algorithms, templates, and execution constraints for this stage are fully defined in this protocol; do not jump to other protocols for equivalent steps or invent additional default algorithms.

### 2.1 Shadow directory and default strategy

- Create new directories only under `<processed_root>/<unit_root>/dwi/`.
- Symlink only original DWI files, not the entire original subject directory.
- Unless the user specifies otherwise, run only necessary steps by default: **brain mask + topup (depending on the data) + eddy**.
- Run `denoise` and `gibbs` only when explicitly requested or genuinely required by data quality.
- Use the corresponding templates in this protocol's `code_templates/`, first confirming support for `<unit_root>` in templates and batch scripts; do not assume all inputs are in `sub-*/dwi/`.

### 2.2 Default algorithms

- brain mask：DIPY `median_otsu`
- denoise：MRtrix `dwidenoise`
- gibbs：DIPY `gibbs_removal`
- topup：FSL `topup`
- Eddy: FSL `eddy` (prefer the GPU version)
- tractography：UKF tractography

Mandatory `topup` eligibility constraints:
- Run only with a standard reverse-PE pair on the same axis with opposite polarities, for example `j/j-`, `i/i-`, or `k/k-`.
- Cross-axis combinations such as `i- + j` or `j + k-` do not meet `topup` requirements by default; record `SKIP/unsupported` without running it.

### 2.3 Templates directly available in this protocol

- `code_templates/brain_mask_median_otsu.py`
- `code_templates/denoise_mrtrix_dwidenoise.sh`
- `code_templates/gibbs_removal_dipy.py`
- `code_templates/topup_fsl.sh`
- `code_templates/make_acqp_from_bids_json.py`
- `code_templates/make_index_txt.py`
- `code_templates/eddy_fsl.sh`
- `code_templates/mask_cast_to_uint8_nrrd.py`
- `code_templates/nhdr_write_help.txt`
- `code_templates/ukf_tractography.sh`
- `code_templates/ukf_tractography_parallel_units.sh`

Mandatory constraints:

- If a corresponding template exists for a stage, load and use it before deciding whether minimal outer scheduling code is needed.
- Do not rewrite NHDR generation, mask NRRD conversion, UKF invocation, eddy invocation, or topup invocation when working templates already exist.
- For `tractography`, prefer `code_templates/ukf_tractography_parallel_units.sh` for batch scheduling by default; if not using it, at least use `code_templates/ukf_tractography.sh` as each unit's command body.
- Write a minimal repair wrapper only after actually loading and test-running the template and obtaining specific failure evidence; do not skip templates and write a complete replacement first.

### 2.4 Eddy template and parallel execution requirements (mandatory)

- `code_templates/eddy_fsl.sh` is the per-unit eddy command body: it generates `acqp.txt` from actual JSON and `index.txt` from volume counts, then invokes FSL eddy.
- Schedule independent subjects/sessions around `code_templates/eddy_fsl.sh`, choosing concurrency and GPU assignment according to resources; the whole batch need not start simultaneously.
- If `eddy_cuda11.0` fails with `cudaErrorNoDevice`, unavailable NVML, or other GPU initialization errors, CPU `eddy` is allowed; CPU fallback may use subject/session-level parallelism with `OMP_NUM_THREADS` controlling total CPU load.
- Do not hardcode `EDDY_CMD="eddy"` as the default. Prefer automatic `eddy_cuda11.0` selection by `eddy_fsl.sh`, or explicitly select `eddy_cuda11.0` after verifying the GPU. Set `EDDY_CMD="eddy"` only with evidence that GPU execution is unavailable.
- DWI input selection must exclude masks, topup intermediates, and eddy outputs; do not blindly use `glob("*.nii.gz")[0]`. Select original or preprocessed DWI with matching `.nii.gz/.bval/.bvec/.json`.
- An eddy failure for one unit must not block other units; record its reason, continue others, and report it in stage QC and the final summary.

Implementation guidance: reuse the per-unit template with a concurrency-limited batch scheduler; pass verified input mappings, available GPUs, and independent log paths. Do not background all tasks without a limit or hardcode GPU IDs or batch size.

Stage checks (required):

- Expect eddy-corrected data suitable for tractography: corrected DWI, corresponding rotated gradients, and logs explaining execution.
- Confirm successful `acqp.txt` and `index.txt` generation with matching volume counts.
- Confirm eddy outputs (corrected DWI, rotated bvec, logs) exist.
- Record planned and actual concurrency, each unit's `EDDY_CMD`, `CUDA_VISIBLE_DEVICES`, or CPU-fallback reason.
- Record actual execution and the resource rationale; single-unit or resource-limited tasks may run serially.

### 2.5 Tractography template requirements (mandatory)

- `code_templates/ukf_tractography.sh` prepares NHDR and mask NRRD and invokes UKF for one unit.
- `code_templates/ukf_tractography_parallel_units.sh` performs actual subject/session-level parallel scheduling from the `unit_root` manifest.
- For batch execution, load and use `code_templates/ukf_tractography_parallel_units.sh` by default.
- If input layout or manifest format requires a lightweight outer wrapper around the batch template, that wrapper may only:
  - Pass `<processed_root>`
  - Pass the `unit_root` / `unit_id` manifest
  - Set concurrency and thread parameters
  - Collect logs and QC
- That wrapper must not rewrite NHDR, NRRD, UKF executable paths, LD_LIBRARY_PATH, or input-preparation logic inside the template.

### 2.6 Stage checks (required)

- `brain_mask`: expect a nonempty mask covering the main brain parenchyma in DWI space; verify existence, matching dimensions, and nonemptiness.
- `topup`: if run, expect field estimates and reference outputs suitable for distortion correction; verify valid `acqp.txt` and existing `topup_results`.
- `eddy`: expect corrected DWI, corresponding rotated gradients, and logs suitable for tractography; verify corrected DWI, rotated bvec, and logs exist, with `acqp/index` matching volume counts.
- `tractography`：
  - Expect a clearly nonempty whole-brain tractogram; common results are relatively large VTK/VTP files, typically hundreds of thousands to millions of streamlines rather than near-empty small bundles.
  - The output tractogram exists and is nonempty.
  - For batch processing, prefer `code_templates/ukf_tractography_parallel_units.sh`; do not revert to per-unit serial execution without evidence.
  - Record tract file size, total streamline count (if readable), key parameters, and output paths.
  - Record actual subject concurrency, within-subject thread count, and whether subject-level parallel scheduling was actually enabled.
  - Record concurrency and thread settings without requiring a fixed concurrency.
  - For batch processing, compare tract file sizes and streamline counts across the cohort; a subject with both a clearly small tract file and clearly few streamlines must be flagged as suspicious in QC.
  - Reconcile against `processing_units`: report `expected_units / processed_units / failed_units / missing_units`, not subject-only counts.

---

## 3. FreeSurfer T1w brain parcellation

### 3.1 Default task

- This pipeline requires FreeSurfer primarily to **obtain brain parcellations for connectome node definitions**, especially `aparc+aseg`, rather than all downstream cortical reconstruction products.
- Default strategy: **prefer the minimal necessary workflow that reliably produces `aparc+aseg`**; do not expand scope solely for extra surface outputs unrelated to the connectome.
- If trimming FreeSurfer stages would significantly increase implementation complexity, stability risk, or environmental uncertainty, fall back directly to **`recon-all -all`** rather than sacrificing robustness for a lighter workflow.
- If a subject has multiple T1w images, do not silently merge or average them; explicitly identify which T1w is used.

### 3.2 Selecting the minimal necessary workflow

- A smaller workflow may be used when the executor knows which stages reliably produce `aparc+aseg` in the current environment.
- If uncertain, or concerned that trimming stages could leave `aparc+aseg` missing, incomplete, or incompatible with node definitions, use `recon-all -all` directly.
- Thus, **prefer segmentation-only over full surface reconstruction where possible, but prioritize robustness over time savings.**
- For `ses-*` input layouts, run FreeSurfer independently for each `<unit_root>`; do not share one FreeSurfer output directory across a subject's sessions.

### 3.3 Parallel execution and process status

- Independent subjects/sessions may run FreeSurfer in parallel; choose concurrency based on per-task resources without a fixed batch size.
- If a wrapper exits, logs are empty, or lock files exist, check actual `recon-all` processes, log paths, and task status before relaunching. A lock may indicate an active task and must not be deleted merely to rerun.
- Handle scheduling failures, individual input failures, and resource shortages separately; retain completed stages and reduce concurrency or run serially as appropriate.

### 3.4 Execution location

- Suggested `SUBJECTS_DIR`: `<processed_root>/<unit_root>/anat/FreeSurfer`
- Default FreeSurfer subject id: `<unit_id>`

### 3.5 Key outputs

At minimum:

- `mri/aparc+aseg.mgz`
- `mri/brain.mgz`
- `mri/T1.mgz`
- `stats/aseg.stats`

Reference templates:

- `code_templates/recon_all.sh`
- `code_templates/export_aparc_aseg.sh`

Stage checks (required):

- Expect complete FreeSurfer segmentation artifacts suitable for node definitions: at least a skull-stripped T1, `aparc+aseg`-type labels, and basic segmentation statistics.
- FreeSurfer must finish successfully; do not proceed to registration before it finishes.
- `aparc+aseg.mgz`, `brain.mgz`, and `T1.mgz` exist.
- `stats/aseg.stats` exists, indicating basic completeness of segmentation artifacts.
- If using a minimal necessary workflow instead of `recon-all -all`, additionally confirm reliable `aparc+aseg` generation; otherwise immediately fall back to a more complete workflow rather than passing incomplete segmentation downstream.
- Record the FreeSurfer input T1w path, SUBJECTS_DIR, and primary output paths.
- Record planned and actual FreeSurfer concurrency, per-subject thread counts, and any concurrency reduction.
- Reconcile against `processing_units`; do not process only one session merely because a subject has `ses-*` directories.

---

## 4. T1w → dMRI registration and label mapping (ANTs)

This is one of the most critical connectome steps. Only label maps **in the same dMRI space as tractography** may be used to construct the final connectivity matrix.

### 4.1 Fixed and moving images

- Fixed: **post-eddy mean b0** or another low-b reference in the same space
- Moving: T1w (prefer a skull-stripped T1 obtained by converting FreeSurfer `brain.mgz` to NIfTI)
- Moving labels: FreeSurfer `aparc+aseg` (export to NIfTI first)

### 4.2 Default registration strategy

- Default to **rigid + affine** first
- Consider nonlinear deformation only when rigid + affine is clearly insufficient and the user permits more complex transforms
- Use **ANTs** for registration

### 4.3 Label resampling rules (mandatory constraints)

- **Use nearest-neighbor resampling** to map `aparc+aseg` to dMRI space
- Do not use linear, BSpline, or other continuous interpolation on discrete label maps

### 4.4 Required artifacts

Suggested outputs:

- `<processed_root>/<unit_root>/registration/mean_b0.nii.gz`
- `<processed_root>/<unit_root>/registration/T1w_in_dwi.nii.gz`
- `<processed_root>/<unit_root>/registration/aparc+aseg_in_dwi.nii.gz`
- `<processed_root>/<unit_root>/registration/T1w_to_dwi_0GenericAffine.mat`
- `<processed_root>/<unit_root>/registration/T1w_to_dwi_1Warp.nii.gz` (if nonlinear registration is used)

Reference templates:

- `code_templates/register_t1_labels_to_dwi.sh`

Stage checks (required):

- Expect T1 and label images in dMRI space, with discrete label boundaries and reasonable overall positioning, suitable for direct streamline endpoint assignment.
- Transform files exist.
- `aparc+aseg_in_dwi.nii.gz` dimensions, spacing, and affine match the dMRI reference.
- The transformed label map is nonempty with a reasonable fraction of nonzero labels; fail if it is nearly all 0 or clearly out of bounds.
- Perform at least one quick overlay check (mean b0 / transformed T1 / transformed labels) for obvious left-right flips or global displacement.
- Reconcile against `processing_units`; individually list any `sub-*/ses-*` missing registration outputs rather than summarizing only by subject.

---

## 5. Node definition

### 5.1 Default node source

- Default to FreeSurfer `aparc+aseg`
- Retain both cortical and subcortical structures
- Use the fixed node table bundled with this protocol by default:
  - `references/aparc_aseg_connectome_nodes.csv`
- The table defines a fixed project-level set of 84 nodes:
  - 68 Desikan cortical nodes
  - 16 commonly used subcortical nodes
- Unless the user explicitly supplies another node table, do not dynamically change node membership, ordering, or numbering according to labels present in an individual subject.

### 5.2 Default exclusions

- Background `0`
- `Unknown`
- Non-brain/non-parenchymal labels clearly unsuitable as connectome nodes, such as CSF, ventricles, choroid plexus, and vessels

### 5.3 Node table (required)

Load and save the fixed node table before matrix construction. By default, use:

```text
references/aparc_aseg_connectome_nodes.csv
```

Requirements:

- The resource uses CSV by default for human inspection, sorting, and maintenance.
- Default resource columns:
  - `node_index, fs_label, node_name, hemisphere, category, include`

```text
node_index,fs_label,node_name,hemisphere,category,include
1,10,lh_thalamus,lh,subcortex,1
2,11,lh_caudate,lh,subcortex,1
...
```

Also copy/save the node table actually used into the result directory, for example:

```text
connectome_node_table.tsv
node_index  fs_label  node_name  hemisphere  category  include
1           10        lh_thalamus  lh        subcortex 1
2           11        lh_caudate   lh        subcortex 1
...
```

Requirements:

- Default node ordering must match the protocol's bundled CSV; do not reorder by labels present in one subject.
- If a subject lacks nodes, retain their matrix rows/columns as 0; do not reduce matrix dimensions.
- Save the mapping from original FreeSurfer label IDs to final node indices.
- If the user requires different node definitions, override the default table with a new explicit node-table file rather than temporary hardcoding.

---

## 6. Connectivity matrix construction

### 6.1 Core inputs

Before this stage, the following must exist:

1. Brain labels in dMRI space: `aparc+aseg_in_dwi.nii.gz`
2. UKF tractography in the same dMRI space

Default connectomes contain **one matrix per processing unit**:
- For `subject` units, produce one matrix per subject
- For `subject-session` units, produce one matrix per `sub-*/ses-*`
- Do not silently merge sessions into one matrix unless the user explicitly requests cross-session aggregation

### 6.2 Coordinate-system rules (mandatory constraints)

- Confirm that tractography point coordinates and label sampling use the same physical-space definition.
- If streamline points use world/RAS coordinates, transform them into voxel coordinates using the label affine before sampling.
- Do not use world coordinates directly as voxel indices.

### 6.3 Default assignment strategy

For each streamline:

1. Take endpoints `p0` and `p1`
2. Query the labels at the endpoint voxels directly in the dMRI-space label map
3. If an endpoint has 0/an invalid label, perform a **2-4 mm radial search**
4. If still invalid, perform a **reverse search** from that endpoint into the streamline
5. Count the streamline in the connectivity matrix only when both endpoints have valid nodes
6. Otherwise mark it `unassigned`

Default principles:

- `endpoint direct lookup` is the first step
- `radial search fallback` is the default recovery strategy
- `reverse search` is the second recovery layer; do not skip direct lookup at the outset

### 6.4 Edge-weight definitions

Output at least one matrix by default:

- **Streamline count matrix**: each streamline assigned to `(i, j)` increments `M[i, j] += 1`

Recommended additional outputs:

- **mean streamline length matrix**

If explicitly requested, also output:

- FA / MD / QA or other scalar-weighted matrices

### 6.5 Self-connections and symmetry

- Construct a symmetric matrix by default: `M[i, j] = M[j, i]`
- Self-connections with `l0 == l1` may first be counted in the raw matrix; if not needed downstream, additionally export a zero-diagonal version

Reference templates:

- `code_templates/build_connectome_matrix.py`

Implementation guidance:

- Prefer reading `references/aparc_aseg_connectome_nodes.csv` and using it as the fixed `node_index -> fs_label` mapping.
- Use other node-definition files only when the user explicitly specifies a replacement table.

---

### 6.1 Tracking coverage and node schemes

- A whole-brain connectome requires corresponding whole-brain tracking coverage. A tractogram seeded from one ROI or restricted to one target tract may support limited-scope connectivity analysis, but a square matrix alone does not make it a whole-brain connectome.
- If one task requests both a specific tract and a whole-brain connectome, explain the tracking and filtering source for each output separately. Suitable whole-brain tracking may be reused, but do not present a local ROI result as whole-brain input.
- The default node table corresponds only to this protocol's default atlas. If another atlas is selected, create its explicit label-to-node mapping and check background, missing labels, and physical space instead of assuming the default node count.

## 7. Connectivity matrix stage QC (required)

### 7.1 Per-subject QC

Record and save:

- Expect an interpretable connectome result package containing at least a node table, square connectivity matrix, and assignment-quality summary; a normal result should not be an almost all-zero empty matrix.
- Total tractography streamline count
- Number of streamlines successfully assigned to two nodes
- `assigned_rate = assigned / total`
- `unassigned_rate = unassigned / total`
- Number of nonzero edges
- Presence of large all-zero rows/columns
- Matrix symmetry
- Output matrix and node-table paths

### 7.2 Batch cohort-level QC

For a subject batch, perform cohort-level numerical checks during the `connectome` stage:

- Each subject's tract file size
- Each subject's total streamline count
- Each subject's `assigned_rate`
- Each subject's nonzero edge count
- Each subject's total matrix edge weight (for example total streamline count)

Flag these abnormalities in particular:

- Clearly small tract files together with clearly few streamlines
- Normal streamline counts but abnormally low `assigned_rate`
- Abnormally few nonzero edges, making the matrix too sparse to use

If a subject simultaneously has “a small tract file + few streamlines + few nonzero connectome edges,” its tractography or registration is usually problematic; explicitly state this in the QC report.
- Reconcile every stage and final cohort QC fully against `processing_units`, reporting:
  - `expected_units`
  - `processed_units`
  - `failed_units`
  - `missing_units`
- If a `sub-*/ses-*` was not processed, report the exact processing unit instead of a vague “no data for this subject.”

---

## 8. Directory structure (protocol bundle)

```text
protocols/DMRI_CONNECTOME/
  PROTOCOL.md
  code_templates/
    brain_mask_median_otsu.py
    denoise_mrtrix_dwidenoise.sh
    gibbs_removal_dipy.py
    topup_fsl.sh
    make_acqp_from_bids_json.py
    make_index_txt.py
    eddy_fsl.sh
    mask_cast_to_uint8_nrrd.py
    nhdr_write_help.txt
    ukf_tractography.sh
    ukf_tractography_parallel_units.sh
    recon_all.sh
    export_aparc_aseg.sh
    register_t1_labels_to_dwi.sh
    build_connectome_matrix.py
  references/
    aparc_aseg_connectome_nodes.csv
```
