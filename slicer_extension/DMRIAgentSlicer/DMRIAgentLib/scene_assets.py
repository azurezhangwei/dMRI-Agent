from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


VOLUME_SUFFIXES = (".nii", ".nii.gz", ".nrrd", ".nhdr", ".mha", ".mhd")
MODEL_SUFFIXES = (".vtk", ".vtp", ".stl", ".ply", ".obj")
MARKUP_SUFFIXES = (".fcsv", ".mrk.json", ".json")
SCENE_SUFFIXES = (".mrml", ".mrb")


def agent_output_paths(output: Any, workspace: str | Path | None) -> list[str]:
    """Read explicit successful tool artifacts, never arbitrary paths in prose."""
    if workspace is None:
        return []
    try:
        payload = json.loads(output) if isinstance(output, str) else output
    except (ValueError, TypeError):
        return []
    if not isinstance(payload, dict) or payload.get("ok") is False:
        return []
    if str(payload.get("status", "")).lower() in {"error", "failed", "failure", "cancelled"}:
        return []
    candidates = payload.get("output_paths") or []
    candidates = list(candidates) if isinstance(candidates, list) else []
    for key in ("savedOutputs", "saved_outputs", "artifacts"):
        items = payload.get(key) or []
        if isinstance(items, list):
            candidates.extend(item.get("path") for item in items if isinstance(item, dict))
    if payload.get("ok") is True:
        preview = str(payload.get("stdout_preview") or payload.get("stdout") or "")
        if re.search(r"(?im)^\s*saved\s*:\s*(?:true|1)\s*$", preview):
            candidates.extend(re.findall(r"(?im)^\s*(?:path|output_path)\s*:\s*(.+?)\s*$", preview))
    root = Path(workspace).expanduser().resolve()
    paths = []
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        try:
            path = Path(candidate.strip()).expanduser()
            path = (root / path if not path.is_absolute() else path).resolve()
            path.relative_to(root)
            exists = path.exists()
        except (ValueError, OSError, RuntimeError):
            continue
        if exists and str(path) not in paths:
            paths.append(str(path))
    return paths


def capture_slice_views(scene: Any, selection: Any) -> dict[str, Any]:
    state: dict[str, Any] = {"layers": {}, "views": {}, "selection": {}}
    for suffix in ("ActiveVolumeID", "SecondaryVolumeID", "ActiveLabelVolumeID"):
        getter = getattr(selection, "Get" + suffix, None)
        if getter is not None:
            state["selection"][suffix] = getter() or ""
    for index in range(scene.GetNumberOfNodesByClass("vtkMRMLSliceCompositeNode")):
        node = scene.GetNthNodeByClass(index, "vtkMRMLSliceCompositeNode")
        state["layers"][node.GetID()] = [node.GetBackgroundVolumeID(), node.GetForegroundVolumeID(), node.GetLabelVolumeID()]
    for index in range(scene.GetNumberOfNodesByClass("vtkMRMLSliceNode")):
        node = scene.GetNthNodeByClass(index, "vtkMRMLSliceNode")
        matrix = node.GetSliceToRAS()
        state["views"][node.GetID()] = {
            "fov": tuple(node.GetFieldOfView()),
            "matrix": tuple(matrix.GetElement(row, col) for row in range(4) for col in range(4)),
        }
    return state


def restore_slice_views(scene: Any, selection: Any, state: dict[str, Any]) -> None:
    for suffix, value in state.get("selection", {}).items():
        setter = getattr(selection, "SetReference" + suffix, None)
        if setter is not None:
            setter(value if value and scene.GetNodeByID(value) is not None else "")
    for node_id, layers in state.get("layers", {}).items():
        node = scene.GetNodeByID(node_id)
        if node is None:
            continue
        for suffix, value in zip(("BackgroundVolumeID", "ForegroundVolumeID", "LabelVolumeID"), layers):
            getattr(node, "Set" + suffix)(value if value and scene.GetNodeByID(value) is not None else None)
    for node_id, view in state.get("views", {}).items():
        node = scene.GetNodeByID(node_id)
        if node is not None:
            node.SetFieldOfView(*view["fov"])
            node.GetSliceToRAS().DeepCopy(view["matrix"])
            node.UpdateMatrices()


