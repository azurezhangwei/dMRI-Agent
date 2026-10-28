#!/usr/bin/env python3
"""Refresh declared license metadata for the exact locked dependencies."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
LOCKS = ("backend/requirements.lock.txt",)


def python_package(item: tuple[str, str]) -> dict:
    name, version = item
    source = f"https://pypi.org/pypi/{name}/{version}/json"
    request = Request(source, headers={"User-Agent": "dMRI-Agent-license-inventory"})
    with urlopen(request, timeout=30) as response:
        info = json.load(response)["info"]
    declared = info.get("license_expression")
    if not declared:
        declared = [c.removeprefix("License :: ") for c in info.get("classifiers", []) if c.startswith("License :: ")]
    if not declared:
        raw = (info.get("license") or "").strip()
        declared = raw if len(raw) <= 200 else "See upstream license text in source metadata"
    return {"name": name, "version": version, "declared_license": declared or "UNSPECIFIED", "source": source}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Write the maintenance inventory to this file")
    args = parser.parse_args()
    python_lock = (ROOT / LOCKS[0]).read_text(encoding="utf-8")
    pinned = re.findall(r"(?m)^([A-Za-z0-9_.-]+)==([^\s\\]+)", python_lock)
    with ThreadPoolExecutor(max_workers=8) as pool:
        python = list(pool.map(python_package, pinned))
    payload = {
        "schema_version": 1,
        "review_date": date.today().isoformat(),
        "scope": "Registry/lockfile declarations; not a substitute for upstream license texts or a source provenance audit.",
        "lock_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in LOCKS},
        "python": python,
    }
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    missing = [p["name"] for p in python if p["declared_license"] == "UNSPECIFIED"]
    print(f"License metadata: {len(python)} Python packages; unspecified: {missing}")


if __name__ == "__main__":
    main()
