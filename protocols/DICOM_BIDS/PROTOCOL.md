---
name: Privacy-preserving DICOM to BIDS Workflow
description: >
  Use this pipeline to discover heterogeneous MRI DICOM trees, convert every
  identifiable patient-study unit with dcm2niix, organize high-confidence series
  as raw BIDS, and retain uncertain series without making unsupported BIDS claims.
version: 1.1.0
type: pipeline
---

# DICOM to BIDS Workflow

## Scope

- Applies to MRI DICOM batches with variable directory structures, multiple participants or examinations mixed in one directory, multiple sequences/repeated acquisitions/reversed phase encoding.
- This Protocol handles only DICOM discovery, dcm2niix conversion, deterministic BIDS organization, privacy cleanup, and structural QC; it does not perform preprocessing, registration, tractography, statistical analysis, or biological interpretation.
- Algorithms and rules reside in this Protocol and `code_templates/convert_dicom_batch.py`, `code_templates/organize_bids.py`, and `code_templates/validate_bids.py`. Do not modify the Planner, Executor, or frontend architecture for this workflow.
- If raw data are incomplete, modality evidence is insufficient, or task/fieldmap associations are missing, preserve the data and return `partial`; do not guess and then claim BIDS completion.

## Input Contract

Required inputs:

- `dicom_root`: an absolute local path; treat the directory as read-only. Arbitrarily nested subdirectories and non-DICOM files are allowed.
- Three new output paths: `converted_root`, `bids_root`, and `qc_root`. Defaults are `<dicom_root>_converted`, `<dicom_root>_bids`, and `<dicom_root>_processing_qc`, respectively.
- `bids_root` and `converted_root` must not exist or must be empty, and must not be inside the original DICOM tree.
- Dataset name; if none is provided, use a generic name without identifying information.

Before execution:

1. Call `software_inventory(refresh=true)` first, then load the automatically refreshed `RUNTIME_LOCAL` and verify the actual absolute path and version of `dcm2niix`; do not use runtime paths from previous sessions or before the refresh.
2. Obtain `agent_runtime.python` (equivalent to `DMRI_AGENT_PYTHON`) from `RUNTIME_LOCAL`; all Python templates in this Protocol must use that absolute interpreter path. Missing Agent-installed dependencies such as `pydicom` indicate an installation-integrity failure and require stopping; do not switch to system/Conda Python or install unrelated algorithm packages.
3. If software is missing, use only `software_manage(action=plan/apply)` and user approval to execute the exact policy declared in the catalog; do not run package managers directly through generic Shell, or use other software as a dependency substitute.
4. Verify available disk space, output locations, read/write permissions, and whether hardlinks are allowed. Default to `copy`; for large batches on the same filesystem, `hardlink` may be used after the user accepts that linked copies cannot be modified independently.
5. Do not write API keys, PatientID, PatientName, original DICOM headers, or patient data into Protocols, memory, or chat summaries.

## Output Contract

Fixed artifacts:

```text
<converted_root>/unit-<subject_token>-<study_token>/input-*/
<bids_root>/dataset_description.json
<bids_root>/participants.tsv
<bids_root>/sub-*/[ses-*/]{anat,dwi,func,fmap}/
<bids_root>/sourcedata/unclassified/       # Present only when sequences remain unclassified
<qc_root>/flatten/manifest.json
<qc_root>/dcm2niix/conversion_manifest.json
<qc_root>/bids/organization_manifest.json
<qc_root>/bids/validator.json
<qc_root>/private/                         # Mode 0700; sensitive local mappings and logs
```

Status definitions:

- `success`: all identifiable input units converted successfully; every converted bundle has one unique destination; no sequences remain unclassified; the official BIDS validator reports no errors, and each warning has been reviewed as a recommendation that does not affect compliance.
- `partial`: all readable artifacts have been preserved, but unit failures, incomplete sidecars, low-confidence classifications, validator errors, or unresolved warnings remain; report evidence for each item.
- `failed`: no readable DICOM, an unexecutable tool, unsafe output roots, a corrupted manifest, a failed privacy check, or inability to produce a verifiable dataset.

File existence or a command exit code of 0 is not a completion criterion. The final response must provide all three output roots, counts for each status, software/Protocol versions, validator results, and evidence for unfinished items.

## Workflow

### 1. Freeze inputs and software state

