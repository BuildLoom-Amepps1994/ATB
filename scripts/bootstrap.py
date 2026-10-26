"""Install an environment only when requirements change or packages are missing."""
from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The lock files are resolved for these interpreters. 3.15 is excluded until every
# pinned dependency ships prebuilt Windows packages for it.
OLDEST_PYTHON = (3, 12)
NEWEST_PYTHON = (3, 14)
RECOMMENDED_PYTHON = '3.14'


def supported_python(version: tuple[int, int]) -> str | None:
    """Return the reason a Python version cannot run the pinned dependencies, or None."""
    if version < OLDEST_PYTHON:
        return (f'CryptoPilot requires Python {OLDEST_PYTHON[0]}.{OLDEST_PYTHON[1]} or newer. '
                f'Python {RECOMMENDED_PYTHON} is recommended.')
    if version > NEWEST_PYTHON:
        return (f'CryptoPilot does not support Python {version[0]}.{version[1]} yet: its pinned '
                f'dependencies have no prebuilt packages for it. Install Python {RECOMMENDED_PYTHON}, '
                f'delete this environment ({sys.prefix}) and run again.')
    return None


def installed(name: str) -> bool:
    """True when ``name`` is importable; a dotted name whose parent is missing is simply absent."""
    try:
        return importlib.util.find_spec(name) is not None
    except ImportError:
        return False


def main() -> int:
    problem = supported_python(tuple(sys.version_info[:2]))
    if problem:
        print(problem)
        return 1
    build = '--build' in sys.argv[1:]
    runtime = ROOT / 'requirements-lock.txt'
    if not runtime.exists():
        runtime = ROOT / 'requirements.txt'
    build_requirements = ROOT / 'requirements-build-lock.txt'
    if not build_requirements.exists():
        build_requirements = ROOT / 'requirements-build.txt'
    requirements = [runtime] + ([build_requirements] if build else [])
    fingerprint = hashlib.sha256()
    fingerprint.update(f'{sys.version_info[:2]}:{sys.platform}:{build}'.encode())
    for path in requirements:
        fingerprint.update(path.name.encode())
        fingerprint.update(path.read_bytes())
    marker = Path(sys.prefix) / '.cryptopilot-requirements.sha256'
    needed = ['fastapi', 'uvicorn', 'ccxt', 'pandas', 'numpy', 'keyring', 'google.protobuf'] + (['PyInstaller'] if build else [])
    complete = all(installed(name) for name in needed)
    if complete and marker.exists() and marker.read_text(encoding='ascii') == fingerprint.hexdigest():
        print('CryptoPilot dependencies are ready.')
        return 0
    command = [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input']
    for path in requirements:
        command.extend(['-r', str(path)])
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode:
        print('Installation failed. Check your connection and Python installation, then run again.')
        return result.returncode
    marker.write_text(fingerprint.hexdigest(), encoding='ascii')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
