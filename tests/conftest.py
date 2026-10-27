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
* Outbound connections, datagrams and name lookups are refused unless they go
  to a loopback address or a local socket (``scripts/offline_net.py``). The
  suite needs no internet; a test that tried to reach an exchange, a DNS
  server or GitHub fails instead of silently doing so. The guard answers
  lookups of ``localhost`` and of loopback addresses itself, so those never
  reach a DNS server either, and it blocks uvloop and winloop, whose event
  loops resolve and connect outside Python's socket module.
* Proxy variables are removed and ``NO_PROXY`` is ``*``: a proxy on loopback
  would otherwise pass the guard and forward a request to any host.
* Python child processes get the same guard: ``tests/offline_child`` goes
  first on ``PYTHONPATH``, and its ``sitecustomize`` installs it at start-up.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
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


_GUARD = Path(__file__).resolve().parents[1] / 'scripts' / 'offline_net.py'
_spec = importlib.util.spec_from_file_location('offline_net', _GUARD)
offline_net = importlib.util.module_from_spec(_spec)
sys.modules['offline_net'] = offline_net
_spec.loader.exec_module(offline_net)
RemoteNetworkRefused = offline_net.RemoteNetworkRefused
offline_net.clear_proxies(os.environ)
offline_net.install()
os.environ['PYTHONPATH'] = os.pathsep.join(
    [str(Path(__file__).resolve().parent / 'offline_child'), *filter(None, [os.environ.get('PYTHONPATH')])])


def pytest_report_header(config):
    return [f'cryptopilot isolation: data dir {DATA_DIR}; keyring null backend; remote networking and proxies refused, in child processes too']


def pytest_unconfigure(config):
    shutil.rmtree(ROOT, ignore_errors=True)
