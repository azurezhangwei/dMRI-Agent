from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from backend.runtime_state import managed_protocols_home


ROOT_DIR = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ProtocolEntry:
    protocol_id: str
    name: str
    description: str
    protocol_type: str
    version: str
    path: Path


def parse_protocol_frontmatter(text: str) -> Dict[str, str]:
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    if len(lines) < 3:
        return {}
    meta: Dict[str, str] = {}
    idx = 1
    while idx < len(lines):
        line = lines[idx]
        if line.strip() == "---":
            break
        if ":" not in line:
            idx += 1
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        value = raw_value.strip().strip('"')
        if value in {">", "|"}:
            idx += 1
            block_lines = []
            while idx < len(lines):
                next_line = lines[idx]
                if next_line.strip() == "---":
                    idx -= 1
                    break
                if next_line.startswith("  ") or next_line.startswith("\t"):
                    block_lines.append(next_line.strip())
                    idx += 1
                else:
                    idx -= 1
                    break
            value = " ".join(block_lines).strip()
        meta[key] = value
        idx += 1
    return meta


def protocol_roots(cwd: str | Path | None = None) -> List[Path]:
    """Return built-in and user-managed protocol roots for this installation.

    Built-ins live under `<agent-project>/protocols` and remain read-only at
    runtime. Learned or generated protocols live under the user-managed protocols
    directory. Runtime working directories are data roots, not registration
    points.
    """

    del cwd
    roots: List[Path] = []
    for root in ((ROOT_DIR / "protocols").resolve(), managed_protocols_home()):
        if root.exists() and root.is_dir() and root not in roots:
            roots.append(root)
    return roots


def _iter_protocol_docs(cwd: str | Path | None = None) -> Iterable[Path]:
    for root in protocol_roots(cwd):
        yield from sorted(root.glob("*/PROTOCOL.md"))


def iter_protocol_entries(cwd: str | Path | None = None) -> List[ProtocolEntry]:
    entries: List[ProtocolEntry] = []
    seen_ids: set[str] = set()
    for protocol_path in _iter_protocol_docs(cwd):
        protocol_id = protocol_path.parent.name
        key = protocol_id.lower()
        if key in seen_ids:
            continue
        seen_ids.add(key)
        meta = parse_protocol_frontmatter(protocol_path.read_text(encoding="utf-8"))
        entries.append(
            ProtocolEntry(
                protocol_id=protocol_id,
                name=meta.get("name") or protocol_id,
                description=meta.get("description", ""),
                protocol_type=meta.get("type", "pipeline"),
                version=meta.get("version", ""),
                path=protocol_path.resolve(),
            )
        )
    return entries


def find_protocol_doc(name: str, cwd: str | Path | None = None) -> Optional[Path]:
    if not name:
        return None
    wanted = name.strip().lower()
    for protocol_path in _iter_protocol_docs(cwd):
        protocol_id = protocol_path.parent.name
        if protocol_id.lower() == wanted:
            return protocol_path.resolve()
        meta = parse_protocol_frontmatter(protocol_path.read_text(encoding="utf-8"))
        meta_name = meta.get("name", "")
        if meta_name and meta_name.lower() == wanted:
            return protocol_path.resolve()
    return None


def protocol_registry_json(cwd: str | Path | None = None) -> str:
    entries = [
        {
            "id": entry.protocol_id,
            "name": entry.name,
            "description": entry.description,
            "type": entry.protocol_type,
            "path": str(entry.path),
            "version": entry.version,
        }
        for entry in iter_protocol_entries(cwd)
    ]
    return json.dumps(entries, ensure_ascii=False)
