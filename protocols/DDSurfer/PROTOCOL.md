---
name: DDSurfer Cortical Surface Reconstruction
description: >
  Use when the user asks for DDSurfer cortical surface reconstruction from
  diffusion MRI: left/right white and pial surfaces in native space, with
  optional surface thickness, curvature, cortical parcellation and regional
  statistics. Also supports standalone DDSurfer surface postprocessing.
  This is surface reconstruction, not DDParcel volumetric brain segmentation.
version: 1.0.0
type: pipeline
---

## Scope
Perform user-authorized DDSurfer cortical surface reconstruction according to the workflow below; this does not replace DDParcel.

## Input Contract
Inputs, software, weights, GPU, and licenses must satisfy the workflow below; software installation is managed by SOFTWARE_MANAGEMENT.

## Output Contract
Preserve input and output paths, software and weight versions, execution status, and QC evidence.

## Workflow
Follow the detailed workflow and code_templates below; execute within the authorized workspace.

## Quality Control
Perform all existing QC checks. Missing software or weights, or failed QC, must not be marked as success.

## Failure Boundaries
If required software or weights are missing, report deployment requirements first; do not silently switch algorithms or automatically download large resources.

## Privacy Boundaries
Keep images and identifiable information local; provide only necessary paths, status, and QC summaries in conversation.

## Test Contract
Use only one minimal representative processing unit by default; deployment and weight downloads require separate confirmation.

# DDSurfer Cortical Surface Reconstruction

Use the installed DDSurfer implementation rather than recreating its tensor,
registration, normalization, neural-network or coordinate-conversion steps.
This protocol was checked against DDSurfer revision `0c399ef`. Verify the
installed source revision and its `--help` before running another version.

## 1. Scope and task selection

- DDSurfer reconstructs cortical **surfaces**, not an anatomical label volume.
  The ordinary output is `lh.white.obj`, `lh.pial.obj`, `rh.white.obj` and
  `rh.pial.obj`. DDParcel's `*-DDSurfer-wmparc.nii.gz` is a different product;
  the shared name does not make its prediction code or weights interchangeable.
- A request for DWI-space volumetric parcellation belongs to `DDParcel`.
  A request for white/pial reconstruction from DWI belongs to `DDSurfer`.
  A complete T1w `recon-all` task belongs to `FreeSurfer`.
- Default to external execution through `execute_stage` with
  `protocol_id="DDSurfer"` and `Execution mode: external`. The upstream pipeline
  launches installed Slicer CLI programs without requiring computation in the
  interactive MRML scene. A request to display surfaces alone does not require
  an in-scene reconstruction.
- Process the subject and data scope supplied by the user. Use explicit input
  paths when directory discovery is ambiguous. Preserve original files and
  previous runs; distinguish newly computed products from verified reuse.
- White/pial reconstruction needs neither a T1w image, DDParcel nor a full
  FreeSurfer reconstruction. Optional surface postprocessing is a separate
  capability and is off by default.

## 2. Runtime and assets

The main entry point is `<DDSURFER_ROOT>/run_ddsurfer_pipeline.py`. Standalone
postprocessing uses `<DDSURFER_ROOT>/postprocessing/pipeline.py`.
`code_templates/run_ddsurfer.sh` calls either entry point and passes backend
arguments unchanged. It accepts an explicit source root and Python interpreter.

The DDSurfer source and a compatible Python environment must be supplied
separately; neither is bundled with dMRI-Agent. Check source, assets, Python
packages and input/output visibility in the actual execution environment.
If unavailable, report the missing deployment and required mount/install;
do not substitute `/opt/DDParcel` or another subject's existing surfaces.

Required components for reconstruction:

- A Python environment satisfying DDSurfer's `requirements.txt`; the inspected
  requirements include PyTorch 2.4.1, torchvision 0.19.1, NumPy, SciPy,
  SimpleITK, nibabel, trimesh and pynrrd. Do not upgrade shared environments
  merely to load this protocol.
- `SLICER_PATH` pointing to the **installation directory** containing `Slicer`,
  with SlicerDMRI `DWIToDTIEstimation` and
  `DiffusionTensorScalarMeasurements`, and core `BRAINSFit` and
  `ResampleScalarVectorDWIVolume` executables available to the backend.
- The matching `weights/manifest.json`, `weights/SHA256SUMS`, hemisphere/surface
  checkpoints, `template/ddsurfer_lh_template.obj`,
  `template/ddsurfer_rh_template.obj` and the registration reference
  `template/100HCP-population-mean-T2-1mm.nii.gz`.

The bundled checkpoint profile requires `--data-type hcp`. This is the backend's
model profile, not a requirement to rename input directories into HCP layout.
Do not bypass manifest, checkpoint or template checks to accept other assets.
`--checkpoint-root` and `--template-dir` select compatible alternative assets;
`DTI_REFERENCE_IMAGE` selects an alternative registration reference, which must
be appropriate for that model's trained geometry.

