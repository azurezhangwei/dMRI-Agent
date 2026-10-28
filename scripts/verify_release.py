#!/usr/bin/env python3
"""Verify the single cross-platform ZIP and optionally extract it for CI."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import zipfile


def verify_release(archive: Path, *, extract_to: Path | None = None, revision: str | None = None) -> dict:
    checksum_lines = (archive.parent / 'SHA256SUMS').read_text(encoding='ascii').splitlines()
    expected = dict(line.split(maxsplit=1)[::-1] for line in checksum_lines if line.strip())
    if set(expected) != {archive.name}:
        raise ValueError('SHA256SUMS must describe exactly this one product ZIP')
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != expected[archive.name]:
        raise ValueError('Product ZIP checksum mismatch')
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len({m.filename.casefold() for m in members}) != len(members):
            raise ValueError('Duplicate or case-colliding ZIP entries')
        for member in members:
            path = PurePosixPath(member.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in member.filename or ':' in member.filename or stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError('Unsafe ZIP entry')
        manifests = [m.filename for m in members if m.filename.endswith('/RELEASE_MANIFEST.json')]
        if len(manifests) != 1:
            raise ValueError('Expected one release manifest')
        manifest = json.loads(bundle.read(manifests[0]))
        prefix = f"dmri-agent-{manifest['version']}/"
        if manifests[0] != prefix + 'RELEASE_MANIFEST.json' or manifest.get('distribution') != 'unified':
            raise ValueError('Expected a unified product directory')
        if revision and manifest['source_revision'] != revision:
            raise ValueError('Package does not match the expected source revision')
        paths = {entry['path'] for entry in manifest['files']}
        required = {'LICENSE', 'README.md', 'install.sh', 'install.ps1', 'uninstall.sh', 'uninstall.ps1', 'scripts/runtime_bootstrap.sh', 'scripts/native_install.py'}
        if not required <= paths or set(manifest.get('install_commands', {})) != {'macos', 'linux', 'windows'}:
            raise ValueError('Package lacks a supported platform entrypoint')
        actual_files = {m.filename for m in members if not m.is_dir()}
        if actual_files != {prefix + p for p in paths} | {manifests[0]}:
            raise ValueError('Manifest and ZIP file lists differ')
        for entry in manifest['files']:
            raw = bundle.read(prefix + entry['path'])
            if len(raw) != entry['size'] or hashlib.sha256(raw).hexdigest() != entry['sha256']:
                raise ValueError('Packaged file checksum mismatch: ' + entry['path'])
        if extract_to is not None:
            extract_to.mkdir(parents=True, exist_ok=True)
            if any(extract_to.iterdir()):
                raise ValueError('Extraction directory must be empty')
            for member in members:
                target = extract_to.joinpath(*PurePosixPath(member.filename).parts)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(bundle.read(member))
                    if os.name != 'nt':
                        target.chmod((member.external_attr >> 16) & 0o777)
    return {'ok': True, 'archive': archive.name, 'sha256': digest,
            'source_revision': manifest['source_revision'], 'files_verified': len(paths), 'platforms': ['macos', 'linux', 'windows']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--extract-to', type=Path)
    parser.add_argument('--revision')
    args = parser.parse_args()
    print(json.dumps(verify_release(args.archive, extract_to=args.extract_to, revision=args.revision)))
