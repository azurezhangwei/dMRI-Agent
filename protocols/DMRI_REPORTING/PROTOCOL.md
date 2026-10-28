---
name: dMRI Evidence Reporting Workflow
description: >
  Use this workflow after one or more dMRI processing, QC, group-analysis, or
  federated stages have produced real evidence. It normalizes provenance and
  creates an auditable report; it must not invent missing results or perform
  an unspecified statistical analysis.
version: 1.0.0
type: workflow
---

# dMRI Evidence Reporting Workflow

## Scope

- Use after upstream Protocols have produced files, QC summaries, tool logs, statistics, or federated events.
- Use for single-subject, cohort, multi-center, benchmark, and competition reports.
- This Protocol organizes and verifies existing evidence. It does not choose a new imaging algorithm, repair failed data, reinterpret an unspecified statistical design, or create a success result from file existence alone.
- If a requested report depends on a missing analysis, return the missing evidence as a blocker and load the appropriate processing/analysis Protocol.

## Input Contract

At least one authoritative evidence source is required:

- Session/stage summary with actual tool calls and execution status.
- Input and output manifests with absolute paths.
- Stage-level or cohort/center-level QC tables.
- Statistical result tables with design, contrasts, effect sizes, uncertainty, correction method, and software version.
- Federated round/node events, model hashes, aggregation records, or central-reference metrics.

Before reporting, freeze a report manifest based on `code_templates/report_manifest.schema.json`. Unknown fields remain `null`, empty, or explicitly marked `not_available`; they must not be inferred from filenames or narrative text.

## Output Contract

Default report directory:

```text
<requested_output_root>/dmri_report/
├── report_manifest.json
├── report.md
└── assets/
```

Required completion evidence:

- `report_manifest.json` records report ID, creation time, task, status, Protocol/software versions, inputs, stages, outputs, QC, limitations, and evidence paths.
- `report.md` is generated from the manifest using `code_templates/build_report.py` or an equivalent deterministic renderer.
- Every claimed output or metric has a source path or structured evidence reference.
- Failed, skipped, excluded, and not-run units remain visible in the report.

## Workflow

1. **Evidence inventory**: collect only user-specified and upstream-returned manifests, logs, outputs, QC, statistics, and federated events.
2. **Existence and identity checks**: verify referenced local files; record size/hash when reproducibility requires it. Do not scan unrelated roots.
3. **Manifest normalization**: map evidence into `code_templates/report_manifest.schema.json`; preserve original units and distinguish observations from interpretations.
4. **Deterministic rendering**: load `code_templates/build_report.py` and render `report.md` from the validated JSON manifest.
5. **Report QC**: compare all claims against the manifest and source evidence, check broken paths, missing units, status totals, units, denominators, and unresolved limitations.

## Quality Control

- Counts must reconcile: total units equal successful + failed + skipped/excluded units.
- A stage cannot be `success` when required outputs or required QC are missing.
- Statistics must include population/contrast, sample size, effect estimate, uncertainty or test statistic, multiple-comparison handling, and source evidence.
- Centerized/federated comparisons must state metric definition, reference result, tolerance, random seed/data split, and failed centers.
- Report generation failure never changes upstream processing status.
- Final report QC must record `pass`, `warning`, or `fail` with explicit reasons.

## Failure Boundaries

- Missing optional assets produce a warning.
- Missing required evidence, contradictory counts, unreadable manifests, invalid JSON, broken required paths, or unsupported claims produce failure.
- Do not rerun expensive upstream algorithms unless the user approves and the corresponding Protocol is loaded.
- Do not silently remove failed subjects/centers or recompute statistics with a different design.

## Privacy Boundaries

- Reports must not include API keys, credentials, direct identifiers, raw DICOM headers with identifying fields, or unrestricted absolute paths from another center.
- Never include paths or contents from privacy-restricted evidence areas such as `qc/private/`, nor files named like `subject_mapping`, `patient_mapping`, `*_salt`, or raw-header exports, in `report_manifest.json`, `report.md`, or report assets. A public manifest may report only the aggregate privacy-audit status and non-identifying counts already exposed by an approved upstream manifest.
- When an upstream Protocol marks a source path or mapping as private, replace it in the report with a non-identifying label such as `redacted_local_source`; the reporting manifest's normal absolute-path convention does not override that Protocol's privacy boundary.
- Multi-center reports use approved center IDs and aggregate metrics. Raw images and individual labels remain local unless an explicit compliant transfer policy exists.
- Public/competition reports must use public, synthetic, or confirmed de-identified evidence.

## Test Contract

- Minimum fixture: `code_templates/example_manifest.json`.
- Reproduction command:

```bash
"$DMRI_AGENT_PYTHON" code_templates/build_report.py \
  --manifest code_templates/example_manifest.json \
  --output /tmp/dmri-report-smoke.md
```

- Expected invariants: valid Markdown, exact report ID and stage status, visible QC state, visible limitations, and no unsupported result fields.
- Failure fixtures must cover malformed JSON, absent required manifest fields, inconsistent stage/unit status, and missing required evidence paths.
