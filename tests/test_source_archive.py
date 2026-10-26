"""The source archive holds exactly the committed files, is deterministic and refuses private material.

The private material below is assembled at run time, so this file never holds
a literal key block or credential and the archive tool accepts it.
"""
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import make_source_archive as archive   # noqa: E402

KEY_BLOCK = b'-----BEGIN ' + b'PRIVATE KEY-----\nabc\n'
OPAQUE = b'Z9' * 16


@pytest.fixture(autouse=True)
def plain_git(tmp_path, monkeypatch):
    """No global or system git config: no signing key, hooks, fsmonitor or autocrlf of the developer's."""
    empty = tmp_path / 'empty.gitconfig'
    empty.write_bytes(b'')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(empty))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')


def git(root: Path, *args: str, stdin: bytes | None = None) -> str:
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, input=stdin).stdout.decode()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init', '-q')
    git(root, 'config', 'user.email', 'test@example.invalid')
    git(root, 'config', 'user.name', 'test')
    (root / 'app.py').write_bytes(b'print("hello")\n')          # bytes: write_text gives CRLF on Windows
    (root / 'docs').mkdir()
    (root / 'docs' / 'guide.md').write_bytes(b'# Guide\n')
    (root / 'portfolio.sqlite3').write_bytes(b'not tracked')          # local data: never tracked, never archived
    git(root, 'add', 'app.py', 'docs/guide.md')
    git(root, 'commit', '-q', '-m', 'init')
    return root


def entry(tmp_path, out: str, name: str) -> bytes:
    with zipfile.ZipFile(tmp_path / out / 'src.zip') as bundle:
        return bundle.read(f'src/{name}')


def test_archive_holds_exactly_the_committed_files_and_is_deterministic(repo, tmp_path):
    first = archive.build(tmp_path / 'out1', 'src', root=repo)
    second = archive.build(tmp_path / 'out2', 'src', root=repo)
    assert first['archive_sha256'] == second['archive_sha256']
    assert [f['path'] for f in first['files']] == ['app.py', 'docs/guide.md']
    with zipfile.ZipFile(tmp_path / 'out1' / 'src.zip') as bundle:
        assert sorted(bundle.namelist()) == ['src/app.py', 'src/docs/guide.md']
        assert hashlib.sha256(bundle.read('src/app.py')).hexdigest() == first['files'][0]['sha256']
        assert all(info.compress_type == zipfile.ZIP_STORED for info in bundle.infolist())
    sums = (tmp_path / 'out1' / 'SHA256SUMS').read_text()
    assert sums == f"{first['archive_sha256']}  src.zip\n"
    manifest = json.loads((tmp_path / 'out1' / 'src.manifest.json').read_text())
    assert manifest['commit'] == first['commit'] == git(repo, 'rev-parse', 'HEAD').strip()
    assert manifest['file_count'] == 2


def test_uncommitted_changes_are_refused(repo, tmp_path):
    (repo / 'app.py').write_bytes(b'print("edited")\n')
    with pytest.raises(archive.ArchiveRefused, match='Commit or discard'):
        archive.build(tmp_path / 'out', 'src', root=repo)
    assert not (tmp_path / 'out' / 'src.zip').exists()


def test_a_skip_worktree_edit_never_ships(repo, tmp_path):
    git(repo, 'update-index', '--skip-worktree', 'app.py')
    (repo / 'app.py').write_bytes(b'print("local edit git status does not show")\n')
    archive.build(tmp_path / 'out', 'src', root=repo)
    assert entry(tmp_path, 'out', 'app.py') == b'print("hello")\n'


def test_a_crlf_checkout_gives_the_same_archive(repo, tmp_path):
    before = archive.build(tmp_path / 'lf', 'src', root=repo)
    git(repo, 'config', 'core.autocrlf', 'true')                # Git for Windows' usual setting
    (repo / 'app.py').unlink()
    git(repo, 'checkout', '--', 'app.py')
    assert (repo / 'app.py').read_bytes() == b'print("hello")\r\n'
    after = archive.build(tmp_path / 'crlf', 'src', root=repo)
    assert after['archive_sha256'] == before['archive_sha256']
    assert entry(tmp_path, 'crlf', 'app.py') == b'print("hello")\n'


