"""The source archive holds exactly the committed files, is deterministic and refuses private material."""
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import make_source_archive as archive   # noqa: E402


def git(root: Path, *args: str) -> None:
    subprocess.run(['git', *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init', '-q')
    git(root, 'config', 'user.email', 'test@example.invalid')
    git(root, 'config', 'user.name', 'test')
    (root / 'app.py').write_text('print("hello")\n')
    (root / 'docs').mkdir()
    (root / 'docs' / 'guide.md').write_text('# Guide\n')
    (root / 'portfolio.sqlite3').write_bytes(b'not tracked')          # local data: never tracked, never archived
    git(root, 'add', 'app.py', 'docs/guide.md')
    git(root, 'commit', '-q', '-m', 'init')
    return root


def test_archive_holds_exactly_the_committed_files_and_is_deterministic(repo, tmp_path):
    first = archive.build(tmp_path / 'out1', 'src', root=repo)
    second = archive.build(tmp_path / 'out2', 'src', root=repo)
    assert first['archive_sha256'] == second['archive_sha256']
    assert [f['path'] for f in first['files']] == ['app.py', 'docs/guide.md']
    with zipfile.ZipFile(tmp_path / 'out1' / 'src.zip') as bundle:
        assert sorted(bundle.namelist()) == ['src/app.py', 'src/docs/guide.md']
        assert hashlib.sha256(bundle.read('src/app.py')).hexdigest() == first['files'][0]['sha256']
    sums = (tmp_path / 'out1' / 'SHA256SUMS').read_text()
    assert sums == f"{first['archive_sha256']}  src.zip\n"
    manifest = json.loads((tmp_path / 'out1' / 'src.manifest.json').read_text())
    assert manifest['commit'] == first['commit'] and manifest['file_count'] == 2


def test_uncommitted_changes_are_refused(repo, tmp_path):
    (repo / 'app.py').write_text('print("edited")\n')
    with pytest.raises(archive.ArchiveRefused, match='Commit or discard'):
        archive.build(tmp_path / 'out', 'src', root=repo)
    assert not (tmp_path / 'out' / 'src.zip').exists()


@pytest.mark.parametrize('name, content', [
    ('settings.json', b'{}'),
    ('backup/portfolio.sqlite3', b'x'),
    ('keys.pem', b'x'),
    ('config.py', b'API_KEY = "abcdefghijklmnopqrstuvwxyz012345"\n'),
    ('notes.txt', b'-----BEGIN PRIVATE KEY-----\nabc\n'),
])
def test_private_material_stops_the_build(repo, tmp_path, name, content):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    git(repo, 'add', '-f', name)
    git(repo, 'commit', '-q', '-m', 'add')
    with pytest.raises(archive.ArchiveRefused, match=name.replace('.', r'\.')):
        archive.build(tmp_path / 'out', 'src', root=repo)
    assert not (tmp_path / 'out' / 'src.zip').exists()


def test_a_tampered_archive_fails_verification(tmp_path):
    entries = [('a.txt', b'one')]
    path = tmp_path / 'x.zip'
    with zipfile.ZipFile(path, 'w') as bundle:
        bundle.writestr('x/a.txt', b'two')
    with pytest.raises(archive.ArchiveRefused, match='does not match'):
        archive.verify(path, 'x', entries)
    with zipfile.ZipFile(path, 'w') as bundle:
        bundle.writestr('x/a.txt', b'one')
        bundle.writestr('x/extra.txt', b'?')
    with pytest.raises(archive.ArchiveRefused, match='membership'):
        archive.verify(path, 'x', entries)
