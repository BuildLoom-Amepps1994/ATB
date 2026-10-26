"""In-app update checks and verification; no network, no real installer runs."""
import hashlib
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import updater  # noqa: E402
from test_web_with_mock_exchange import webapp  # noqa: E402,F401,F811

PAYLOAD = b'MZ fake installer bytes' * 100
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


def release(version='9.9.9', digest=DIGEST, checksum=True, size=None, **extra):
    tag = 'v' + version
    name = f'CryptoPilot-Setup-{version}.exe'
    prefix = updater.DOWNLOAD_PREFIX + tag + '/'
    assets = [{'name': name, 'browser_download_url': prefix + name, 'size': len(PAYLOAD) if size is None else size,
               'digest': 'sha256:' + digest if digest else None}]
    if checksum:
        assets.append({'name': name + '.sha256', 'browser_download_url': prefix + name + '.sha256', 'size': 90})
    return {'tag_name': tag, 'draft': False, 'prerelease': False, 'assets': assets, 'body': 'Notes',
            'html_url': updater.RELEASES_PAGE + '/tag/' + tag, **extra}


@pytest.fixture(autouse=True)
def update_checks_enabled(monkeypatch):
    # CI disables real update checks for the whole job; these tests use a fake server.
    monkeypatch.delenv('CRYPTOPILOT_NO_UPDATE_CHECK', raising=False)


class FakeServer:
    def __init__(self, rel, payload=PAYLOAD, checksum=DIGEST):
        self.rel, self.payload, self.checksum, self.calls = rel, payload, checksum, []

    def __call__(self, url, accept, timeout):
        self.calls.append(url)
        if url == updater.LATEST_URL:
            if isinstance(self.rel, Exception):
                raise self.rel
            return io.BytesIO(json.dumps(self.rel).encode())
        if url.endswith('.sha256'):
            return io.BytesIO(f'{self.checksum}  setup.exe\n'.encode())
        if url.endswith('.exe'):
            return io.BytesIO(self.payload)
        raise AssertionError(url)


def make_root(tmp_path, version='1.4.0'):
    root = tmp_path / 'install'
    root.mkdir()
    (root / 'installation.json').write_text(json.dumps({'installed_version': version}))
    return root


def make(tmp_path, server, root=None, launched=None):
    def launch(installer, install_root, log):
        launched.append((installer, install_root, log))
    return updater.Updater(open_url=server, download_dir=tmp_path / 'updates', root=root,
                           launch=launch if launched is not None else None, clock=lambda: 1000.0)


def test_versions_are_compared_numerically():
    assert updater.parse_version('v1.10.0') > updater.parse_version('1.9.9')
    assert updater.parse_version('1.4') is None and updater.parse_version('v1.4.0-beta') is None
    assert updater.parse_version(None) is None


def test_newer_release_is_offered_and_installed_version_comes_from_record(tmp_path):
    root = make_root(tmp_path, '1.4.0')
    status = make(tmp_path, FakeServer(release('1.5.0')), root=root).check()
    assert status['available'] and status['can_install']
    assert status['current_version'] == '1.4.0' and status['latest_version'] == '1.5.0'


def test_same_or_older_release_is_not_offered(tmp_path):
    root = make_root(tmp_path, '1.5.0')
    for version in ('1.5.0', '1.4.9'):
        status = make(tmp_path, FakeServer(release(version)), root=root).check()
        assert not status['available'] and not status['can_install']


def test_source_runs_see_updates_but_cannot_self_install(tmp_path):
    status = make(tmp_path, FakeServer(release('99.0.0')), root=None).check()
    assert status['available'] and not status['can_install'] and not status['installed']


def test_checks_are_cached_unless_forced(tmp_path):
    server = FakeServer(release('99.0.0'))
    item = make(tmp_path, server)
    item.check(); item.check()
    assert server.calls.count(updater.LATEST_URL) == 1
    item.check(force=True)
    assert server.calls.count(updater.LATEST_URL) == 2


def test_check_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv('CRYPTOPILOT_NO_UPDATE_CHECK', '1')
    server = FakeServer(release('99.0.0'))
    status = make(tmp_path, server).check(force=True)
    assert status['disabled'] and not status['available'] and server.calls == []


@pytest.mark.parametrize('error, text', [
    (urllib.error.HTTPError(updater.LATEST_URL, 404, 'x', {}, None), 'No published release'),
    (urllib.error.URLError('offline'), 'Could not reach GitHub'),
])
def test_network_failures_are_reported_not_raised(tmp_path, error, text):
    status = make(tmp_path, FakeServer(error)).check()
    assert text in status['error'] and not status['available']


