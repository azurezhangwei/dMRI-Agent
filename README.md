<div align="center">

# dMRI-Agent

**Protocol-driven diffusion MRI workflows in 3D Slicer**

[![Python](https://img.shields.io/badge/Language-Python-3776AB?logo=python&logoColor=white)](#key-dependencies)
[![3D Slicer](https://img.shields.io/badge/Interface-3D_Slicer_5.6%2B-58728B)](#installation)
[![Qt](https://img.shields.io/badge/GUI-Qt_%2B_PythonQt-41CD52?logo=qt&logoColor=white)](#technology-stack)
[![FastAPI](https://img.shields.io/badge/Backend-FastAPI-009688?logo=fastapi&logoColor=white)](#technology-stack)
[![LangChain](https://img.shields.io/badge/Model_Adapters-LangChain-1C3C3C?logo=langchain&logoColor=white)](#technology-stack)
[![pydicom](https://img.shields.io/badge/DICOM-pydicom-3776AB)](protocols/DICOM_BIDS/PROTOCOL.md)
[![License](https://img.shields.io/badge/License-MIT-4D7587)](LICENSE)

[Overview](#overview) | [Installation](#installation) | [Quick Start](#quick-start) | [Protocols](#protocols) | [Dependencies](#key-dependencies) | [Development](#development) | [License](#license)

</div>

dMRI-Agent connects natural-language requests to local neuroimaging tools and
the active [3D Slicer](https://www.slicer.org/) scene. Reusable **Protocols**
define workflow steps, input requirements, quality control and failure
boundaries; the Agent inspects data, coordinates execution and summarizes
the evidence.

**Describe the task. Review the actions. Inspect the results in Slicer.**

Research software. Not intended for clinical use. **3D Slicer is the only
frontend in this release.**

---

## Overview

| Capability | What it provides |
| --- | --- |
| Conversational workflows | Inspect inputs, select a Protocol and coordinate the requested processing steps. |
| Protocol-guided execution | Load workflow instructions and command templates instead of reconstructing a pipeline from scratch. |
| Slicer scene integration | Inspect scene nodes, load supported outputs and review images, surfaces and tractography in the same application. |
| Review and control | Review plans and requested tool approvals; use the stop control to request task cancellation. |
| Evidence and QC | Record execution locally and report output paths, checks, failures and limitations. |
| Provider selection | Configure DeepSeek, OpenAI, Qwen (Beijing) or Anthropic Claude from Settings. |

```text
Task in Slicer Chat
  -> input and environment checks
  -> Protocol selection and action review
  -> local tools / bounded Executor stages / Slicer scene operations
  -> output inspection, QC and summary
```

The Main Agent handles conversation and short tasks directly. Longer pipeline
stages can be delegated to a scoped Executor. Processing may run through
external tools; loading a result into Slicer does not require computing it
inside the interactive scene.

**The Agent coordinates existing methods; it does not bundle every processing
tool, atlas or model.** See [dependencies](#key-dependencies) before running
an imaging workflow.

---

## Installation

### Get dMRI-Agent

Install **3D Slicer 5.6 or newer**, then choose either route:

- **Release package:** download `dmri-agent-<version>.zip` from
  [Releases](https://github.com/azurezhangwei/dMRI-Agent/releases) and extract it.
- **Source checkout:** clone the repository:

```bash
git clone https://github.com/azurezhangwei/dMRI-Agent.git
cd dMRI-Agent
```

### Install the Plugin

**Close Slicer first.** From the extracted package or repository root, run:

| Platform | Command |
| --- | --- |
| macOS / Ubuntu / Linux | `bash ./install.sh` |
| Windows, 64-bit PowerShell | `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1` |

The same ZIP contains both platform entrypoints. The installer prepares the
Agent's private Python environment, downloads a compatible Python runtime when
needed, and registers the Slicer plugin. **Node.js and npm are not required.**

First installation requires internet access. Keep the managed application
folder created by the installer; the downloaded package can be removed after
installation.

If automatic Slicer discovery fails, specify the installation explicitly:

```bash
# macOS / Linux; replace the path with your Slicer executable or .app.
bash ./install.sh --slicer /absolute/path/to/Slicer
```

```powershell
# Windows; replace the path with your Slicer executable.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Slicer "C:\path\to\Slicer.exe"
```

Installing the plugin prepares the Agent, **not all neuroimaging workflows**.
External software, licenses, atlases and weights must be configured separately.
Platform support for an individual workflow depends on those tools; Bash
templates require a compatible execution environment on Windows.

---

## Quick Start

1. Open Slicer and select **Modules > Diffusion MRI > dMRI-Agent**.
2. Select a provider in the top toolbar. Open **Settings**, enter the API key
   and model name, then click **Save & Test**. Use **Get model list** to discover
   model names; fetching the list does not automatically change the selection.
3. Choose a **Workspace** in Settings or **Files > Workspace > Choose...**.
   Both controls use the same folder. Expand Files to browse or search files;
   **Insert into Prompt** inserts the selected file's path into Chat.
4. Describe the task, review the requested approvals and inspect the outputs.
   Leave **Auto approve** unchecked when you want to review requests manually.

### Start with Input Inspection

```text
Inspect the diffusion MRI files in my workspace and check whether they are
suitable for FA estimation. Report missing inputs and required software.
Do not start processing yet.
```

### Run a Bounded Analysis

After preparing the required tools and inputs, adapt this example:

```text
Use the DMRI_METRICS Protocol to compute FA and MD for subject sub-01 in my
workspace. Show me the proposed steps and wait for confirmation. Save new
results in a separate output folder, load the maps into Slicer, and summarize
the output paths and QC checks.
```

Use the exact subject and paths for your dataset. DWI, gradients and masks must
meet the selected Protocol's input contract; there is no single directory
layout that fits every workflow.

Changing Workspace starts a new chat while retaining the scene and unsent
draft. Stop an active task before changing its workspace.

---

## Protocols

A Protocol is a local workflow package: `PROTOCOL.md` specifies scope, inputs,
outputs, processing rules and QC; supporting resources provide command
templates. The Agent receives a registry of Protocol IDs, names and
descriptions, selects the relevant entry, and loads its instructions and
resources as needed. You can request a specific Protocol by name.

The repository includes **11 Protocol packages**:

| Area | Protocol | Workflow scope |
| --- | --- | --- |
| Data organization | [DICOM_BIDS](protocols/DICOM_BIDS/PROTOCOL.md) | Privacy-aware DICOM conversion and BIDS organization. |
| Diffusion metrics | [DMRI_METRICS](protocols/DMRI_METRICS/PROTOCOL.md) | Tensor-derived FA, MD, AD and RD maps with input and output checks. |
| Brain parcellation | [DDParcel](protocols/DDParcel/PROTOCOL.md) | Brain region segmentation in diffusion space. |
| Cortical surfaces | [DDSurfer](protocols/DDSurfer/PROTOCOL.md) | White and pial surface reconstruction from dMRI; optional surface postprocessing. |
| Tractography and bundles | [WMA](protocols/WMA/PROTOCOL.md) | Diffusion preprocessing, UKF tracking, bundle segmentation and measurements. |
| Structural connectomes | [DMRI_CONNECTOME](protocols/DMRI_CONNECTOME/PROTOCOL.md) | Parcellation, tractography and connectivity matrix construction. |
| Group analysis | [TBSS](protocols/TBSS/PROTOCOL.md) | FSL skeleton-based diffusion analysis; statistics when valid design files are supplied. |
| Structural MRI | [FreeSurfer](protocols/FreeSurfer/PROTOCOL.md) | T1-weighted reconstruction, segmentation and selected processing stages. |
| HCP preprocessing | [HCPPipeline](protocols/HCPPipeline/PROTOCOL.md) | HCP-style structural and diffusion preprocessing. |
| Evidence reporting | [DMRI_REPORTING](protocols/DMRI_REPORTING/PROTOCOL.md) | Evidence manifests and reports grounded in recorded outputs and QC. |
| Runtime guidance | [RUNTIME_ENV](protocols/RUNTIME_ENV/PROTOCOL.md) | Supplementary software inventory and environment guidance, not a processing pipeline. |

**A listed Protocol is not a preinstalled algorithm.** Check its requirements
before execution. For example, DDSurfer surface reconstruction and DDParcel
volumetric parcellation are different tasks with different outputs and assets.

Output locations and completion checks follow the selected Protocol and the
approved task. Inspect the generated files and QC evidence rather than treating
a successful tool return or a scene load as proof of a valid analysis.

---

## Technology Stack

| Layer | Components | Role |
| --- | --- | --- |
| Slicer interface | 3D Slicer, Qt/PythonQt, MRML | Chat, Files, approvals and scene interaction. |
| Local Agent service | Python, FastAPI, HTTP/SSE | Sessions, streamed events and frontend/backend communication. |
| Model adapters | LangChain, OpenAI-compatible and Anthropic-native clients | Provider-specific model requests and tool interactions. |
| DICOM metadata | pydicom | Read DICOM metadata in the conversion workflow. |
| Workflow guidance | Protocol registry, Markdown contracts, command templates | Task selection, processing constraints and reusable execution resources. |
| Execution and records | Main Agent, scoped Executor, local logs and operational memory | Tool orchestration and retained execution context. |

---

## Key Dependencies

| Component | Requirement |
| --- | --- |
| 3D Slicer 5.6+ | Install separately before registering the plugin. |
| Agent Python environment | Prepared by the installer; dependencies are pinned with hashes in [requirements.lock.txt](backend/requirements.lock.txt). |
| Model provider | An API key and an accessible model for the selected provider; see [model profiles](config/models.json). |
| Neuroimaging tools | Install the tools required by your Protocol, such as SlicerDMRI, UKFTractography, FSL or FreeSurfer. |
| Algorithm-specific resources | Configure DDParcel/DDSurfer implementations, compatible environments, atlases, weights and licenses when required. |

The Agent's Python environment is separate from algorithm-specific scientific
environments. Installing its dependencies does not install every imaging
backend. Missing software or assets should be reported and resolved before
running the affected workflow.

---

## Update or Uninstall

To update, close Slicer and run the new package's installer. Before uninstalling,
close Slicer and let the Agent backend exit. From the **installed application
folder**, run:

| Platform | Uninstall command |
| --- | --- |
| macOS / Linux | `bash ./uninstall.sh` |
| Windows PowerShell | `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1` |

Ordinary uninstall preserves user configuration, API keys, workspaces and logs.
To also remove saved keys, settings, logs and managed Protocols, append
`--purge` on macOS/Linux (confirmation required), or `-Purge -Yes` on Windows.
**Workspaces are preserved.**

---

## Development

| Directory | Contents |
| --- | --- |
| `backend/` | Main Agent, Executor, model adapters, local service and operational memory. |
| `slicer_extension/` | Slicer interface, startup and scene integration. |
| `protocols/` | Workflow definitions, templates and supporting resources. |
| `config/` | Provider profiles, software catalog and runtime defaults. |
| `scripts/` | Installers, launchers, Protocol validation and release checks. |

Use an existing `protocols/<id>/` package as a reference when extending a
workflow. From the repository root, validate a package and check source hygiene:

```bash
python scripts/validate_protocol_package.py protocols/DDSurfer
python scripts/check_repository.py
```

Replace `DDSurfer` with your Protocol ID. Add `--runtime-only` to the validator
to check runtime metadata without the full contract checks. These checks do
not run imaging algorithms or establish scientific validity.

Build the product ZIP from committed Git sources:

```bash
bash scripts/build_release.sh
```

The [CI workflow](.github/workflows/native-platforms.yml) checks packaging,
installer entrypoints, locked dependencies and runtime imports on Linux,
macOS and Windows. These checks do not demonstrate that every external
neuroimaging pipeline runs on all three platforms.

---

## Data and Privacy

Data files and execution logs are stored locally, but **local processing does
not mean offline inference**. Prompts, relevant tool outputs and images may be
sent to the selected model provider. Use authorized data and review your
provider's policies before submitting sensitive information.

API keys are saved in user-local configuration, not intended to be committed
to the repository. Logs can still contain paths and task details; review them
before sharing. This software is for research, not clinical diagnosis or
treatment.

---

## License

dMRI-Agent is released under the [MIT License](LICENSE). Dependencies, external
applications, datasets, atlases and model weights retain their own terms.
See [Third-party notices](THIRD_PARTY_NOTICES.md) for source references,
provenance limitations and redistribution considerations.

---

## Support

For questions, bug reports or feature requests, open a
[GitHub issue](https://github.com/azurezhangwei/dMRI-Agent/issues). Include the
platform, Slicer version, Protocol and relevant error evidence; remove API keys,
identifiable participant information and sensitive paths before sharing logs.

---

## Acknowledgments

Developed by **Fan's Lab**. Main contributors: **Wei Zhang, Chengjin Li, and
Guanlin Huang**. We thank the 3D Slicer and neuroimaging software communities.

The project's design and development drew on
[Qwen Code](https://github.com/QwenLM/qwen-code),
[Codex](https://github.com/openai/codex), and
[Gemini CLI](https://github.com/google-gemini/gemini-cli). Source references and
applicable terms are recorded in the [third-party notices](THIRD_PARTY_NOTICES.md).
