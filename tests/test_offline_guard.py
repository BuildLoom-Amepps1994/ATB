"""The offline guard (scripts/offline_net.py, installed by conftest) refuses every way out.

Each case aims at 203.0.113.10 (TEST-NET-3, never routed) or a public name, so a
guard that let one through would fail the test without reaching a real service.
"""
import asyncio
import os
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import pytest

import offline_net          # registered by conftest before any test module is imported

REMOTE = ('203.0.113.10', 443)


@pytest.fixture
def fake_proxy():
    """A loopback listener that records whatever a client sends it, as a proxy would receive it."""
    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    server.listen()
    server.settimeout(0.2)
    received, stop = [], threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(1)
                try:
                    received.append(conn.recv(4096))
                except OSError:
                    pass
    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    yield f'http://127.0.0.1:{server.getsockname()[1]}', received
    stop.set()
    worker.join(timeout=2)
    server.close()


def test_no_proxy_variable_survives_the_session_setup():
    leftovers = {k: v for k, v in os.environ.items() if k.lower().endswith('_proxy') and k.lower() != 'no_proxy'}
    assert leftovers == {}
    assert os.environ.get('NO_PROXY') == '*' or os.environ.get('no_proxy') == '*'


def test_clear_proxies_removes_every_case_and_scheme():
    env = {'HTTPS_PROXY': 'http://127.0.0.1:3128', 'http_proxy': 'x', 'ALL_PROXY': 'x', 'wss_proxy': 'x',
           'NO_PROXY': 'internal', 'PATH': '/bin'}
    offline_net.clear_proxies(env)
    assert env == {'PATH': '/bin', 'NO_PROXY': '*', 'no_proxy': '*'}


@pytest.mark.parametrize('variable', ['HTTPS_PROXY', 'https_proxy', 'ALL_PROXY'])
def test_a_loopback_proxy_cannot_carry_urllib_to_a_remote_host(monkeypatch, fake_proxy, variable):
    url, received = fake_proxy
    monkeypatch.setenv(variable, url)
    with pytest.raises(urllib.error.URLError) as caught:
        urllib.request.urlopen('https://api.mexc.com/api/v3/ping', timeout=5)
    assert isinstance(caught.value.reason, offline_net.RemoteNetworkRefused)
    assert received == []                                       # nothing reached the proxy


def test_a_loopback_proxy_cannot_carry_requests_to_a_remote_host(monkeypatch, fake_proxy):
    requests = pytest.importorskip('requests')
    url, received = fake_proxy
    monkeypatch.setenv('HTTPS_PROXY', url)
    with pytest.raises(requests.exceptions.ConnectionError):
        requests.get('https://api.github.com/', timeout=5)
    assert received == []


def test_asyncio_cannot_connect_to_a_remote_address():
    async def attempt():
        await asyncio.wait_for(asyncio.open_connection(*REMOTE), timeout=5)
    with pytest.raises(offline_net.RemoteNetworkRefused):
        asyncio.run(attempt())


def test_name_and_address_lookups_are_refused_but_localhost_resolves():
    for lookup in (socket.gethostbyname, socket.gethostbyname_ex):
        with pytest.raises(offline_net.RemoteNetworkRefused):
            lookup('api.mexc.com')
    with pytest.raises(offline_net.RemoteNetworkRefused):
        socket.gethostbyaddr(REMOTE[0])
    assert socket.gethostbyname('localhost').startswith('127.')


def test_a_datagram_to_a_remote_address_is_refused_and_one_to_loopback_is_sent():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver, \
         socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        receiver.bind(('127.0.0.1', 0))
        receiver.settimeout(2)
        with pytest.raises(offline_net.RemoteNetworkRefused):
            sender.sendto(b'query', ('203.0.113.10', 53))
        with pytest.raises(offline_net.RemoteNetworkRefused):
            sender.sendto(b'query', 0, ('203.0.113.10', 53))
        sender.sendto(b'local', receiver.getsockname())
        assert receiver.recv(16) == b'local'


def test_a_python_child_process_gets_the_same_guard():
    code = ('import socket\n'
            'try:\n'
            '    socket.create_connection(("203.0.113.10", 443), timeout=5)\n'
            'except ConnectionRefusedError as exc:\n'
            '    print(type(exc).__name__)\n')
    done = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == 'RemoteNetworkRefused'