@pytest.mark.parametrize('change', [
    lambda r: r.update(draft=True),
    lambda r: r.update(prerelease=True),
    lambda r: r.update(tag_name='latest'),
    lambda r: r['assets'][0].update(browser_download_url='https://evil.example/CryptoPilot-Setup-9.9.9.exe'),
    lambda r: r['assets'][0].update(name='Other.exe'),
    lambda r: r['assets'][0].update(size=updater.MAX_INSTALLER_BYTES + 1),
    lambda r: (r['assets'][0].update(digest=None), r['assets'].pop()),
])
def test_untrusted_or_incomplete_releases_are_rejected(change):
    rel = release()
    change(rel)
    with pytest.raises(updater.UpdateError):
        updater.select_installer(rel)


def test_verified_installer_is_launched_and_app_asked_to_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(updater.threading, 'Timer', lambda delay, fn: type('T', (), {'start': lambda self: fn()})())
    root, launched, exits = make_root(tmp_path), [], []
    item = make(tmp_path, FakeServer(release('9.9.9')), root=root, launched=launched)
    result = item.install(lambda: True, lambda: exits.append(1))
    assert result == {'ok': True, 'version': '9.9.9'}
    installer, install_root, _ = launched[0]
    assert install_root == root and installer.read_bytes() == PAYLOAD
    assert exits == [1] and item.phase == 'installing'


@pytest.mark.parametrize('server', [
    FakeServer(release(), payload=PAYLOAD[:-1] + b'X'),
    FakeServer(release(), checksum='0' * 64),
    FakeServer(release(), payload=PAYLOAD[:-5]),
    FakeServer(release(digest=None), payload=PAYLOAD[:-1] + b'X'),
])
def test_tampered_or_truncated_downloads_never_run(tmp_path, server):
    launched = []
    item = make(tmp_path, server, root=make_root(tmp_path), launched=launched)
    with pytest.raises(updater.UpdateError):
        item.install(lambda: True, None)
    assert launched == [] and item.phase == 'idle'
    assert not list((tmp_path / 'updates').glob('*.exe'))
    assert not list((tmp_path / 'updates').glob('*.part'))


def test_running_bot_blocks_install_even_if_started_during_download(tmp_path):
    launched = []
    item = make(tmp_path, FakeServer(release()), root=make_root(tmp_path), launched=launched)
    with pytest.raises(updater.UpdateError, match='Stop the bot'):
        item.install(lambda: False, None)
    answers = iter([True, False])
    with pytest.raises(updater.UpdateError, match='Stop the bot'):
        item.install(lambda: next(answers), None)
    assert launched == []


def test_uninstalled_source_runs_cannot_install(tmp_path):
    with pytest.raises(updater.UpdateError, match='Windows installed version'):
        make(tmp_path, FakeServer(release()), root=None, launched=[]).install(lambda: True, None)


def test_installer_version_matches_app_version():
    sys.path.insert(0, str(ROOT / 'scripts'))
    import build_installer
    assert build_installer.app_version() == updater.APP_VERSION
    iss = (ROOT / 'installer/CryptoPilot.iss').read_text(encoding='utf-8')
    assert f'#define AppVersion "{updater.APP_VERSION}"' in iss


def test_every_merge_gets_a_newer_release_version():
    sys.path.insert(0, str(ROOT / 'scripts'))
    import build_installer
    major, minor, _ = updater.parse_version(updater.APP_VERSION)
    assert build_installer.release_version(27) == f'{major}.{minor}.27'
    versions = [updater.parse_version(build_installer.release_version(n)) for n in (1, 2, 27, 28, 400)]
    assert versions == sorted(versions) and len(set(versions)) == len(versions)
    # Copies installed before automatic releases report 1.4.0 and must see the first one as newer.
    assert updater.parse_version(build_installer.release_version(1)) > updater.parse_version('1.4.0')
    for bad in (0, -3, '12', 2.0, True):
        with pytest.raises(ValueError):
            build_installer.release_version(bad)


def test_test_builds_never_outrank_a_release():
    sys.path.insert(0, str(ROOT / 'scripts'))
    import build_installer
    major, minor, _ = updater.parse_version(updater.APP_VERSION)
    assert build_installer.build_version(31, publish=True) == f'{major}.{minor}.31'
    # A pull-request artifact, whatever its commit count, is older than the first release.
    artifact = updater.parse_version(build_installer.build_version(400, publish=False))
    assert artifact == (major, minor, 0)
    assert artifact < updater.parse_version(build_installer.release_version(1))


