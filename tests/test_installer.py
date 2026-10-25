"""Installer payload integrity and policy checks; never installs on the real profile."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('build_installer', ROOT / 'scripts/build_installer.py')
builder = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = builder
spec.loader.exec_module(builder)


@pytest.fixture
def stage(tmp_path):
    root = tmp_path / 'editable'
    for file in builder.REQUIRED_APP:
        path = root / 'app' / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'# test {file}\n', encoding='utf-8')
    for file in ('runtime/CryptoPilotRuntime.exe', 'runtime/_internal/python312.dll',
                 'maintenance/update_installation.py'):
        path = root / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'test payload')
    files = {file: builder.digest(root / 'app' / file) for file in sorted(builder.REQUIRED_APP)}
    manifest = {'format': 1, 'product': 'CryptoPilot', 'installed_version': '1.1.0',
                'requirements_sha256': files['requirements-lock.txt'], 'app_files': files}
    (root / 'installation.json').write_text(json.dumps(manifest), encoding='utf-8')
    return root


def test_valid_stage_is_accepted(stage):
    assert builder.validate_stage(stage)['installed_version'] == '1.1.0'


@pytest.mark.parametrize('file', ['app/trader.py', 'runtime/CryptoPilotRuntime.exe',
                                 'maintenance/update_installation.py'])
def test_missing_required_files_fail(stage, file):
    (stage / file).unlink()
    with pytest.raises(ValueError):
        builder.validate_stage(stage)


def test_modified_source_fails(stage):
    (stage / 'app/strategy.py').write_text('# changed', encoding='utf-8')
    with pytest.raises(ValueError, match='fingerprint mismatch'):
        builder.validate_stage(stage)


@pytest.mark.parametrize('name', ['settings.json', '.env', 'trades.db', '__pycache__/trader.pyc'])
def test_untracked_state_and_credentials_cannot_be_packaged(stage, name):
    path = stage / 'app' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'should never ship')
    with pytest.raises(ValueError, match='exactly match'):
        builder.validate_stage(stage)


def test_tracked_local_state_cannot_be_packaged(stage):
    path = stage / 'app/settings.json'
    path.write_text('{}', encoding='utf-8')
    record = json.loads((stage / 'installation.json').read_text())
    record['app_files']['settings.json'] = builder.digest(path)
    (stage / 'installation.json').write_text(json.dumps(record), encoding='utf-8')
    with pytest.raises(ValueError, match='Unexpected application'):
        builder.validate_stage(stage)


def test_dependency_mismatch_fails(stage):
    record = json.loads((stage / 'installation.json').read_text())
    record['requirements_sha256'] = '0' * 64
    (stage / 'installation.json').write_text(json.dumps(record), encoding='utf-8')
    with pytest.raises(ValueError, match='dependency fingerprint'):
        builder.validate_stage(stage)


def test_compile_arguments_are_shell_free_and_input_is_validated(stage, tmp_path, monkeypatch):
    captured = {}
    output = tmp_path / 'setup files'

    def run(args, **kwargs):
        captured.update(args=args, kwargs=kwargs)
        (output / 'CryptoPilot-Setup-1.1.0.exe').write_bytes(b'test installer')

    monkeypatch.setattr(builder.subprocess, 'run', run)
    result = builder.compile_installer(stage, output, Path('C:/compiler path/ISCC.exe'))
    assert result.exists()
    assert '/DStageDir=' + str(stage.resolve()) in captured['args']
    assert captured['kwargs']['check'] is True
    assert 'shell' not in captured['kwargs']


def test_output_cannot_be_inside_input(stage, monkeypatch):
    monkeypatch.setattr(builder.subprocess, 'run', lambda *_a, **_k: pytest.fail('compiler ran'))
    with pytest.raises(ValueError, match='outside'):
        builder.compile_installer(stage, stage / 'installer', Path('ISCC.exe'))


def test_invalid_version_does_not_execute_compiler(stage, tmp_path, monkeypatch):
    monkeypatch.setattr(builder.subprocess, 'run', lambda *_a, **_k: pytest.fail('compiler ran'))
    with pytest.raises(ValueError, match='dotted numeric'):
        builder.compile_installer(stage, tmp_path / 'out', Path('ISCC.exe'), '1.0"\nInjected=yes')


def test_installer_preserves_safeguards():
    script = (ROOT / 'installer/CryptoPilot.iss').read_text(encoding='utf-8')
    assert 'PrivilegesRequired=lowest' in script
    assert 'DefaultDirName={localappdata}\\Programs\\CryptoPilot' in script
    assert 'CloseApplications=no' in script
    assert 'RestartApplications=no' in script
    assert '[Run]' not in script and '[UninstallRun]' not in script
    assert '[UninstallDelete]' not in script
    assert 'update-transaction.json' in script
    assert "LockFile(InstallLock, 0, 0, 1, 0)" in script
    assert 'CreateUninstallRegKey=not TestInstall' in script
    assert 'uninsneveruninstall' in script
