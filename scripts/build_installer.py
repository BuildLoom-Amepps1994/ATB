"""Compile the validated editable distribution into a per-user Windows installer.

The build uses a locally installed Inno Setup 6/7 compiler; it never downloads or
executes a compiler implicitly. All subprocess arguments are passed without a shell.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_APP = {
    'app.py', 'launcher.py', 'trader.py', 'strategy.py', 'storage.py',
    'credentials.py', 'market_data.py', 'recovery.py', 'requirements-lock.txt',
    'static/index.html', 'static/dashboard.js', 'static/dashboard-core.js',
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assemble_stage(runtime: Path, stage: Path, version: str = '1.3.0', source: Path = ROOT) -> Path:
    """Create a new source-only app payload beside an existing runtime bundle."""
    runtime, stage, source = runtime.resolve(), stage.resolve(), source.resolve()
    if stage.exists():
        raise ValueError('Use a new staging folder; existing builds are never overwritten.')
    safe_tree(runtime)
    if not (runtime/'CryptoPilotRuntime.exe').is_file():
        raise ValueError('Build CryptoPilotEditable.spec before assembling the installer.')
    if stage == runtime or runtime in stage.parents or stage == source or stage in source.parents:
        raise ValueError('Staging must be separate from the input runtime and source ancestors.')
    stage.mkdir(parents=True)
    shutil.copytree(runtime,stage/'runtime')
    for name in sorted(REQUIRED_APP):
        path = source/name
        destination = stage/'app'/name
        destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(path,destination)
    shutil.copytree(source/'maintenance',stage/'maintenance',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    if (source/'WINDOWS_INSTALL.md').exists():
        shutil.copy2(source/'WINDOWS_INSTALL.md',stage/'README.md')
    else:
        shutil.copy2(source/'README.md',stage/'README.md')
    files = {name:digest(stage/'app'/name) for name in sorted(REQUIRED_APP)}
    record = {'format':1,'product':'CryptoPilot','installed_version':version,
              'requirements_sha256':digest(stage/'app/requirements-lock.txt'),'app_files':files}
    (stage/'installation.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf8')
    validate_stage(stage)
    return stage


def safe_tree(root: Path) -> None:
    if not root.is_dir():
        raise ValueError(f'Distribution directory does not exist: {root}')
    for path in (root, *root.rglob('*')):
        info = path.lstat()
        if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f'Distribution must not contain links or junctions: {path}')


def validate_stage(stage: Path) -> dict:
    safe_tree(stage)
    required = ['runtime/CryptoPilotRuntime.exe', 'installation.json',
                'maintenance/update_installation.py']
    for relative in required:
        path = stage / relative
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f'Distribution is missing a required file: {relative}')
    if not (stage / 'runtime/_internal').is_dir():
        raise ValueError('Distribution is missing the bundled runtime dependencies.')
    try:
        manifest = json.loads((stage / 'installation.json').read_text(encoding='utf-8'))
    except (ValueError, UnicodeError) as exc:
        raise ValueError('The installation record is not valid UTF-8 JSON.') from exc
    if manifest.get('format') != 1 or manifest.get('product') != 'CryptoPilot':
        raise ValueError('Unsupported CryptoPilot installation record.')
    if not isinstance(manifest.get('installed_version'), str) or not manifest['installed_version']:
        raise ValueError('The installation record has no version.')
    files = manifest.get('app_files')
    if not isinstance(files, dict) or not REQUIRED_APP.issubset(files):
        raise ValueError('The installation record is missing required application files.')
    actual = {p.relative_to(stage / 'app').as_posix() for p in (stage / 'app').rglob('*') if p.is_file()}
    if actual != set(files):
        raise ValueError('The application payload does not exactly match the installation record.')
    for relative, expected in files.items():
        pure = PurePosixPath(relative)
        if pure.is_absolute() or '..' in pure.parts or '\\' in relative or ':' in relative:
            raise ValueError(f'Unsafe application path: {relative}')
        # The editable application is deliberately source-only: no local state or credentials.
        if not (relative in REQUIRED_APP or relative.startswith('static/')):
            raise ValueError(f'Unexpected application payload file: {relative}')
        if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise ValueError(f'Invalid content fingerprint: {relative}')
        if digest(stage / 'app' / relative) != expected:
            raise ValueError(f'Application content fingerprint mismatch: {relative}')
    if manifest.get('requirements_sha256') != digest(stage / 'app/requirements-lock.txt'):
        raise ValueError('The dependency fingerprint does not match requirements-lock.txt.')
    return manifest


def find_iscc(explicit: str | None = None) -> Path:
    candidates = [explicit, os.environ.get('INNO_SETUP_COMPILER'), shutil.which('ISCC.exe')]
    for base in (os.environ.get('ProgramFiles(x86)'), os.environ.get('ProgramFiles'),
                 str(Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs')):
        if base:
            for name in ('Inno Setup 7', 'Inno Setup 6'):
                candidates.append(str(Path(base) / name / 'ISCC.exe'))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    raise ValueError('Install Inno Setup 6 or 7 from https://jrsoftware.org/isdl.php, '
                     'or set INNO_SETUP_COMPILER to the full path of ISCC.exe.')


def compile_installer(stage: Path, output: Path, compiler: Path, version: str | None = None) -> Path:
    stage = stage.resolve()
    output = output.resolve()
    manifest = validate_stage(stage)
    version = version or manifest['installed_version']
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?', version):
        raise ValueError('Installer version must be a dotted numeric version with an optional release suffix.')
    for path in (stage, output, compiler):
        if any(char in str(path) for char in ('"', '\r', '\n')):
            raise ValueError('Build paths must not contain quotes or line breaks.')
    # Never place an installer inside its own input tree.
    if output == stage or stage in output.parents:
        raise ValueError('The installer output folder must be outside the distribution input.')
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(compiler), '/Qp', f'/DStageDir={stage}', f'/DOutputDir={output}',
                    f'/DAppVersion={version}', str(ROOT / 'installer/CryptoPilot.iss')],
                   cwd=ROOT, check=True)
    result = output / f'CryptoPilot-Setup-{version}.exe'
    if not result.is_file():
        raise RuntimeError('The compiler did not create the expected installer.')
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, default=ROOT / 'dist/editable')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/installer')
    parser.add_argument('--iscc', help='Explicit path to the Inno Setup compiler.')
    parser.add_argument('--version', help='Defaults to the staged installation version.')
    parser.add_argument('--runtime-dir',type=Path,help='Assemble a new stage from the built dependency runtime first.')
    args = parser.parse_args(argv)
    try:
        if args.runtime_dir is not None:
            assemble_stage(args.runtime_dir,args.stage,args.version or '1.3.0')
        result = compile_installer(args.stage, args.output, find_iscc(args.iscc), args.version)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Installer build failed: {exc}\n')
    print(f'Windows installer: {result}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