def test_merges_to_main_publish_releases_stamped_with_that_version():
    workflow = (ROOT / '.github/workflows/windows-installer.yml').read_text(encoding='utf-8')
    assert 'branches: [main]' in workflow and 'fetch-depth: 0' in workflow
    assert "--version ${{ steps.version.outputs.version }}" in workflow
    assert "if: needs.build.outputs.publish == 'true'" in workflow
    assert 'build_installer.build_version(commits, publish)' in workflow


def publish_script():
    lines = (ROOT / '.github/workflows/windows-installer.yml').read_text(encoding='utf-8').splitlines()
    start = lines.index('      - name: Publish GitHub Release')
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == 'run: |')
    body = []
    for line in lines[run + 1:]:
        if line.strip() and not line.startswith(' ' * 10):
            break
        body.append(line[10:])
    return '\n'.join(body) + '\n'


FAKE_GH = """import json, os, sys
state_path = os.environ['FAKE_GH_STATE']
state = json.load(open(state_path))
args = sys.argv[1:]
state['calls'].append(args)
releases = state['releases']
def save():
    json.dump(state, open(state_path, 'w'))
if args[:2] == ['release', 'view']:
    tag = args[2] if not args[2].startswith('--') else None
    if tag is None:
        published = [r for r in releases if not r['draft']]
        save()
        if not published:
            sys.exit(1)
        print(published[-1]['tag'])
        sys.exit(0)
    found = [r for r in releases if r['tag'] == tag]
    save()
    if not found:
        sys.exit(1)
    print('true' if found[0]['draft'] else 'false')
elif args[:2] == ['release', 'delete']:
    state['releases'] = [r for r in releases if r['tag'] != args[2]]
    save()
elif args[:2] == ['release', 'create']:
    assert '--latest' in args
    releases.append({'tag': args[2], 'draft': False})
    save()
else:
    sys.exit(2)
"""


@pytest.mark.skipif(sys.platform == 'win32', reason='the publish job runs on Linux')
@pytest.mark.parametrize('releases, version, published', [
    ([], '1.5.29', True),
    ([{'tag': 'v1.5.29', 'draft': False}], '1.5.29', False),
    ([{'tag': 'v1.5.29', 'draft': True}], '1.5.29', True),
    ([{'tag': 'v1.5.31', 'draft': False}], '1.5.29', False),
    ([{'tag': 'v1.5.9', 'draft': False}], '1.5.10', True),
])
def test_publish_job_never_replaces_or_outranks_a_release(tmp_path, releases, version, published):
    import shutil
    import subprocess
    bash = shutil.which('bash')
    if bash is None:
        pytest.skip('bash is not available')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    gh = bin_dir / 'gh'
    gh.write_text(f'#!{sys.executable}\n' + FAKE_GH, encoding='utf-8')
    gh.chmod(0o755)
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'releases': releases, 'calls': []}))
    (tmp_path / 'release').mkdir()
    (tmp_path / 'release' / f'CryptoPilot-Setup-{version}.exe').write_bytes(b'MZ')
    env = {**__import__('os').environ, 'PATH': f'{bin_dir}:/usr/bin:/bin', 'FAKE_GH_STATE': str(state),
           'VERSION': version, 'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_SHA': 'abc123', 'GH_TOKEN': 'x'}
    result = subprocess.run([bash, '-c', publish_script()], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = json.loads(state.read_text())['calls']
    created = [call for call in calls if call[:2] == ['release', 'create']]
    assert bool(created) == published, calls
    if created:
        assert created[0][2] == f'v{version}' and '--target' in created[0]
    tags = [r['tag'] for r in json.loads(state.read_text())['releases']]
    assert len(tags) == len(set(tags))


def test_setup_supports_waiting_for_the_app_and_reopening_it():
    iss = (ROOT / 'installer/CryptoPilot.iss').read_text(encoding='utf-8')
    assert '{param:WAITLOCK|0}' in iss and '{param:RELAUNCH|0}' in iss
    assert '/WAITLOCK=' in (ROOT / 'updater.py').read_text(encoding='utf-8')


def test_update_api_reports_status_and_requires_token(webapp, monkeypatch):
    from fastapi.testclient import TestClient
    web = webapp
    monkeypatch.setattr(web.updates, 'check', lambda force=False: {'available': False, 'forced': force})
    client = TestClient(web.app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 50000))
    assert client.get('/api/update').json() == {'available': False, 'forced': False}
    assert client.post('/api/update/check', json={'force': True}).status_code == 403
    response = client.post('/api/update/check', json={'force': True}, headers={'X-CryptoPilot-Token': web.CSRF})
    assert response.json()['forced'] is True
    blocked = client.post('/api/update/install', headers={'X-CryptoPilot-Token': web.CSRF})
    assert blocked.status_code == 409