def default_workspace_roots(
    workspace: str | Path | None = None,
) -> list[tuple[str, str]]:
    roots: list[tuple[str, str]] = []
    seen: set[str] = set()
    custom = os.environ.get("MRI_AGENT_FILE_ROOTS", "").strip()
    if custom:
        for entry in custom.split(os.pathsep):
            raw_candidate = entry.strip()
            if not raw_candidate:
                continue
            candidate = str(Path(raw_candidate).expanduser().resolve())
            if candidate != os.path.sep and os.path.isdir(candidate) and candidate not in seen:
                roots.append((Path(candidate).name or candidate, candidate))
                seen.add(candidate)
        return roots
    candidates = (
        ("Workspace", workspace or os.environ.get("MRI_AGENT_WORKSPACE", "")),
        ("Raw data", os.environ.get("DATA_ROOT", "")),
        ("Processed outputs", os.environ.get("PROCESSED_DIR", "")),
    )
    for label, raw_candidate in candidates:
        if not raw_candidate:
            continue
        candidate = str(Path(raw_candidate).expanduser().resolve())
        if candidate != os.path.sep and os.path.isdir(candidate) and candidate not in seen:
            roots.append((label, candidate))
            seen.add(candidate)
    return roots


def detect_file_kind(path: str | Path) -> str:
    path_str = str(path)
    lower = path_str.lower()
    if lower.endswith(".seg.nrrd"):
        return "segmentation"
    if lower.endswith(SCENE_SUFFIXES):
        return "scene"
    if lower.endswith(MARKUP_SUFFIXES):
        return "markups"
    if lower.endswith(MODEL_SUFFIXES):
        # Prefer fiber-bundle loading for common tractography outputs.
        if (
            "tract" in lower
            or "fiber" in lower
            or "streamline" in lower
            or "ukf" in lower
            or "/tractography/" in lower
        ):
            return "fiber_bundle"
        return "model"
    if lower.endswith(VOLUME_SUFFIXES):
        if "mask" in lower or "label" in lower:
            return "labelmap"
        file_name = Path(lower).name
        is_raw_dwi = bool(
            re.search(r"(^|[_-])dwi([_.-]|$)", file_name)
            and not re.search(
                r"(^|[_-])(fa|md|ad|rd|adc|tensor|rgb|colorfa)([_.-]|$)",
                file_name,
            )
        )
        if is_raw_dwi or "diffusion" in lower or lower.endswith(".nhdr"):
            return "diffusion_volume"
        return "scalar_volume"
    return "file"


def infer_subject_id(path: str) -> str:
    match = re.search(r"(sub[-_][A-Za-z0-9]+|(?<![A-Za-z0-9])[0-9]{6}(?![A-Za-z0-9]))", path)
    return match.group(1) if match else ""


def infer_space(path: str) -> str:
    lower = path.lower()
    if "/dwi/" in lower or "diffusion" in lower or lower.endswith(".nhdr"):
        return "dwi"
    if "/t1w/" in lower or "_t1" in lower:
        return "t1w"
    if "/mni" in lower or "mni" in lower:
        return "mni"
    if "/data/" in lower:
        return "raw"
    return "unknown"


def collect_file_metadata(path: str, root_label: str = "") -> dict[str, Any]:
    file_path = Path(path)
    try:
        stats = file_path.stat()
        size_bytes = stats.st_size
        modified_at = stats.st_mtime
        missing = False
    except OSError:
        size_bytes = 0
        modified_at = 0
        missing = True
    kind = "folder" if file_path.is_dir() else detect_file_kind(file_path)
    return {
        "path": str(file_path),
        "name": file_path.name,
        "kind": kind,
        "subjectId": infer_subject_id(str(file_path)),
        "space": infer_space(str(file_path)),
        "suffix": "".join(file_path.suffixes) or file_path.suffix,
        "sizeBytes": size_bytes,
        "modifiedAt": modified_at,
        "rootLabel": root_label,
        "missing": missing,
    }


