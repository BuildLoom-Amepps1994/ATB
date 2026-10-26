"""Windows dependency runtime for the editable, per-user installation.

The frozen executable contains Python and third-party dependencies. Application
code lives next to ``runtime/`` in ``app/`` and is read from source on each start.
Never change sys._MEIPASS: PyInstaller still needs its original dependency path.
"""
from __future__ import annotations

import argparse
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import sys
import tokenize
from pathlib import Path


REQUIRED_MODULES = frozenset({
    'launcher', 'app', 'credentials', 'storage', 'market_data', 'trader',
    'strategy', 'recovery', 'updater',
    'decision', 'feed', 'observability', 'orders', 'paper_exchange', 'risk',
    'micro', 'micro_book', 'micro_economics', 'micro_policy', 'micro_strategy', 'micro_venue',
    'money', 'venue_rules',
})
REQUIRED_ASSETS = ('index.html', 'dashboard.js', 'dashboard-core.js')


class InstallationError(RuntimeError):
    """A local installation cannot safely be started."""


def default_app_directory() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent.parent / 'app'
    return Path(__file__).resolve().parent


def validate_app_directory(directory: Path) -> Path:
    """Check source syntax/assets without executing application or credentials."""
    directory = directory.expanduser().resolve()
    if not directory.is_dir():
        raise InstallationError(f'Application folder is missing: {directory}')
    required = [directory / (name + '.py') for name in sorted(REQUIRED_MODULES)]
    required += [directory / 'static' / name for name in REQUIRED_ASSETS]
    missing = [str(path.relative_to(directory)) for path in required if not path.is_file()]
    if missing:
        raise InstallationError('Application files are missing: ' + ', '.join(missing))
    # Compile from current source; stale bytecode must never hide a syntax error.
    for source in sorted(directory.rglob('*.py')):
        if '__pycache__' in source.parts:
            continue
        try:
            with tokenize.open(source) as handle:
                compile(handle.read(), str(source), 'exec', dont_inherit=True)
        except (OSError, UnicodeError, SyntaxError) as exc:
            relative = source.relative_to(directory)
            raise InstallationError(f'Invalid application source: {relative} ({type(exc).__name__}).') from exc
    return directory


class InstallationLock:
    """Persistent first-byte OS lock, shared with the installer and updater.

    A second process must lock this same byte, not delete or replace the file.
    The OS releases the lock on a crash. No account, profile or port is consulted.
    """
    def __init__(self, installation: Path):
        self.path = installation / 'install.lock'
        self.handle = None

    def __enter__(self):
        try:
            self.handle = os.fdopen(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600), 'r+b')
            self.handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.handle.seek(0)
            self.handle.write(b'\0')
            self.handle.flush()
        except OSError as exc:
            if self.handle is not None:
                self.handle.close()
                self.handle = None
            raise InstallationError('Close CryptoPilot and any installer or updater before continuing. '
                                    'The installation is running, updating, or not writable.') from exc
        return self

    def __exit__(self, *_):
        if self.handle is not None:
            try:
                self.handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None


class CurrentSourceLoader(importlib.machinery.SourceFileLoader):
    """Always compile installed source, including same-size/same-time edits."""
    def get_code(self, fullname):
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)


class InstalledSourceFinder(importlib.abc.MetaPathFinder):
    """Prefer editable application sources over frozen dependency finders.

    Required local modules never fall back to a hidden frozen or working-folder
    copy. Additional Python modules/packages in app/ are editable too.
    """
    def __init__(self, directory: Path):
        self.directory = directory

    def find_spec(self, fullname, path=None, target=None):
        relative = Path(*fullname.split('.'))
        module_path = self.directory / relative.with_suffix('.py')
        package_path = self.directory / relative / '__init__.py'
        if module_path.is_file():
            return importlib.util.spec_from_file_location(
                fullname, module_path, loader=CurrentSourceLoader(fullname, str(module_path)))
        if package_path.is_file():
            return importlib.util.spec_from_file_location(
                fullname, package_path, loader=CurrentSourceLoader(fullname, str(package_path)),
                submodule_search_locations=[str(package_path.parent)])
        if fullname in REQUIRED_MODULES:
            raise ModuleNotFoundError(f'Required editable application module is missing: {fullname}', name=fullname)
        return None


