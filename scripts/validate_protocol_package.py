#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.protocol_manager import PROTOCOL_ID_RE, ProtocolStore  # noqa: E402


STRICT_SECTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("scope", re.compile(r"^#{2,3}\s+.*(?:scope|\u9002\u7528\u8303\u56f4)", re.IGNORECASE | re.MULTILINE)),
    ("input contract", re.compile(r"^#{2,3}\s+.*(?:input contract|\u8f93\u5165\u5951\u7ea6)", re.IGNORECASE | re.MULTILINE)),
    ("output contract", re.compile(r"^#{2,3}\s+.*(?:output contract|\u8f93\u51fa\u5951\u7ea6)", re.IGNORECASE | re.MULTILINE)),
    ("workflow", re.compile(r"^#{2,3}\s+.*(?:workflow|\u5de5\u4f5c\u6d41)", re.IGNORECASE | re.MULTILINE)),
    ("quality control", re.compile(r"^#{2,3}\s+.*(?:quality control|\u8d28\u91cf\u63a7\u5236|\bqc\b)", re.IGNORECASE | re.MULTILINE)),
    ("failure boundaries", re.compile(r"^#{2,3}\s+.*(?:failure|\u5931\u8d25\u8fb9\u754c)", re.IGNORECASE | re.MULTILINE)),
    ("privacy boundaries", re.compile(r"^#{2,3}\s+.*(?:privacy|\u9690\u79c1\u8fb9\u754c)", re.IGNORECASE | re.MULTILINE)),
    ("test contract", re.compile(r"^#{2,3}\s+.*(?:test contract|\u6d4b\u8bd5\u5951\u7ea6)", re.IGNORECASE | re.MULTILINE)),
)
RESOURCE_RE = re.compile(r"(?<![A-Za-z0-9_.-])(code_templates/[A-Za-z0-9_./-]+)")


@dataclass(frozen=True)
class PackageValidation:
    path: str
    valid: bool
    errors: list[str]
    warnings: list[str]
    revision: str

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "valid": self.valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "revision": self.revision,
        }


def validate_package(path: Path, *, strict: bool = True) -> PackageValidation:
    package = path.expanduser().resolve()
    protocol = package / "PROTOCOL.md" if package.is_dir() else package
    package = protocol.parent
    errors: list[str] = []
    warnings: list[str] = []
    if not protocol.is_file():
        return PackageValidation(str(package), False, ["PROTOCOL.md not found"], [], "")
    if not PROTOCOL_ID_RE.fullmatch(package.name):
        errors.append("Protocol directory must match [A-Za-z][A-Za-z0-9_-]{1,63}")

    content = protocol.read_text(encoding="utf-8")
    runtime = ProtocolStore().validate(content)
    errors.extend(runtime.errors)

    for item in package.rglob("*"):
        if item.is_symlink():
            errors.append(f"symlink is not allowed: {item.relative_to(package)}")

    for referenced in sorted(set(RESOURCE_RE.findall(content))):
        relative = referenced.rstrip(".,;:)]}")
        candidate = (package / relative).resolve(strict=False)
        try:
            candidate.relative_to(package)
        except ValueError:
            errors.append(f"resource escapes Protocol directory: {relative}")
            continue
        if not candidate.is_file():
            errors.append(f"referenced resource not found: {relative}")

    if strict:
        for label, pattern in STRICT_SECTIONS:
            if not pattern.search(content):
                errors.append(f"missing strict contract section: {label}")
        if runtime.metadata.get("type") in {"pipeline", "workflow"} and not (
            package / "code_templates"
        ).is_dir():
            warnings.append("code_templates directory is missing")

    return PackageValidation(
        path=str(package),
        valid=not errors,
        errors=list(dict.fromkeys(errors)),
        warnings=list(dict.fromkeys(warnings)),
        revision=runtime.revision,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate dMRI-Agent Protocol packages")
    parser.add_argument("paths", nargs="+", type=Path, help="Protocol directory or PROTOCOL.md")
    parser.add_argument(
        "--runtime-only",
        action="store_true",
        help="Apply only current runtime validation, without the strict contract sections",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    results = [validate_package(path, strict=not args.runtime_only) for path in args.paths]
    if args.json:
        print(json.dumps([result.as_dict() for result in results], ensure_ascii=False, indent=2))
    else:
        for result in results:
            state = "PASS" if result.valid else "FAIL"
            print(f"{state} {result.path} revision={result.revision or '-'}")
            for warning in result.warnings:
                print(f"  WARN: {warning}")
            for error in result.errors:
                print(f"  ERROR: {error}")
    return 0 if all(result.valid for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