FreeSurfer is needed only for the requested optional features: `mri_synthstrip`
and its weights for automatic masking, or the FreeSurfer tools, license and
`fsaverage` assets for surface postprocessing. Initialize the relevant runtime
as described by `RUNTIME_ENV` and the installed DDSurfer documentation.

## 3. Input contract

Provide corrected **4D NIfTI DWI**, matching bval/bvec, and a nonempty 3D NIfTI
brain mask on the same voxel grid and physical affine. bvec may be 3xN or Nx3;
volume ordering and gradient directions must remain consistent. Motion,
eddy-current and susceptibility correction with gradient updates should already
be complete; this pipeline does not perform those corrections.

The backend validates gradient counts, finite geometry, a b0 volume
(`b <= 50`), and enough independent directions for a rank-six tensor design.
A tensor, FA map or anatomy image is not a replacement for 4D DWI input.

Prefer explicit `--dwi`, `--bval`, `--bvec` and `--mask` paths. Otherwise
`--raw-input-root` supports bundles such as `<root>/<subject>/dwi.nii.gz`,
`dwi.bval`, `dwi.bvec`, `mask.nii.gz`, or the HCP `T1w/Diffusion/` layout.

If the mask is missing, select a justified preparation route. The upstream
`--auto-mask` option runs SynthStrip on mean b0 with `b <= 50`, on CPU; it
retains `dti/<subject>-brainmask.nii.gz` and `logs/mask.json`. A supplied mask
has priority and is not replaced. This option does not enable postprocessing.
Check anatomical coverage as well as shape/affine, especially before accepting
an automatically generated mask.

## 4. Execution

### 4.1 Native cortical surfaces

After verifying the paths in the chosen runtime, load the execution template
with `load_protocol_resource("DDSurfer", "code_templates/run_ddsurfer.sh")`.
For example, the following paths assume DDSurfer has been installed in the
container; `/opt/DDSurfer` is **not** presently provided by the default image:

```bash
export SLICER_PATH=/opt/Slicer-5.2.2-linux-amd64
bash /workspace/dMRI-Agent/protocols/DDSurfer/code_templates/run_ddsurfer.sh \
  --ddsurfer-root /opt/DDSurfer --python /opt/conda/envs/ddsurfer/bin/python -- \
  --subject sub-001 \
  --dwi /workspace/docker_data/data/sub-001/dwi/sub-001_dwi.nii.gz \
  --bval /workspace/docker_data/data/sub-001/dwi/sub-001_dwi.bval \
  --bvec /workspace/docker_data/data/sub-001/dwi/sub-001_dwi.bvec \
  --mask /workspace/docker_data/data/sub-001/dwi/sub-001_mask.nii.gz \
  --output-root /workspace/docker_data/processed/ddsurfer_run_001 \
  --device cpu --precision fp32 --keep-cache
```

`--output-root` is a **parent**: actual outputs are
`<output-root>/<subject>/`. Do not accidentally append the subject twice.
The workflow is WLS tensor estimation and native scalar export, b0-to-atlas
registration, masking/resampling/normalization of the five inference channels,
bilateral surface prediction, then conversion to native scanner-RAS millimetres.
Use the upstream conversion; do not manually invert or flip its transform.

Select `--device cuda:<index>` only after checking CUDA in the actual Python
environment and free GPU memory. Default precision is FP32 on CPU and CUDA.
CUDA's `--max-gpu-gib` defaults to 24; the inspected preflight requires at least
8 GiB budget plus a 4 GiB free-memory margin. It does not reserve the GPU.
`--precision bf16` is an explicit numerical change on compatible hardware,
not an automatic fallback. Report device, precision and resource choices.
`--cpu-threads` defaults to 4 and `--preprocess-jobs` to 1; inference budgets
do not cap registration or postprocessing. `--predict-mode wm` requests only
the two white surfaces; otherwise `all` produces all four.

### 4.2 Optional surface postprocessing

When thickness, curvature, surface parcellation or regional statistics are
requested, add `--post-process` to the main command. Configure FreeSurfer and
its license first. The main pipeline requires `--predict-mode all` for this
mode. It supports `--postprocess-hemi left|right|both`,
`--postprocess-atlases aparc,aparc.a2009s`, `--postprocess-threads` and
`--postprocess-serial`.

For **existing surfaces**, avoid reconstructing them again. Use the standalone
postprocessor; its flag names differ from the main pipeline:

```bash
bash /path/to/run_ddsurfer.sh --ddsurfer-root /path/to/DDSurfer \
  --python /path/to/python --stage postprocess -- \
  --subject sub-001 --output-root /path/to/new_postprocessing_run \
  --lh-white /path/to/lh.white.obj --lh-pial /path/to/lh.pial.obj \
  --rh-white /path/to/rh.white.obj --rh-pial /path/to/rh.pial.obj \
  --hemi both --atlases aparc,aparc.a2009s --threads 4
```