def load_maintenance(installation: Path):
    """Load only this install's maintenance helper, without app path searching."""
    path = installation / 'maintenance' / 'update_installation.py'
    if not path.is_file():
        raise InstallationError('Installation maintenance files are missing. Repair or reinstall CryptoPilot.')
    name = '_cryptopilot_installed_maintenance'
    spec = importlib.util.spec_from_file_location(name, path, loader=CurrentSourceLoader(name, str(path)))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def run_application(directory: Path, installation: Path) -> int:
    """Hold the installation lock before validation or any external app import."""
    with InstallationLock(installation):
        maintenance = load_maintenance(installation)
        try:
            maintenance.recover_if_needed(installation)
        except maintenance.UpdateError as exc:
            raise InstallationError(str(exc)) from exc
        directory = validate_app_directory(directory)
        if REQUIRED_MODULES.intersection(sys.modules):
            raise InstallationError('Application modules were loaded before the editable runtime started.')
        finder = InstalledSourceFinder(directory)
        sys.meta_path.insert(0, finder)
        try:
            import launcher
            return int(launcher.main())
        finally:
            sys.meta_path.remove(finder)


def check_tls_runtime() -> dict:
    """Offline TLS/trust-store check; never load the app or contact an exchange."""
    import ssl
    import certifi
    try:
        context = ssl.create_default_context(cafile=certifi.where())
    except (OSError, ValueError) as exc:
        raise InstallationError('The TLS runtime or certificate store is incomplete. '
                                'Repair or reinstall CryptoPilot before connecting to an exchange.') from exc
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise InstallationError('TLS certificate and hostname verification must both be enabled.')
    return {'valid': True, 'tls': ssl.OPENSSL_VERSION, 'certificate_verification': True,
            'hostname_verification': True}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='CryptoPilot editable Windows runtime')
    parser.add_argument('--app-dir', type=Path, help='Installed app folder (must match this runtime).')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--check-app', type=Path, metavar='PATH',
                        help='Validate source syntax and required assets, then exit without starting the app.')
    action.add_argument('--update-source', type=Path, metavar='SOURCE',
                        help='Apply local application source using the installed maintenance helper.')
    action.add_argument('--check-runtime', action='store_true',
                        help='Check bundled TLS and certificates offline without loading the app.')
    parser.add_argument('--replace-local-changes', action='store_true',
                        help='Explicitly allow an update to replace locally edited app files (with a backup).')
    args = parser.parse_args(argv)
    try:
        if args.replace_local_changes and args.update_source is None:
            raise InstallationError('--replace-local-changes requires --update-source.')
        if args.check_runtime:
            print(json.dumps(check_tls_runtime()))
            return 0
        if args.check_app is not None:
            directory = validate_app_directory(args.check_app)
            print(json.dumps({'valid': True, 'app_directory': str(directory)}))
            return 0
        default = default_app_directory().resolve()
        directory = (args.app_dir or default).expanduser().resolve()
        if getattr(sys, 'frozen', False) and directory != default:
            raise InstallationError('This runtime can only launch its installed app folder: ' + str(default))
        installation = directory.parent if getattr(sys, 'frozen', False) or directory.name == 'app' else directory
        if args.update_source is not None:
            maintenance = load_maintenance(installation)
            try:
                result = maintenance.update_installation(
                    installation, args.update_source, replace_local_changes=args.replace_local_changes)
            except maintenance.UpdateError as exc:
                raise InstallationError(str(exc)) from exc
            if result is not None:
                print(json.dumps(result, default=str))
            return 0
        return run_application(directory, installation)
    except InstallationError as exc:
        print('CryptoPilot could not start: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
