"""Offline launcher tests: no app import, credentials or exchange calls."""
import os
import socket
import subprocess
import sys
import time
import threading
import ssl
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import launcher


def test_launcher_import_has_no_engine_side_effects():
    command = [sys.executable, '-c', "import sys; import launcher; assert 'app' not in sys.modules; assert 'trader' not in sys.modules"]
    # Windows process creation/security scanning can exceed ten seconds on a
    # cold runtime. This checks import isolation, not launch-time performance.
    subprocess.run(command, cwd=ROOT, check=True, timeout=60)


@pytest.mark.parametrize('value', ['0', '-1', '65536', 'not-a-port'])
def test_invalid_port_is_rejected(monkeypatch, value):
    monkeypatch.setenv('CRYPTOPILOT_PORT', value)
    with pytest.raises(ValueError, match='1 to 65535'):
        launcher.configured_port()


def test_data_override_is_exact_and_working_directory_independent(monkeypatch, tmp_path):
    monkeypatch.setenv('CRYPTOPILOT_DATA_DIR', str(tmp_path))
    assert launcher.data_directory() == tmp_path.resolve()


def test_instance_lock_prevents_second_process_and_releases(tmp_path):
    script = "from pathlib import Path; from launcher import InstanceLock,AlreadyRunning; import sys\ntry:\n with InstanceLock(Path(sys.argv[1])): pass\nexcept AlreadyRunning:\n sys.exit(23)"
    with launcher.InstanceLock(tmp_path):
        result = subprocess.run([sys.executable, '-c', script, str(tmp_path)], cwd=ROOT, timeout=60, capture_output=True)
        assert result.returncode == 23, result.stderr
    subprocess.run([sys.executable, '-c', script, str(tmp_path)], cwd=ROOT, check=True, timeout=60)
    assert (tmp_path / 'instance.lock').exists()


def test_simultaneous_first_launches_have_one_owner_and_clean_rejections(tmp_path):
    (tmp_path / 'instance.lock').touch()
    worker = """
import os, sys, time
from pathlib import Path
from launcher import InstanceLock, AlreadyRunning
folder = Path(sys.argv[1])
(folder / ('ready.' + str(os.getpid()))).touch()
deadline = time.monotonic() + 10
while not (folder / 'go').exists() and time.monotonic() < deadline: time.sleep(.01)
try:
    with InstanceLock(folder):
        (folder / 'owner').touch()
        while not (folder / 'release').exists() and time.monotonic() < deadline: time.sleep(.01)
except AlreadyRunning:
    sys.exit(23)
"""
    children = [subprocess.Popen([sys.executable, '-c', worker, str(tmp_path)], cwd=ROOT,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
    try:
        deadline = time.monotonic() + 10
        while len(list(tmp_path.glob('ready.*'))) < 4 and time.monotonic() < deadline:
            time.sleep(.01)
        assert len(list(tmp_path.glob('ready.*'))) == 4
        (tmp_path / 'go').touch()
        while sum(child.poll() is not None for child in children) < 3 and time.monotonic() < deadline:
            time.sleep(.01)
        assert sum(child.poll() is None for child in children) == 1
        assert sorted(child.returncode for child in children if child.returncode is not None) == [23, 23, 23]
        assert (tmp_path / 'owner').exists()
    finally:
        (tmp_path / 'release').touch()
        for child in children:
            child.communicate(timeout=5)


def test_loopback_port_is_exclusive():
    with launcher.bind_loopback(0) as first:
        assert first.getsockname()[0] == '127.0.0.1'
        with pytest.raises(OSError):
            launcher.bind_loopback(first.getsockname()[1])


def test_busy_port_fails_before_app_initialization(monkeypatch, tmp_path, capsys):
    with socket.socket() as blocker:
        blocker.bind(('127.0.0.1', 0))
        blocker.listen()
        monkeypatch.setenv('CRYPTOPILOT_PORT', str(blocker.getsockname()[1]))
        monkeypatch.setenv('CRYPTOPILOT_DATA_DIR', str(tmp_path))
        assert launcher.main() == 1
    assert 'already in use' in capsys.readouterr().out


def test_browser_readiness_uses_loopback_http_without_tls_or_proxies(monkeypatch):
    token = 'this-process-only-token'
    opened = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(token.encode())

        def log_message(self, *_):
            pass

    def unexpected_tls(*_args, **_kwargs):
        raise AssertionError('Local HTTP readiness must not construct TLS')

    monkeypatch.setattr(ssl, 'create_default_context', unexpected_tls)
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setenv('ALL_PROXY', 'http://127.0.0.1:1')
    for name in ('NO_PROXY', 'no_proxy'):          # conftest's bypass-all would hide a launcher that used them
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(launcher.webbrowser, 'open', lambda url: opened.append(url) or True)
    with HTTPServer(('127.0.0.1', 0), Handler) as server:
        worker = threading.Thread(target=server.handle_request, daemon=True)
        worker.start()
        url = f'http://127.0.0.1:{server.server_port}/'
        launcher.open_when_ready(url, token, threading.Event())
        worker.join(timeout=3)
    assert opened == [url]


@pytest.mark.parametrize('failure', [False, OSError('No browser'), launcher.webbrowser.Error('No browser')])
def test_browser_open_failure_reports_manual_url(monkeypatch, capsys, failure):
    closed = []

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args):
            pass

        def getresponse(self):
            return self

        status = 200

        def read(self, _limit):
            return b'local-token'

        def close(self):
            closed.append(True)

    def browser_open(_url):
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(launcher.http.client, 'HTTPConnection', Connection)
    monkeypatch.setattr(launcher.webbrowser, 'open', browser_open)
    url = 'http://127.0.0.1:8765/'
    launcher.open_when_ready(url, 'local-token', threading.Event())
    assert url in capsys.readouterr().out
    assert closed == [True]


@pytest.mark.parametrize('status, body', [(200, b'other-process-token'), (503, b'local-token')])
def test_browser_never_opens_unverified_or_failed_page(monkeypatch, status, body):
    stop = threading.Event()
    closed = []

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args):
            pass

        def getresponse(self):
            return self

        def read(self, _limit):
            return body

        def close(self):
            closed.append(True)

    Connection.status = status
    monkeypatch.setattr(launcher.http.client, 'HTTPConnection', Connection)
    monkeypatch.setattr(stop, 'wait', lambda _: stop.set())
    monkeypatch.setattr(launcher.webbrowser, 'open', lambda _: pytest.fail('Unverified page opened'))
    launcher.open_when_ready('http://127.0.0.1:8765/', 'local-token', stop)
    assert closed == [True]


@pytest.mark.parametrize('url', ['https://127.0.0.1:8765/', 'http://example.org/', 'http://127.0.0.1:bad/'])
def test_browser_readiness_rejects_nonlocal_or_invalid_url(monkeypatch, capsys, url):
    monkeypatch.setattr(launcher.http.client, 'HTTPConnection', lambda *_args, **_kwargs: pytest.fail('Unexpected request'))
    launcher.open_when_ready(url, 'local-token', threading.Event())
    assert 'Cannot open' in capsys.readouterr().out
