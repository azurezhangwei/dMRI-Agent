from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Tuple


def map_container_to_host(path: Path) -> Path:
    mapped = _map_path(path, direction="container_to_host")
    if mapped is None:
        return path
    mapped_path, target_prefix = mapped
    if target_prefix.exists():
        return mapped_path
    return path


def map_host_to_container(path: Path) -> Path:
    mapped = _map_path(path, direction="host_to_container")
    if mapped is None:
        return path
    mapped_path, _ = mapped
    # The target belongs to a different filesystem namespace and is not
    # expected to exist on the host where this mapping is calculated.
    return mapped_path


def _map_path(path: Path, direction: str) -> Optional[Tuple[Path, Path]]:
    maps = _load_path_maps()
    if not maps:
        return None
    path_str = str(path)
    def source_prefix(pair: Tuple[str, str]) -> str:
        container_prefix, host_prefix = pair
        return container_prefix if direction == "container_to_host" else host_prefix

    for container_prefix, host_prefix in sorted(
        maps, key=lambda pair: len(source_prefix(pair)), reverse=True
    ):
        source = container_prefix if direction == "container_to_host" else host_prefix
        target = host_prefix if direction == "container_to_host" else container_prefix
        if path_str == source or path_str.startswith(source + os.sep):
            suffix = path_str[len(source) :]
            return Path(target + suffix), Path(target)
    return None


def _load_path_maps() -> List[Tuple[str, str]]:
    raw = os.environ.get("PATH_MAPS", "").strip()
    if not raw:
        return []
    pairs: List[Tuple[str, str]] = []
    for item in raw.split(","):
        token = item.strip()
        if not token:
            continue
        if "=" not in token:
            continue
        container_prefix, host_prefix = token.split("=", 1)
        container_prefix = container_prefix.strip().rstrip(os.sep)
        host_prefix = host_prefix.strip().rstrip(os.sep)
        if container_prefix and host_prefix:
            pairs.append((container_prefix, host_prefix))
    return pairs
