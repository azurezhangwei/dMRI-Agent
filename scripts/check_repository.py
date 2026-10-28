#!/usr/bin/env python3
"""Check tracked source hygiene without printing matched credential values."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERNS = {
    "provider-key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})"),
    "google-key": re.compile(r"\bAIza[A-Za-z0-9_-]{25,}"),
    "private-key": re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
}
GENERATED_PARTS = {"node_modules", "__pycache__", ".venv", ".pytest_cache", ".ruff_cache", "dist"}
RAW_PREFIXES = ("benchmark/archived_logs/", "benchmark/manual_cases/", "benchmark/figures/")
LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*(<[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)")
REFERENCE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(<[^>]+>|\S+)", re.MULTILINE)


def check(root: Path, paths: list[str], *, license_inventory: Path | None = None) -> list[dict]:
    root = root.resolve()
    issues: list[dict] = []

    def report(path: str, line: int, rule: str, detail: str) -> None:
        issues.append({"path": path, "line": line, "rule": rule, "detail": detail})

    for relative in sorted(paths):
        parts = PurePosixPath(relative).parts
        path = root / relative
        if not path.is_file():
            report(relative, 0, "missing-tracked-file", "Tracked path is absent")
            continue
        secret_name = path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example")
        if secret_name or path.suffix in {".pem", ".key", ".p12"} or relative == "config/secrets.json":
            report(relative, 0, "credential-file", "Private configuration must stay outside Git")
        if GENERATED_PARTS.intersection(parts) or relative.startswith(RAW_PREFIXES):
            report(relative, 0, "generated-or-raw-output", "Generated files and raw run logs must stay outside Git")
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeError:
            continue
        for line_number, line in enumerate(source.splitlines(), 1):
            for rule, pattern in SECRET_PATTERNS.items():
                for match in pattern.finditer(line):
                    report(relative, line_number, rule, "Possible credential; value withheld")
        if path.suffix.lower() != ".md":
            continue
        # Ignore examples in fenced/inline code; preserve newlines for locations.
        prose = re.sub(r"(?ms)^\s*(```|~~~).*?^\s*\1[^\n]*", lambda m: "\n" * m.group().count("\n"), source)
        prose = re.sub(r"`[^`\n]+`", "", prose)
        for match in [*LINK.finditer(prose), *REFERENCE.finditer(prose)]:
            target = match.group(1).strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            destination = unquote(parsed.path)
            location = (path.parent / destination).resolve()
            if not location.is_relative_to(root) or not location.exists():
                report(relative, prose[:match.start()].count("\n") + 1, "markdown-link", f"Unresolved local link: {destination}")
    if license_inventory is not None:
        for relative, expected in json.loads(license_inventory.read_text(encoding="utf-8")).get("lock_sha256", {}).items():
            lock = root / relative
            if not lock.is_file() or hashlib.sha256(lock.read_bytes()).hexdigest() != expected:
                report(relative, 0, "license-inventory-stale", "Refresh the license inventory after reviewing changed dependencies")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, help="Write a value-redacted result file")
    parser.add_argument("--license-inventory", type=Path, help="Also check a separate maintenance license inventory")
    args = parser.parse_args()
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    paths = [p for p in paths if p]
    issues = check(ROOT, paths, license_inventory=args.license_inventory)
    payload = {"ok": not issues, "tracked_files": len(paths), "issues": issues}
    if args.json:
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for issue in issues:
        print("{path}:{line}: {rule}: {detail}".format(**issue))
    print(f"Repository hygiene: {len(paths)} tracked files, {len(issues)} issue(s)")
    return int(bool(issues))


if __name__ == "__main__":
    raise SystemExit(main())
