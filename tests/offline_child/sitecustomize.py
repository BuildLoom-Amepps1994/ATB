"""Loaded at start-up by each Python child process that the offline test suite starts.

tests/conftest.py puts this folder first on PYTHONPATH, so a child (the soak
script, the runtime checks, the launcher probes) refuses remote networking
just as the test process does. A child started with -I or -E ignores
PYTHONPATH and is not covered.
"""
import importlib.util
import os
import sys

_GUARD = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                      'scripts', 'offline_net.py')
_spec = importlib.util.spec_from_file_location('offline_net', _GUARD)
_guard = importlib.util.module_from_spec(_spec)
sys.modules['offline_net'] = _guard
_spec.loader.exec_module(_guard)
_guard.install()