@dataclass
class SceneAsset:
    asset_id: str
    mrml_node_id: str
    node_name: str
    node_class: str
    workspace_path: str
    source_root: str
    kind: str
    subject_id: str
    space: str

    def to_prompt_payload(self) -> dict[str, Any]:
        return {
            "assetId": self.asset_id,
            "mrmlNodeId": self.mrml_node_id,
            "name": self.node_name,
            "nodeClass": self.node_class,
            "workspacePath": self.workspace_path,
            "sourceRoot": self.source_root,
            "kind": self.kind,
            "subjectId": self.subject_id,
            "space": self.space,
        }


class SceneAssetRegistry:
    def __init__(self) -> None:
        self._assets: dict[str, SceneAsset] = {}
        self._by_node_id: dict[str, str] = {}
        self._by_path: dict[str, str] = {}

    def register_node(
        self,
        node: Any,
        workspace_path: str = "",
        source_root: str = "",
        kind: Optional[str] = None,
    ) -> SceneAsset:
        node_id = node.GetID() or str(uuid.uuid4())
        existing_id = self._by_node_id.get(node_id)
        if existing_id:
            asset = self._assets[existing_id]
            if workspace_path:
                asset.workspace_path = workspace_path
                self._by_path[workspace_path] = asset.asset_id
            if source_root:
                asset.source_root = source_root
            if kind:
                asset.kind = kind
            return asset

        workspace_path = workspace_path or self._extract_storage_path(node)
        inferred_kind = kind or self._infer_kind_from_node(node, workspace_path)
        asset = SceneAsset(
            asset_id=f"asset-{uuid.uuid4().hex[:8]}",
            mrml_node_id=node_id,
            node_name=node.GetName() or "(unnamed)",
            node_class=node.GetClassName() or "vtkMRMLNode",
            workspace_path=workspace_path,
            source_root=source_root,
            kind=inferred_kind,
            subject_id=infer_subject_id(workspace_path),
            space=infer_space(workspace_path),
        )
        self._assets[asset.asset_id] = asset
        self._by_node_id[node_id] = asset.asset_id
        if workspace_path:
            self._by_path[workspace_path] = asset.asset_id
        return asset

    def get(self, asset_id: str) -> Optional[SceneAsset]:
        return self._assets.get(asset_id)

    def get_by_path(self, workspace_path: str) -> Optional[SceneAsset]:
        asset_id = self._by_path.get(workspace_path)
        return self._assets.get(asset_id) if asset_id else None

    def get_by_node_id(self, node_id: str) -> Optional[SceneAsset]:
        asset_id = self._by_node_id.get(node_id)
        return self._assets.get(asset_id) if asset_id else None

    def list_assets(self) -> list[SceneAsset]:
        return sorted(self._assets.values(), key=lambda asset: asset.node_name.lower())

    def _infer_kind_from_node(self, node: Any, workspace_path: str) -> str:
        node_class = node.GetClassName() or ""
        if node_class == "vtkMRMLSegmentationNode":
            return "segmentation"
        if node_class == "vtkMRMLLabelMapVolumeNode":
            return "labelmap"
        if node_class == "vtkMRMLDiffusionWeightedVolumeNode":
            return "diffusion_volume"
        if node_class == "vtkMRMLFiberBundleNode":
            return "fiber_bundle"
        if node_class == "vtkMRMLModelNode":
            return "model"
        if node_class.startswith("vtkMRMLMarkups"):
            return "markups"
        if workspace_path:
            return detect_file_kind(workspace_path)
        return "scene_node"

    def _extract_storage_path(self, node: Any) -> str:
        storage_node = node.GetStorageNode()
        if storage_node:
            return storage_node.GetFileName() or ""
        return ""
