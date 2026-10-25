"""Install an environment only when requirements change or packages are missing."""
from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if sys.version_info < (3, 11):
        print('CryptoPilot requires Python 3.11 or newer. Python 3.12 is recommended.')
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
    needed = ['fastapi', 'uvicorn', 'ccxt', 'pandas', 'numpy', 'keyring'] + (['PyInstaller'] if build else [])
    complete = all(importlib.util.find_spec(name) is not None for name in needed)
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
