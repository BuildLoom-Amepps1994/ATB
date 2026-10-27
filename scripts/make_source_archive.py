"""Build a reproducible source archive with a SHA-256 manifest. No network, no secrets.

Run from the repository root:
    python scripts/make_source_archive.py [--out dist] [--label NAME]

The archive holds exactly the files of the HEAD commit, read from git's own
objects (``git ls-tree`` and ``git cat-file``), never from the working tree.
So local data (portfolio databases, settings, logs, saved keys, virtual
environments, build output) can never be swept in by accident: none of those
is committed, and .gitignore keeps it that way. Reading the objects also
means a file marked skip-worktree or assume-unchanged ships as committed, and
a Windows checkout with CRLF line endings ships the same bytes as any other.
A committed symbolic link or submodule is refused, because its content lives
outside the commit.

Before writing, every file is checked against a list of names and contents
that must never ship: databases and their journal files, settings and
environment files, logs, private keys, credential-shaped assignments in code,
JSON, YAML or .env form (also in UTF-16), and SQLite content under any name.
A hit stops the build and names the file; nothing is written.

A working tree with uncommitted changes to tracked files is refused, so what
the operator sees is what ships. After writing, the archive is read back and
its membership and every entry's hash are compared with the manifest; a
mismatch deletes it.

The ZIP is deterministic: sorted entries, a fixed timestamp, fixed
permissions and no compression (deflate output can differ between zlib
implementations and versions), so the same commit produces the same archive
hash on any machine. The manifest lists each file's size and SHA-256
plus the archive's own hash, and is written beside the archive
(``<label>.manifest.json``) and as a plain ``SHA256SUMS`` file that
``sha256sum -c`` (or PowerShell ``Get-FileHash``) can check.
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
FORBIDDEN_NAMES = re.compile(r'''(
      \.(sqlite3?|db)(-wal|-shm|-journal)?       # databases and their write-ahead and journal files
    | \.(pem|key|p12|pfx|kdbx)
    | \.env(\.[^/]*)?                          # .env, .env.local, prod.env, secrets.env.example
    | settings(\.[^/]*)?\.json                 # settings.json, settings.last-good.json, app_settings.json
    | \.log(\.\d+)?
    | instance\.lock | id_rsa | id_ed25519 | id_ecdsa
)$''', re.IGNORECASE | re.VERBOSE)
SECRET_NAME = re.compile(rb'(?i)api[_-]?key|api[_-]?secret|secret|password|passphrase|private[_-]?key|access[_-]?token')
# A name, quoted or not (code, a dict, JSON, YAML), then a long opaque quoted value. Whether the name
# holds a secret word is checked afterwards, which keeps both patterns linear on long tokens.
ASSIGNMENT_QUOTED = re.compile(rb'''["']?(?<![\w.-])([\w.-]+)["']?\s*[=:]\s*["'][A-Za-z0-9+/=_-]{24,}["']''')
# The .env and INI form: NAME=value alone on its line, the value long, opaque and holding a digit.
ASSIGNMENT_BARE = re.compile(rb'(?m)^[ \t]*(?:export[ \t]+)?([\w.-]+)[ \t]*[=:][ \t]*'
                             rb'(?=[^\s#]*\d)[A-Za-z0-9+/=_-]{24,}[ \t]*\r?$')
PRIVATE_KEY = re.compile(rb'-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----')
SQLITE_MAGIC = b'SQLite format 3\x00'
REGULAR_FILE_MODES = ('100644', '100755')


class ArchiveRefused(RuntimeError):
    pass


def _git(root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, input=stdin).stdout


def committed_files(root: Path = ROOT) -> list[tuple[str, str, bytes | None]]:
    """(path, mode, content) for every file of HEAD, sorted; content is None for a link or submodule."""
    rows = []
    for record in _git(root, 'ls-tree', '-r', '-z', '--full-tree', 'HEAD').split(b'\0'):
        if record:
            meta, path = record.split(b'\t', 1)
            mode, kind, oid = meta.decode('ascii').split()
            rows.append((path.decode('utf-8'), mode, kind, oid))
    rows.sort()
    blobs = [row for row in rows if row[1] in REGULAR_FILE_MODES and row[2] == 'blob']
    output = _git(root, 'cat-file', '--batch', stdin=b''.join(oid.encode('ascii') + b'\n' for *_, oid in blobs))
    contents, position = {}, 0
    for path, _mode, _kind, oid in blobs:
        line_end = output.index(b'\n', position)
        found, kind, size = output[position:line_end].decode('ascii').split()
        if found != oid or kind != 'blob':
            raise ArchiveRefused(f'git returned {found} {kind} for {path}')
        start = line_end + 1
        contents[path] = output[start:start + int(size)]
        position = start + int(size) + 1
    return [(path, mode, contents.get(path)) for path, mode, _kind, _oid in rows]


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


def screen(name: str, content: bytes | None, mode: str = '100644') -> str | None:
    """Why ``name`` must not ship, or None."""
    if mode == '120000':
        return 'a symbolic link (its target is outside the commit)'
    if mode not in REGULAR_FILE_MODES or content is None:
        return 'a submodule or other non-file entry'
    if FORBIDDEN_NAMES.search(name):
        return 'a data, settings, log or key file name'
    if content.startswith(SQLITE_MAGIC):
        return 'an SQLite database'
    # UTF-16 text (PowerShell's default output) hides a match behind NUL bytes; check without them too.
    for text in (content, content.replace(b'\0', b'')) if b'\0' in content else (content,):
        if PRIVATE_KEY.search(text):
            return 'a private key block'
        for pattern in (ASSIGNMENT_QUOTED, ASSIGNMENT_BARE):
            if any(SECRET_NAME.search(found.group(1)) for found in pattern.finditer(text)):
                return 'a credential-shaped assignment'
    return None


def build(out_dir: Path, label: str, root: Path = ROOT) -> dict:
    dirty = dirty_files(root)
    if dirty:
        raise ArchiveRefused('Archive not written. Commit or discard these changes first:\n  ' + '\n  '.join(dirty))
    entries, refused = [], []
    for name, mode, content in committed_files(root):
        reason = screen(name, content, mode)
        if reason:
            refused.append(f'{name}: {reason}')
            continue
        entries.append((name, content))
    if refused:
        raise ArchiveRefused('Archive not written. These files must not ship:\n  ' + '\n  '.join(refused))
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f'{label}.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED) as bundle:
        for name, content in entries:
            info = zipfile.ZipInfo(f'{label}/{name}', date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3          # ZipInfo picks 0 on Windows and 3 elsewhere; fix it so the hash matches
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
        'source': 'git objects of the commit, not the working tree',
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
