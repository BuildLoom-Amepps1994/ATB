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
from test_web_with_mock_exchange import webapp  # noqa: E402,F401

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