def test_a_committed_symbolic_link_is_refused_and_its_target_never_read(repo, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'portfolio.sqlite3').write_bytes(archive.SQLITE_MAGIC + b'real trades')
    link = git(repo, 'hash-object', '-w', '--stdin', stdin=b'../../outside/portfolio.sqlite3').strip()
    git(repo, 'update-index', '--add', '--cacheinfo', f'120000,{link},data/trades.bin')
    git(repo, 'commit', '-q', '-m', 'link')
    git(repo, 'checkout', '--', 'data/trades.bin')              # a real link where the platform allows one
    with pytest.raises(archive.ArchiveRefused, match=r'data/trades\.bin: a symbolic link'):
        archive.build(tmp_path / 'out', 'src', root=repo)
    assert not (tmp_path / 'out' / 'src.zip').exists()


def test_a_submodule_is_refused(repo, tmp_path):
    head = git(repo, 'rev-parse', 'HEAD').strip()
    git(repo, 'update-index', '--add', '--cacheinfo', f'160000,{head},vendor/lib')
    git(repo, 'commit', '-q', '-m', 'submodule')
    (repo / 'vendor' / 'lib').mkdir(parents=True)
    with pytest.raises(archive.ArchiveRefused, match=r'vendor/lib: a submodule'):
        archive.build(tmp_path / 'out', 'src', root=repo)


@pytest.mark.parametrize('name, content', [
    ('settings.json', b'{}'),
    ('profile/settings.last-good.json', b'{}'),
    ('backup/portfolio.sqlite3', b'x'),
    ('backup/portfolio.sqlite3-wal', b'x'),
    ('orders.db-journal', b'x'),
    ('keys.pem', b'x'),
    ('.env', b'x'),
    ('deploy/.env.local', b'x'),
    ('logs/cryptopilot.log', b'started'),
    ('id_ed25519', b'x'),
    ('data/cache.bin', archive.SQLITE_MAGIC + b'\x10\x00'),
    ('config.py', b'API_' + b'KEY = "' + OPAQUE + b'"\n'),
    ('client.py', b"creds = {'sec" + b"ret': '" + OPAQUE + b"'}\n"),
    ('fixtures/account.json', b'{"api' + b'Key": "' + OPAQUE + b'"}'),
    ('notes/keys.txt', b'MEXC_API_' + b'SECRET=' + b'q1' * 16 + b'\n'),
    ('notes/export.txt', b'\xff\xfe' + ('pass' + 'word: "' + 'Z9' * 16 + '"\r\n').encode('utf-16-le')),
    ('notes.txt', KEY_BLOCK),
])
def test_private_material_stops_the_build(repo, tmp_path, name, content):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    git(repo, 'add', '-f', name)
    git(repo, 'commit', '-q', '-m', 'add')
    with pytest.raises(archive.ArchiveRefused, match=re.escape(name)):
        archive.build(tmp_path / 'out', 'src', root=repo)
    assert not (tmp_path / 'out' / 'src.zip').exists()


@pytest.mark.parametrize('content', [
    b'api_key = credentials.load(exchange)\n',                  # a call, not a value
    b'secret = "short"\n',
    b'PASSWORD_HINT=use-the-operating-system-credential-store\n',  # no digit: prose, not a key
    b'# set MEXC_API_KEY in the app, never in a file\n',
])
def test_ordinary_code_and_prose_pass_the_screen(content):
    assert archive.screen('module.py', content) is None


def test_this_repository_passes_its_own_screen():
    if not (archive.ROOT / '.git').exists():
        pytest.skip('not a git checkout: there is no commit to screen')
    committed = [f'{name}: {reason}' for name, mode, content in archive.committed_files(archive.ROOT)
                 if (reason := archive.screen(name, content, mode))]
    assert committed == []
    tracked = subprocess.run(['git', 'ls-files', '-z'], cwd=archive.ROOT, check=True, capture_output=True).stdout
    working = [f'{name}: {reason}' for name in tracked.decode().split('\0') if name
               if (archive.ROOT / name).is_file()
               and (reason := archive.screen(name, (archive.ROOT / name).read_bytes()))]
    assert working == []


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
