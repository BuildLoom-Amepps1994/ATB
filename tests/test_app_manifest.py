"""Every top-level application module is shipped, and every list that names them agrees.

The editable Windows installation copies an explicit set of files (scripts/build_installer.py),
the frozen runtime refuses to start without its required modules (windows_runtime.py) and the
offline source updater packages a fixed module list (maintenance/update_installation.py).
A module added to the repository but not to those lists imports fine from a checkout and fails
only in the installed app, so this test fails first.
"""
import ast
import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import windows_runtime


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load('build_installer_manifest', 'scripts/build_installer.py')
updater = load('update_installation_manifest', 'maintenance/update_installation.py')
APPLICATION_MODULES = {path.stem for path in ROOT.glob('*.py')} - {'windows_runtime'}


def local_imports(stem):
    """Top-level application modules imported anywhere in one module, including inside functions."""
    tree = ast.parse((ROOT / f'{stem}.py').read_text(encoding='utf-8-sig'))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split('.')[0])
    return names & APPLICATION_MODULES


def reachable_from(stem):
    seen, pending = set(), [stem]
    while pending:
        current = pending.pop()
        if current not in seen:
            seen.add(current)
            pending.extend(local_imports(current) - seen)
    return seen


def test_installer_runtime_and_source_updater_ship_every_application_module():
    staged = {name[:-3] for name in builder.REQUIRED_APP if name.endswith('.py')}
    assert staged == APPLICATION_MODULES, 'scripts/build_installer.py REQUIRED_APP is out of step with the repository'
    assert set(windows_runtime.REQUIRED_MODULES) == APPLICATION_MODULES, 'windows_runtime.REQUIRED_MODULES is out of step'
    assert {name[:-3] for name in updater.MODULES} == APPLICATION_MODULES, 'maintenance MODULES is out of step'


def test_everything_the_launcher_imports_is_shipped():
    needed = reachable_from('launcher') | reachable_from('recovery')
    missing = needed - {name[:-3] for name in builder.REQUIRED_APP}
    assert not missing, f'imported by the application but not staged by the installer: {sorted(missing)}'


def test_dashboard_assets_are_all_staged():
    present = {path.relative_to(ROOT).as_posix() for path in (ROOT / 'static').rglob('*') if path.is_file()}
    staged = {name for name in builder.REQUIRED_APP if name.startswith('static/')}
    assert present == staged
    assert set(updater.REQUIRED_ASSETS) == staged
    assert {f'static/{name}' for name in windows_runtime.REQUIRED_ASSETS} == staged