- Record `dicom_root`, output paths, Protocol revision, the absolute path/version of `dcm2niix`, CPU concurrency, and transfer mode.
- Do not perform an additional `find | wc`, copy, or symlink-based flattening of the entire tree. The conversion template reads each candidate file's DICOM header in one streaming traversal, using `stop_before_pixels=True` without reading pixels.
- For every directory directly containing files, verify Patient/Study identity for all files; do not infer the entire directory from a sample of the first few files. For mixed directories, create temporary numbered symlinks only for the corresponding identity groups, then remove them afterward.

### 2. Privacy-safe conversion

Load and run `code_templates/convert_dicom_batch.py`:

```bash
"$DMRI_AGENT_PYTHON" code_templates/convert_dicom_batch.py \
  --dicom-root <dicom_root> \
  --converted-root <converted_root> \
  --qc-root <qc_root> \
  --dcm2niix <verified_absolute_dcm2niix> \
  --workers <bounded_workers> \
  --compression y
```

The template always uses:

```text
-b y -ba y -i n -f %p_%d_%z_%s_%u_%j
```

- `-ba y` preserves dcm2niix BIDS sidecar anonymization; do not change it to `-ba n`.
- `-i n` retains derived/localizer/2D sequences for explicit decisions later; do not silently discard them for tidiness.
- Use separate directories for each patient-study unit and input group; overwriting identical names is prohibited.
- Default concurrency must not exceed approximately 70% of available CPUs, with a maximum of 8. Record a failed input group and continue other units.

### 3. Deterministic BIDS organization

After conversion, read only the conversion manifest and `<converted_root>`; do not rescan the original DICOM:

```bash
"$DMRI_AGENT_PYTHON" code_templates/organize_bids.py \
  --conversion-manifest <qc_root>/dcm2niix/conversion_manifest.json \
  --bids-root <bids_root> \
  --qc-root <qc_root> \
  --dataset-name <non_identifying_name> \
  --transfer-mode copy
```

Organization rules:

- Use stable HMAC tokens generated with a private local salt for subjects; PatientID/PatientName must not enter BIDS.
- If any subject has multiple Studies, use `ses-*` consistently for the entire batch; number sessions according to the frozen study order.
- Classify a bundle as `dwi/*_dwi` only when complete `.bval` and `.bvec` files are present with matching counts.
- For T1w/T2w/FLAIR and BOLD, read key JSON fields first, then use sequence terms with word boundaries. BOLD must have a reliable `TaskName`; use `task-rest` when resting-state is explicit.
- Encode `PhaseEncodingDirection=i/i-/j/j-/k/k-` as valid `dir-i/ineg/j/jneg/k/kneg`, respectively. Use anatomical direction labels only when AP/PA/LR/RL are explicit in the metadata; do not invent coordinate mappings.
- Stably sort repeated acquisitions with the same subject/session/datatype/entities by AcquisitionTime, SeriesNumber, and path, and use `run-01...`; do not overwrite.
- Place bundles with uncertain classification, incomplete gradients, corrupted JSON, or missing fieldmap associations in `sourcedata/unclassified` using safe filenames, and set the status to `partial`; do not create nonstandard `sub-*/other/` directories.
- Recursively remove direct identity and institutional identifier fields from all sidecars. Keep invalid original JSON only in the permission-restricted conversion/QC workspace; do not copy it into shareable BIDS.

### 4. Official validation and reporting

- Load and run `code_templates/validate_bids.py`; its validator executable argument is always named `--validator`:

```bash
"$DMRI_AGENT_PYTHON" code_templates/validate_bids.py \
  --bids-root <bids_root> \
  --qc-root <qc_root> \
  --validator <verified_absolute_bids_validator>
```

- Run full validation of `<bids_root>` using the official `bids-validator-deno`, save the machine-readable results unchanged to `<qc_root>/bids/validator.json`, and record the validator version, command, exit code, and severity counts in `validator_run.json`.
- Verify conservation of counts across conversion units, bundles, BIDS destinations, and unclassified items.
- Use `DMRI_REPORTING` to organize the conversion manifest, organization manifest, validator JSON, software versions, and limitations; reports must not relabel `partial` as success.

## Quality Control

### Discovery QC

- Record readable DICOM counts, identity-group counts, and mixed-directory splits for every directory directly containing files.
- The conversion unit count must be greater than 0; private mapping must cover all units, with file and parent-directory permissions of 0600/0700, respectively.

### Conversion QC

