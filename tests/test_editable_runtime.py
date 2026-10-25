"""Offline editable-runtime tests: fake source only, no credentials/exchanges."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import windows_runtime as runtime


def fake_app(folder):
    folder.mkdir(parents=True)
    for name in runtime.REQUIRED_MODULES:
        (folder / (name + '.py')).write_text('', encoding='utf8')
    (folder / 'static').mkdir()
    for name in runtime.REQUIRED_ASSETS:
        (folder / 'static' / name).write_text('asset', encoding='utf8')
    (folder / 'launcher.py').write_text('def main():\n    return 23\n', encoding='utf8')
    maintenance = folder.parent / 'maintenance'
    maintenance.mkdir(exist_ok=True)
    (maintenance / 'update_installation.py').write_text(
        'class UpdateError(RuntimeError): pass\n'
        'def recover_if_needed(root): pass\n'
        'def update_installation(root, source, replace_local_changes=False):\n'
        '    return {"updated": True, "replace": replace_local_changes}\n', encoding='utf8')
    return folder


def invoke(directory, *args, cwd=None):
    return subprocess.run([sys.executable, str(ROOT / 'windows_runtime.py'),
                           '--app-dir', str(directory), *args], cwd=cwd or directory.parent,
                          capture_output=True, text=True, timeout=60)


def test_check_app_is_offline_and_never_executes_source(tmp_path):
    folder = fake_app(tmp_path / 'app')
    (folder / 'app.py').write_text("raise AssertionError('must not import')", encoding='utf8')
    result = invoke(folder, '--check-app', str(folder))
    assert result.returncode == 0, result.stderr
    assert '"valid": true' in result.stdout
    assert not (folder / '__pycache__').exists()


@pytest.mark.parametrize('relative', ['launcher.py', 'app.py', 'trader.py', 'static/dashboard.js'])
def test_missing_source_and_assets_fail_closed(tmp_path, relative):
    folder = fake_app(tmp_path / 'app')
    (folder / relative).unlink()
    result = invoke(folder)
    assert result.returncode == 1
    assert 'missing' in result.stderr.lower()
    assert relative.replace('/', os.sep) in result.stderr


def test_syntax_error_fails_before_launcher_runs(tmp_path):
    folder = fake_app(tmp_path / 'app')
    (folder / 'trader.py').write_text('def broken(:', encoding='utf8')
    result = invoke(folder)
    assert result.returncode == 1
    assert 'trader.py (SyntaxError)' in result.stderr


def test_source_edit_takes_effect_on_restart_even_with_stale_pyc(tmp_path):
    import py_compile
    folder = fake_app(tmp_path / 'app')
    source = folder / 'strategy.py'
    source.write_text('VALUE = 17\n', encoding='utf8')
    py_compile.compile(str(source), doraise=True)
    original = source.stat()
    (folder / 'launcher.py').write_text('import strategy\ndef main():\n    return strategy.VALUE\n', encoding='utf8')
    assert invoke(folder).returncode == 17
    source.write_text('VALUE = 29\n', encoding='utf8')
    os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert source.stat().st_size == original.st_size
    assert invoke(folder).returncode == 29


def test_working_folder_cannot_override_installed_source(tmp_path):
    folder = fake_app(tmp_path / 'install' / 'app')
    other = tmp_path / 'other'
    other.mkdir()
    (other / 'launcher.py').write_text('raise AssertionError("wrong launcher")', encoding='utf8')
    assert invoke(folder, cwd=other).returncode == 23


def test_source_finder_beats_frozen_fallback_and_blocks_missing_core(tmp_path):
    folder = fake_app(tmp_path / 'app')
    finder = runtime.InstalledSourceFinder(folder)
    spec = finder.find_spec('strategy')
    assert Path(spec.origin) == folder / 'strategy.py'
    assert isinstance(spec.loader, runtime.CurrentSourceLoader)
    (folder / 'strategy.py').unlink()
    with pytest.raises(ModuleNotFoundError, match='Required editable'):
        finder.find_spec('strategy')


def test_new_editable_modules_and_packages_can_be_imported(tmp_path):
    folder = fake_app(tmp_path / 'app')
    (folder / 'extra.py').write_text('VALUE = 31', encoding='utf8')
    package = folder / 'signals'
    package.mkdir()
    (package / '__init__.py').write_text('from .signal import VALUE', encoding='utf8')
    (package / 'signal.py').write_text('VALUE = 7', encoding='utf8')
    (folder / 'launcher.py').write_text('import extra, signals\ndef main():\n    return extra.VALUE + signals.VALUE\n', encoding='utf8')
    assert invoke(folder).returncode == 38


def test_installation_lock_excludes_launch_independent_of_profile(tmp_path):
    folder = fake_app(tmp_path / 'app')
    with runtime.InstallationLock(tmp_path):
        result = invoke(folder)
        assert result.returncode == 1
        assert 'Close CryptoPilot' in result.stderr
    assert invoke(folder).returncode == 23
    assert (tmp_path / 'install.lock').exists()


def test_frozen_directory_comes_from_executable_not_cwd(monkeypatch, tmp_path):
    executable = tmp_path / 'Installed' / 'runtime' / 'CryptoPilotRuntime.exe'
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(executable))
    assert runtime.default_app_directory() == executable.parent.parent / 'app'
    assert runtime.main(['--app-dir', str(tmp_path / 'other')]) == 1


def test_import_runtime_does_not_load_application():
    result = subprocess.run([sys.executable, '-c',
                             "import sys; import windows_runtime; assert 'app' not in sys.modules; assert 'trader' not in sys.modules"],
                            cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def test_missing_maintenance_fails_before_loading_app(tmp_path):
    folder = fake_app(tmp_path / 'app')
    (tmp_path / 'maintenance' / 'update_installation.py').unlink()
    result = invoke(folder)
    assert result.returncode == 1
    assert 'maintenance files are missing' in result.stderr


def test_recovery_runs_before_source_validation(tmp_path):
    folder = fake_app(tmp_path / 'app')
    (folder / 'trader.py').unlink()
    (tmp_path / 'maintenance' / 'update_installation.py').write_text(
        'class UpdateError(RuntimeError): pass\n'
        'def recover_if_needed(root):\n'
        '    (root / "app" / "trader.py").write_text("# repaired")\n', encoding='utf8')
    assert invoke(folder).returncode == 23


def test_update_dispatch_does_not_import_or_start_app(tmp_path):
    folder = fake_app(tmp_path / 'app')
    (folder / 'launcher.py').write_text('raise AssertionError("do not run")', encoding='utf8')
    result = invoke(folder, '--update-source', str(tmp_path), '--replace-local-changes')
    assert result.returncode == 0, result.stderr
    assert '"updated": true' in result.stdout
    assert '"replace": true' in result.stdout


def test_replace_requires_explicit_update_mode(tmp_path):
    folder = fake_app(tmp_path / 'app')
    result = invoke(folder, '--replace-local-changes')
    assert result.returncode == 1
    assert 'requires --update-source' in result.stderr


def test_runtime_tls_check_is_offline_and_never_imports_app(tmp_path):
    folder = fake_app(tmp_path / 'app')
    (folder / 'launcher.py').write_text('raise AssertionError("must not load app")', encoding='utf8')
    result = invoke(folder, '--check-runtime')
    assert result.returncode == 0, result.stderr
    assert '"certificate_verification": true' in result.stdout
    assert '"hostname_verification": true' in result.stdout


def test_incomplete_tls_runtime_reports_repair_error(monkeypatch):
    import ssl

    def broken_context(**kwargs):
        raise FileNotFoundError('missing trust policy')

    monkeypatch.setattr(ssl, 'create_default_context', broken_context)
    with pytest.raises(runtime.InstallationError, match='TLS runtime or certificate store is incomplete'):
        runtime.check_tls_runtime()


@pytest.mark.parametrize('verify_mode, check_hostname', [(0, True), (2, False)])
def test_runtime_tls_check_requires_certificate_and_hostname_verification(monkeypatch, verify_mode, check_hostname):
    import ssl
    from types import SimpleNamespace
    monkeypatch.setattr(ssl, 'create_default_context', lambda **kwargs:
                        SimpleNamespace(verify_mode=verify_mode, check_hostname=check_hostname))
    with pytest.raises(runtime.InstallationError, match='must both be enabled'):
        runtime.check_tls_runtime()
