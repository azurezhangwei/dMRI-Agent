# Third-party sources and terms

Project-owned code is covered by the [MIT License](LICENSE). This file records upstream sources and separate third-party terms.

| Source | Scope in this repository | Upstream terms / status |
|---|---|---|
| [mcp-slicer — zhaoyouj](https://github.com/zhaoyouj/mcp-slicer/tree/3991aa350e3a2f3322e5dbe64e2d57d249409491) | `backend/slicer_tools.py` and `backend/slicer_transport.py` identify its Slicer tool behavior as adapted from this project. | The [reviewed project metadata](https://github.com/zhaoyouj/mcp-slicer/blob/3991aa350e3a2f3322e5dbe64e2d57d249409491/pyproject.toml) declares MIT. No separate license/copyright file was found in that snapshot. Confirm the original material and required notice with its author before public distribution. |
| [Qwen Code — QwenLM](https://github.com/QwenLM/qwen-code) | Context-compression design reference; no Qwen Code runtime is bundled. | [Apache-2.0](https://github.com/QwenLM/qwen-code/blob/main/LICENSE). |
| [Codex — OpenAI](https://github.com/openai/codex) | Historical file-tool and output comments identify Codex-style behavior. The exact source revision and derivation scope were not recorded. | [Apache-2.0](https://github.com/openai/codex/blob/main/LICENSE). This reference does not establish that the retained Python implementation is an independently verified adaptation. |
| [Gemini CLI — Google](https://github.com/google-gemini/gemini-cli) | Design and development reference identified by the project authors. The exact source revision and any code-adaptation scope were not recorded. | [Apache-2.0](https://github.com/google-gemini/gemini-cli/blob/main/LICENSE). |

The release review sampled the named upstream sources and inspected repository references. It cannot certify the provenance of every contribution. If any upstream code or asset is retained or added, preserve its applicable copyright, license and notice text, and identify modifications as required by its terms.

## Installed dependencies

Python dependencies are fixed in `backend/requirements.lock.txt`. Their distributions retain their own license files and metadata in the installed environment. The product ZIP contains dependency declarations, not vendored Python packages. The installer downloads dependencies and, when needed, a private Python runtime; those packages keep their upstream terms.

No package license should be inferred solely from this project's distribution notice. Dependency versions are listed in `backend/requirements.lock.txt`. Maintainers can collect a separate license metadata inventory with `python scripts/collect_dependency_licenses.py --output <inventory.json>`; this inventory does not replace the upstream license texts.

## External software and data

3D Slicer, SlicerDMRI, FSL, FreeSurfer, external algorithms, atlases, datasets and model weights are separate software or data. They are not bundled by this release, and their use or redistribution requires the relevant upstream permissions. Protocol command templates do not grant rights to those resources.
