"""Session isolation for the offline test campaign.

pytest imports this file before it collects any test module, so the
variables below are in place before ``storage`` (which fixes its data folder
at import) or ``credentials`` (which asks keyring for a backend) can load.

* ``CRYPTOPILOT_DATA_DIR`` always points at a new temporary folder, even when
  the shell that started pytest already set one: an inherited value could be
  the operator's real profile.
* ``LOCALAPPDATA`` points into the same temporary tree, so a test that removes
  ``CRYPTOPILOT_DATA_DIR`` still cannot fall back to ``%LOCALAPPDATA%\\CryptoPilot``.
* keyring uses its null backend, which stores nothing and returns nothing, so
  no test can read or write the operating-system credential manager.
* Outbound connections are refused unless they go to a loopback address or a
  local socket. The suite needs no internet; a test that tried to reach an
  exchange, a DNS server or GitHub fails instead of silently doing so.
"""
from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

_APP_MODULES = ('storage', 'credentials', 'trader', 'app', 'risk', 'micro', 'micro_venue')
_loaded_early = [name for name in _APP_MODULES if name in sys.modules]
if _loaded_early:
    raise RuntimeError('Application modules were imported before test isolation was set up: '
                       + ', '.join(_loaded_early))

ROOT = Path(tempfile.mkdtemp(prefix='cryptopilot-tests-'))
DATA_DIR = ROOT / 'data'
os.environ['CRYPTOPILOT_DATA_DIR'] = str(DATA_DIR)
os.environ['LOCALAPPDATA'] = str(ROOT / 'localappdata')
os.environ['PYTHON_KEYRING_BACKEND'] = 'keyring.backends.null.Keyring'
os.environ['CRYPTOPILOT_NO_UPDATE_CHECK'] = '1'
os.environ['CRYPTOPILOT_NO_BROWSER'] = '1'


class RemoteNetworkRefused(ConnectionRefusedError):
    """Raised when a test tries to open a connection that is not local."""


_LOCAL_NAMES = {'localhost', 'localhost.localdomain', 'ip6-localhost', 'ip6-loopback'}


def _local_host(host) -> bool:
    if host is None:
        return True        # getaddrinfo(None, port) means a local wildcard
    if isinstance(host, bytes):
        host = host.decode('ascii', 'replace')
    host = str(host).strip('[]').split('%')[0]
    if host == '' or host.lower() in _LOCAL_NAMES:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _allowed(address) -> bool:
    if isinstance(address, (str, bytes)):
        return True        # AF_UNIX path
    if isinstance(address, tuple) and address:
        return _local_host(address[0])
    return False


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


def _guarded_connect(self, address):
    if not _allowed(address):
        raise RemoteNetworkRefused(f'Offline test campaign refused a connection to {address!r}')
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    if not _allowed(address):
        raise RemoteNetworkRefused(f'Offline test campaign refused a connection to {address!r}')
    return _real_connect_ex(self, address)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if not _local_host(host):
        raise RemoteNetworkRefused(f'Offline test campaign refused a name lookup for {host!r}')
    return _real_getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
socket.getaddrinfo = _guarded_getaddrinfo


def pytest_report_header(config):
    return [f'cryptopilot isolation: data dir {DATA_DIR}; keyring null backend; remote networking refused']


def pytest_unconfigure(config):
    shutil.rmtree(ROOT, ignore_errors=True)