- Record each input group's return code, private log path, and output inventory; a failure must not prevent other groups from continuing.
- Every DWI bundle must include NIfTI, JSON, bval, and bvec; bvec must have three rows, each with the same number of entries as bval.
- Resolve output filename conflicts through separate input directories and BIDS `run-*`; any overwrite is a failure.

### BIDS QC

- The root directory must contain a parseable `dataset_description.json` with `Name`, `BIDSVersion`, `DatasetType=raw`, and `GeneratedBy`.
- Participant counts, subject counts, session layout, and the organization manifest must agree.
- BIDS filenames may contain only valid entity labels; hyphen-containing labels such as `dir-j-` are a failure.
- Scan BIDS text/JSON for PatientID, PatientName, dates of birth, accession, institutional addresses, and private mapping values; stop publication if any are found.
- Validator errors mean `partial` or `failed` and must not be ignored; explain each warning as arising from the fixture, missing recommended metadata, or an actual protocol defect.
- Warning classification must cite actual evidence from `validator.json`, the conversion manifest, or the organization manifest for each item. Only keys explicitly recorded in `removed_metadata_keys` may be described as “removed during privacy cleanup”; recommended fields absent from BIDS sidecars may only be described as “not provided in the current artifacts/source unverified.” Do not infer that the original DICOM never contained them.

## Failure Boundaries

- May continue: individual non-DICOM files, a failed input-group conversion, low-confidence sequences, or explainable validator warnings. Preserve evidence and return `partial`.
- May retry: explicit tool timeouts or temporary disk/process failures; retry only failed units and record parameters and retry counts.
- Must stop: the original directory would be written to, a nonempty output directory, a path-boundary violation, no DICOM, an unparseable manifest, possible identity leakage, overwriting identical names, insufficient disk space, or an unavailable validator.
- Must ask the user: whether to merge Studies with conflicting evidence, functional images with unknown tasks, fieldmaps missing `IntendedFor/B0FieldIdentifier`, whether to exclude valid sequences, whether to accept hardlinks, or whether to publish data with validator errors.
- Do not switch to sampling merely because a directory is large; limit scope only when the user explicitly requests a smoke test, and label the result as not covering the full dataset.

## Privacy Boundaries

- Keep original DICOM, PatientID/PatientName/StudyUID mappings, absolute source paths, and full tool logs only in `<qc_root>/private` on the current machine. Do not send them to models, remote services, other centers, or public reports.
- Use HMAC tokens with a local salt for BIDS subject/session labels; do not include the salt or mappings in BIDS, Git, Protocols, memory, or shared reports.
- `dcm2niix -ba y` is the minimum requirement; clean JSON again during organization. Remove institutional names, addresses, device serial numbers, and personnel fields by default.
- Use the official browser validator only after confirming that it runs locally without uploading data; default to the local CLI validator.
- In multicenter settings, exchange only approved aggregate statistics, model updates, and de-identified QC; keep original DICOM, private mappings, and individual labels at each center.

## Test Contract

Minimal real success fixture:

- Fixed source: `neurolabusc/dcm_qa_ge` commit `e92133ae414e2b23e8c4d07a4030ab4dbb41518e`.
- `scripts/prepare_dcm2niix_smoke_fixture.py` checks the sizes and Git blob SHA-1 values of 14 files.
- Expect at least one DWI NIfTI/JSON/bval/bvec bundle, one pseudonymous subject, no `sourcedata/unclassified`, and no errors from the official validator.

Automated acceptance:

```bash
"$DMRI_AGENT_PYTHON" scripts/validate_protocol_package.py protocols/DICOM_BIDS
"$DMRI_AGENT_PYTHON" -m pytest -q tests/test_dicom_bids_protocol.py
"$DMRI_AGENT_PYTHON" scripts/prepare_dcm2niix_smoke_fixture.py --target <fixture_root>/input
"$DMRI_AGENT_PYTHON" protocols/DICOM_BIDS/code_templates/convert_dicom_batch.py <required_args>
"$DMRI_AGENT_PYTHON" protocols/DICOM_BIDS/code_templates/organize_bids.py <required_args>
"$DMRI_AGENT_PYTHON" protocols/DICOM_BIDS/code_templates/validate_bids.py <required_args>
```

Failure fixtures must cover mixed Patient/Study identities in one directory, missing bvec, corrupted JSON, repeated sequences with identical names, unknown modalities, nonempty output roots, and simulated identifying fields. Acceptance invariants are: unchanged original-input hashes, no overwrites, conserved counts, no private fields entering BIDS, local failures not interrupting other units, and evidence for every unresolved item.
