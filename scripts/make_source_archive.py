"""Build a reproducible source archive with a SHA-256 manifest. No network, no secrets.

Run from the repository root:
    python scripts/make_source_archive.py [--out dist] [--label NAME]

The archive holds exactly the files git tracks at HEAD (``git ls-files``), so
local data (portfolio databases, settings, logs, saved keys, virtual
environments, build output) can never be swept in by accident: none of those
is tracked, and .gitignore keeps it that way. Before writing, every file is
checked against a short list of names and contents that must never ship
(databases, settings files, private keys, API-key shaped assignments). A hit
stops the build and names the file; nothing is written.

A working tree with uncommitted changes to tracked files is refused, so the
archive's content is the commit's content. After writing, the archive is read
back and its membership and every entry's hash are compared with the
manifest; a mismatch deletes it.

The ZIP is deterministic: sorted entries, a fixed timestamp and fixed
permissions, so the same commit always produces the same archive hash. The
manifest lists each file's size and SHA-256 plus the archive's own hash, and
is written beside the archive (``<label>.manifest.json``) and as a plain
``SHA256SUMS`` file that ``sha256sum -c`` (or PowerShell ``Get-FileHash``)
can check.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXED_TIME = (2026, 1, 1, 0, 0, 0)
FORBIDDEN_NAMES = re.compile(r'(\.sqlite3|\.db|\.pem|\.key|\.p12|\.pfx|\.env|settings\.json|settings\.last-good\.json|'
                             r'instance\.lock|id_rsa|id_ed25519)$', re.IGNORECASE)
# Assignments that look like a real credential: a key/secret name followed by a long opaque value.
SECRET_CONTENT = re.compile(rb'(?i)(api[_-]?key|api[_-]?secret|secret[_-]?key|password)\s*[=:]\s*[\'"][A-Za-z0-9+/=_-]{24,}[\'"]')
PRIVATE_KEY = re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')


class ArchiveRefused(RuntimeError):
    pass


def tracked_files(root: Path = ROOT) -> list[str]:
    output = subprocess.run(['git', 'ls-files', '-z'], cwd=root, check=True, capture_output=True).stdout
    return sorted(name for name in output.decode('utf-8').split('\0') if name)


def head_commit(root: Path = ROOT) -> str:
    return subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()


def dirty_files(root: Path = ROOT) -> list[str]:
    output = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=root, check=True,
                            capture_output=True, text=True).stdout
    return [line[3:] for line in output.splitlines() if line.strip()]


def verify(archive: Path, label: str, entries: list[tuple[str, bytes]]) -> None:
    """Read the written ZIP back: the same names, and each entry hashing to its source."""
    expected = {f'{label}/{name}': hashlib.sha256(content).hexdigest() for name, content in entries}
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if len(names) != len(set(names)) or set(names) != set(expected):
            raise ArchiveRefused('Archive membership differs from the selected files.')
        for name in names:
            if hashlib.sha256(bundle.read(name)).hexdigest() != expected[name]:
                raise ArchiveRefused(f'Archive entry {name} does not match its source.')


def screen(name: str, content: bytes) -> str | None:
    """Why ``name`` must not ship, or None."""
    if FORBIDDEN_NAMES.search(name):
        return 'a data, settings or key file name'
    if PRIVATE_KEY.search(content):
        return 'a private key block'
    if SECRET_CONTENT.search(content):
        return 'a credential-shaped assignment'
    return None


def build(out_dir: Path, label: str, root: Path = ROOT) -> dict:
    dirty = dirty_files(root)
    if dirty:
        raise ArchiveRefused('Archive not written. Commit or discard these changes first:\n  ' + '\n  '.join(dirty))
    files = tracked_files(root)
    entries, refused = [], []
    for name in files:
        path = root / name
        if not path.is_file():
            continue     # deleted in the working tree but still tracked: archive what exists
        content = path.read_bytes()
        reason = screen(name, content)
        if reason:
            refused.append(f'{name}: {reason}')
            continue
        entries.append((name, content))
    if refused:
        raise ArchiveRefused('Archive not written. These files must not ship:\n  ' + '\n  '.join(refused))
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f'{label}.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for name, content in entries:
            info = zipfile.ZipInfo(f'{label}/{name}', date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            bundle.writestr(info, content)
    try:
        verify(archive, label, entries)
    except ArchiveRefused:
        archive.unlink()
        raise
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = {
        'label': label, 'commit': head_commit(root), 'file_count': len(entries),
        'archive': archive.name, 'archive_sha256': digest, 'archive_bytes': archive.stat().st_size,
        'files': [{'path': name, 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
                  for name, content in entries],
        'excluded_by_design': 'untracked files: portfolio databases, settings, logs, saved keys, virtual '
                              'environments and build output',
    }
    (out_dir / f'{label}.manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    (out_dir / 'SHA256SUMS').write_text(f'{digest}  {archive.name}\n', encoding='utf-8')
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--out', default=str(ROOT / 'dist'))
    parser.add_argument('--label', default=None)
    args = parser.parse_args(argv)
    label = args.label or f'CryptoPilot-source-{head_commit()[:12]}'
    try:
        manifest = build(Path(args.out), label)
    except ArchiveRefused as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"{manifest['archive']}  {manifest['archive_sha256']}  ({manifest['file_count']} files, commit {manifest['commit'][:12]})")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
