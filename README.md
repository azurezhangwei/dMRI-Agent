# dMRI-Agent

Natural-language diffusion MRI workflows in [3D Slicer](https://www.slicer.org/).

dMRI-Agent connects a conversational agent to local data and the active Slicer scene. Reusable **Protocols** guide processing, proposed actions require review, and local logs record execution. 3D Slicer is the only frontend in this release.

## Install

Install **3D Slicer 5.6 or newer**. Download `dmri-agent-<version>.zip` from this repository's Releases page, extract it, close Slicer, and run the command below from the extracted folder. The same package supports all three platforms; a source checkout can also be installed.

| Platform | Command |
|---|---|
| macOS / Ubuntu / Linux | `bash ./install.sh` |
| Windows PowerShell | `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1` |

The installer detects the platform, prepares a private Python runtime when needed, and registers the plugin. First installation requires internet access. Node.js and npm are not required. Keep the installed application folder; the downloaded package can be removed after installation.

## Use

1. Open Slicer and select **Modules → Diffusion MRI → dMRI-Agent**.
2. Select DeepSeek, OpenAI, Qwen (Beijing), or Anthropic Claude. In **Settings**, enter the provider's API key and model name, then choose **Save & Test**. **Get model list** offers available model names without changing your selection automatically.
3. Choose a workspace and describe your task in Chat. Expand **Files** to browse local files and scene items. Review requested approvals before execution.

Example: “Inspect the diffusion MRI files in my workspace and check whether they are suitable for FA estimation.”

Protocols cover DICOM/BIDS organization, diffusion metrics, tractography, connectomes, surface workflows, and reports. Required neuroimaging tools, atlases, and model weights are installed separately. Windows support for an individual workflow depends on its external tools; Bash templates require their own compatible environment.

## Update or uninstall

To update, close Slicer and run the new package's installer. Before uninstalling, close Slicer and let the Agent backend exit. Ordinary uninstall preserves user configuration, keys, workspaces, and logs. Run the command from the installed application folder:

| Platform | Uninstall |
|---|---|
| macOS / Linux | `bash ./uninstall.sh` |
| Windows PowerShell | `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1` |

The two scripts are platform entrypoints in the same package: `.sh` uses Bash on macOS/Linux; `.ps1` uses Windows PowerShell. To also remove saved keys, settings, logs, and managed Protocols, append `--purge` on macOS/Linux (confirmation required), or `-Purge -Yes` on Windows. Workspaces are preserved.

## Repository

| Directory | Contents |
|---|---|
| `backend/` | Agent, executor, model adapters, local service, and operational memory |
| `slicer_extension/` | Slicer interface and scene integration |
| `protocols/` | `PROTOCOL.md` workflow definitions and supporting resources |
| `config/` | Official provider profiles and runtime defaults |
| `scripts/` | Installation, launchers, packaging, and source checks |

To extend a workflow, use an existing `protocols/<id>/` package as a reference. Validate it with `python scripts/validate_protocol_package.py protocols/<id>`; add `--runtime-only` to check only runtime metadata. Source checks run with `python scripts/check_repository.py`; build the product ZIP from committed Git sources with `bash scripts/build_release.sh`.

## Data and license

Data and execution logs are stored locally. Prompts, relevant tool outputs, and images may be sent to your selected model provider. Use authorized data. This software is for research and is not intended for clinical use.

Developed by **Fan’s Lab**. Main contributors: **Wei Zhang, Chengjin Li, and Guanlin Huang**. We thank the 3D Slicer and neuroimaging software communities.

The project's design and development drew on [Qwen Code](https://github.com/QwenLM/qwen-code), [Codex](https://github.com/openai/codex), and [Gemini CLI](https://github.com/google-gemini/gemini-cli). We thank these open-source projects and their contributors. Source references and applicable terms are recorded in the [third-party notices](THIRD_PARTY_NOTICES.md).

Licensed under the [MIT License](LICENSE). See the [third-party notices](THIRD_PARTY_NOTICES.md).
