"""The source bootstrap accepts only interpreters the lock files were resolved for."""
import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bootstrap', ROOT / 'scripts/bootstrap.py')
bootstrap = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bootstrap
spec.loader.exec_module(bootstrap)


@pytest.mark.parametrize('version', [(3, 12), (3, 13), (3, 14)])
def test_supported_interpreters_pass(version):
    assert bootstrap.supported_python(version) is None


def test_too_old_interpreter_is_refused_with_recommendation():
    message = bootstrap.supported_python((3, 11))
    assert '3.12 or newer' in message and '3.14 is recommended' in message


@pytest.mark.parametrize('version', [(3, 15), (4, 0)])
def test_too_new_interpreter_is_refused_with_recovery_steps(version):
    message = bootstrap.supported_python(version)
    assert f'Python {version[0]}.{version[1]}' in message
    assert 'Install Python 3.14' in message and sys.prefix in message


def test_this_interpreter_is_supported():
    assert bootstrap.supported_python(tuple(sys.version_info[:2])) is None


def test_launchers_only_try_supported_interpreters():
    for name in ('START_WINDOWS.bat', 'BUILD_WINDOWS_EXE.bat', 'BUILD_WINDOWS_INSTALLER.bat'):
        text = (ROOT / name).read_text(encoding='ascii')
        tried = {tuple(int(part) for part in match.split('.'))
                 for match in re.findall(r'\b(3\.\d+)\b', text)}
        assert tried, name
        assert all(bootstrap.supported_python(version) is None for version in tried), (name, tried)
        assert max(tried) == bootstrap.NEWEST_PYTHON, name
        assert bootstrap.RECOMMENDED_PYTHON in text, name