These surfaces may come from DDSurfer or another supported method. White/pial
pairs must have corresponding vertex indices and topology, in native scanner
RAS millimetres. Do not guess correspondence or treat FreeSurfer surface-RAS
coordinates as scanner RAS. Follow the installed loader's volume-geometry
requirements for FreeSurfer binary surfaces. No DWI or T1w is required for
surface-only postprocessing. Without `--brain-source`, its `mri/` reference is
geometry-only: it is not acquired/synthesized anatomy or evidence of tissue
volumes. The main DWI pipeline normally supplies native b0 as its reference.

### 4.3 Resume, monitoring and recovery

The main pipeline stores concise logs in `<subject-dir>/logs/pipeline.log` and
a completion record in `logs/pipeline.json`. Detailed stage logs are under
`.cache/logs/`. `--keep-cache` retains them and intermediates; otherwise cache
is removed after success. Failure retains cache. Monitor a live process with
the Agent's `exec_poll` and bounded log reads; a slow registration is not a
reason to start a duplicate run. A subject lock prevents concurrent writers.

Use `--resume` with the original inputs/settings to reuse final results only
after the backend validates its run signature and output checksums. If they do
not match, the backend attempts a normal run; it may require a fresh output
directory rather than overwriting incompatible caches. `--skip-preprocessing`
instead requires a retained, compatible preprocessing cache and cannot be
combined with `--resume`. File existence alone is not verified reuse.
Preserve valid surfaces when fixing later display or reporting problems.

## 5. Output verification and scientific QC

Expected core products under `<output-root>/<subject>/`:

- `ddsurfer/lh.white.obj`, `rh.white.obj`, and pial counterparts for `all`.
- `dti/<subject>-FA.nii.gz`, `-MD.nii.gz`, `-MinEigenvalue.nii.gz`,
  `-MidEigenvalue.nii.gz`, `-MaxEigenvalue.nii.gz`, and `-Trace.nii.gz`.
  Native maps are not inference-normalized. Some older retained caches lack
  Trace; identify this explicitly rather than inventing a map.
- `logs/pipeline.json`, `raw_inputs.json`, `dti.json`, `b0ToAtlasT2.tfm`;
  optional `mask.json`. The `.tfm` name alone does not establish its direction;
  the installed native converter documents the atlas-LPS-to-native-LPS pull map.
- Optional `surf/`, `label/`, `stats/`, `fsaverage/`, `mri/` and `logs/qc.json`
  when postprocessing was run. Check the requested hemispheres and atlases,
  not just directory existence.

Load `code_templates/qc_ddsurfer_outputs.py` to check core reconstruction:

```bash
/path/to/python /path/to/qc_ddsurfer_outputs.py \
  --subject sub-001 --subject-dir /path/to/output-root/sub-001 \
  --dwi /path/to/the_actual_input_dwi.nii.gz --predict-mode all \
  --report /path/to/qc/ddsurfer_outputs.json
```

The helper checks completion metadata, surface/scalar hashes, finite triangular
meshes, white/pial topology correspondence and native scalar geometry against
the supplied DWI header. It reports topology, bounds and scalar-range warnings.
It does not certify cortical anatomy, intersection freedom, registration quality
or postprocessing results. Review representative native-reference overlays,
white/pial separation, hemispheric placement, folds and registration. For
postprocessing inspect `logs/qc.json`, requested morphometry/statistics files and
their interpretation separately. Report unresolved findings; never equate four
nonempty files with scientifically valid reconstruction.

Older results may lack the current DTI export or completion records. A failed
integrity check for missing provenance does not prove those surfaces were never
computed. Inspect their version, source logs and actual mesh geometry, report
the verification limitation, and decide whether regeneration is needed for the
requested task. Do not discard valid surfaces solely because an old report is
missing.

## 6. Slicer presentation and final report

Use `mcp_slicer_load_image` for a same-space native FA/MD or anatomical
background. Surface OBJ files are **models**, not volumes or fibre bundles;
the existing image/tractography loaders do not accept them. Use a small scene
glue call through `mcp_slicer_execute_python_code` with
`slicer.util.loadModel(path)` and return model node IDs/classes, source paths
and display state. Prefer existing matching model nodes to duplicate loads.
The current OBJ writer declares `SPACE=RAS`; older unmarked OBJ files require
explicit RAS loading. Do not flip vertices solely to make an overlay look right.
Use distinguishable hemisphere/surface names and appropriate visibility or
opacity, then confirm alignment to the native reference.

Summarize computation versus reuse, method/profile, device/precision, output
paths, loaded nodes and unresolved QC. Executor stages must end with
`Status: success|error|incomplete` for the assigned scope. Standalone
postprocessing does not create a main reconstruction completion receipt;
do not apply the core reconstruction QC helper to that mode.
