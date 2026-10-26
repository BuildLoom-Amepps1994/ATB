"""Offline source updates for a stopped, per-user CryptoPilot installation.

This helper deliberately imports no application code and never accesses the
portfolio, credentials, network, or dependency installer. Hashes detect damaged
copies; they do not authenticate the author of an update.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

MODULES = (
    'app.py', 'credentials.py', 'decision.py', 'feed.py', 'launcher.py', 'market_data.py',
    'observability.py', 'orders.py', 'paper_exchange.py', 'recovery.py', 'risk.py',
    'storage.py', 'strategy.py', 'trader.py', 'updater.py',
)
METADATA_FILES = ('requirements-lock.txt',)
REQUIRED_ASSETS = ('static/index.html', 'static/dashboard.js', 'static/dashboard-core.js')
ASSET_EXTENSIONS = {'.html', '.js', '.css', '.svg', '.png', '.jpg', '.jpeg',
                    '.webp', '.ico', '.json', '.woff', '.woff2', '.ttf', '.map'}
JOURNAL = 'update-transaction.json'
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_RESERVED = re.compile(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?\Z', re.I)


class UpdateError(RuntimeError):
    """A safe, local maintenance failure suitable for display to the user."""


class InstallationBusy(UpdateError):
    pass


def _absolute(path: str | Path) -> Path:
    # Resolve only after checking for reparse points; resolve() hides junctions.
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _plain_path(path: Path) -> None:
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise UpdateError('Symbolic links and Windows junctions are not supported in installation/update paths.')


def _overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _protect_data(root: Path) -> None:
    default = _absolute(Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.cryptopilot'))) / 'CryptoPilot')
    candidates = [default]
    if os.environ.get('CRYPTOPILOT_DATA_DIR'):
        candidates.append(_absolute(os.environ['CRYPTOPILOT_DATA_DIR']))
    if any(_overlap(root, candidate) for candidate in candidates):
        raise UpdateError('The installation/update location must be separate from the portfolio data folder.')


def _installation_root(path: str | Path) -> Path:
    root = _absolute(path)
    _plain_path(root)
    _protect_data(root)
    if not root.is_dir() or root == Path(root.anchor):
        raise UpdateError('Select an existing CryptoPilot installation folder.')
    return root


class InstallationLock:
    """A stable OS lock, shared by maintenance and the installed runtime."""
    def __init__(self, root: str | Path):
        self.root = _installation_root(root)
        self.handle = None

    def __enter__(self):
        path = self.root / 'install.lock'
        _plain_path(path)
        try:
            self.handle = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o600), 'r+b')
            self.handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if self.handle is not None:
                self.handle.close()
                self.handle = None
            raise InstallationBusy('Close CryptoPilot and any installer/update window before updating its files.') from exc
        return self

    def __exit__(self, *_):
        if self.handle is not None:
            self.handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def _relative_file(name: str) -> bool:
    if not isinstance(name, str) or '\\' in name or ':' in name or '\0' in name:
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or str(path) != name:
        return False
    if any(part in ('.', '..') or part.endswith(('.', ' ')) or _RESERVED.fullmatch(part)
           for part in path.parts):
        return False
    return name in (*MODULES, *METADATA_FILES) or (len(path.parts) > 1 and path.parts[0] == 'static'
                               and path.suffix.lower() in ASSET_EXTENSIONS)


def _load_json(path: Path) -> dict:
    _plain_path(path)
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise UpdateError('The installation metadata is too large.')
        value = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise UpdateError('Cannot read valid CryptoPilot installation metadata.') from exc
    if not isinstance(value, dict):
        raise UpdateError('Invalid CryptoPilot installation metadata.')
    return value


def _validate_manifest(value: dict) -> dict:
    files = value.get('app_files')
    if (value.get('format') != 1 or value.get('product') != 'CryptoPilot'
            or not isinstance(value.get('installed_version'), str)
            or not 1 <= len(value['installed_version']) <= 100
            or not isinstance(value.get('requirements_sha256'), str)
            or not _HASH.fullmatch(value['requirements_sha256'])
            or not isinstance(files, dict) or not 1 <= len(files) <= 5000):
        raise UpdateError('Unsupported CryptoPilot installation metadata.')
    if not set((*MODULES, *METADATA_FILES, *REQUIRED_ASSETS)).issubset(files):
        raise UpdateError('Installation metadata is missing required application files.')
    seen = set()
    for name, digest in files.items():
        if (not _relative_file(name) or name.casefold() in seen
                or not isinstance(digest, str) or not _HASH.fullmatch(digest)):
            raise UpdateError('Installation metadata contains an unsafe application path or hash.')
        seen.add(name.casefold())
    return value


def read_manifest(root: str | Path) -> dict:
    return _validate_manifest(_load_json(_installation_root(root) / 'installation.json'))


def _read_file(path: Path) -> bytes:
    _plain_path(path)
    try:
        if not path.is_file() or path.stat().st_size > 100 * 1024 * 1024:
            raise UpdateError('Application update contains a missing or oversized file.')
        return path.read_bytes()
    except OSError as exc:
        raise UpdateError('Cannot read an application update file.') from exc


def _inventory(folder: Path, *, compile_sources: bool = False) -> dict[str, str]:
    _plain_path(folder)
    if not folder.is_dir():
        raise UpdateError('Application source folder does not exist.')
    paths = [*MODULES, *METADATA_FILES]
    assets = folder / 'static'
    _plain_path(assets)
    if not assets.is_dir():
        raise UpdateError('Application source is missing its static dashboard folder.')
    try:
        for directory, subdirs, filenames in os.walk(assets, followlinks=False):
            current = Path(directory)
            _plain_path(current)
            for name in subdirs:
                _plain_path(current / name)
            for name in filenames:
                relative = (current / name).relative_to(folder).as_posix()
                if not _relative_file(relative):
                    raise UpdateError('Unsupported or unsafe file in the dashboard asset folder.')
                paths.append(relative)
    except OSError as exc:
        raise UpdateError('Cannot inspect the dashboard asset folder.') from exc
    if len(paths) > 5000 or not set(REQUIRED_ASSETS).issubset(paths):
        raise UpdateError('Application source is missing required dashboard files or contains too many files.')
    result = {}
    seen = set()
    for name in sorted(paths):
        if name.casefold() in seen:
            raise UpdateError('Application source contains Windows path-name collisions.')
        seen.add(name.casefold())
        data = _read_file(folder / name)
        if compile_sources and name in MODULES:
            try:
                compile(data, name, 'exec', dont_inherit=True)
            except (SyntaxError, ValueError, OverflowError) as exc:
                raise UpdateError(f'Python syntax validation failed for {name}. The installed app was not replaced.') from exc
        result[name] = hashlib.sha256(data).hexdigest()
    return result


def _write_json(path: Path, value: dict) -> None:
    _plain_path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf8', newline='\n') as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _has_untracked_source(folder: Path, expected: dict) -> bool:
    for directory, subdirs, files in os.walk(folder, followlinks=False):
        current = Path(directory)
        _plain_path(current)
        subdirs[:] = [name for name in subdirs if name != '__pycache__']
        for name in subdirs:
            _plain_path(current/name)
        for name in files:
            path = current/name
            _plain_path(path)
            if path.relative_to(folder).as_posix() not in expected:
                return True
    return False


def _transaction_paths(root: Path, transaction: dict) -> tuple[Path, Path]:
    backup_name, staging_name = transaction.get('backup'), transaction.get('staging')
    if (transaction.get('format') != 1 or transaction.get('product') != 'CryptoPilot'
            or not isinstance(backup_name, str) or not isinstance(staging_name, str)
            or not re.fullmatch(r'backups/(?:update|setup)-[A-Za-z0-9_-]+', backup_name)
            or not re.fullmatch(r'\.(?:update|setup)-[A-Za-z0-9_-]+', staging_name)):
        raise UpdateError('The interrupted-update record is invalid; preserve the installation for manual recovery.')
    backup, staging = root / backup_name, root / staging_name
    _plain_path(backup)
    _plain_path(staging)
    return backup, staging


def _finish_setup_activation(root: Path, marker: Path, backup: Path, staging: Path) -> bool:
    """Complete a Setup source swap that was interrupted, for example by a power cut.

    Setup records this journal only after it has installed the new runtime and this
    helper, so the new source is the one that matches them. Restoring the previous
    source would pair it with a runtime that may require modules it does not have.
    The previous source stays in the backup folder.
    """
    application, staged_app, staged_record = root / 'app', staging / 'app', staging / 'installation.json'
    for path in (application, staged_app, staged_record, backup / 'app'):
        _plain_path(path)
    try:
        if staged_app.is_dir():
            # Check the new record before anything moves.
            _validate_manifest(_load_json(staged_record))
            if application.exists():
                kept = backup / 'app'
                if kept.exists():
                    kept = backup / ('interrupted-app-' + uuid.uuid4().hex)
                backup.mkdir(parents=True, exist_ok=True)
                os.replace(application, kept)
            os.replace(staged_app, application)
        if staged_record.exists():
            _validate_manifest(_load_json(staged_record))
            os.replace(staged_record, root / 'installation.json')
        if not application.is_dir():
            raise UpdateError('Interrupted Setup left no application to activate. Run CryptoPilot Setup again; '
                              'your previous source is kept in the backups folder.')
        _validate_manifest(_load_json(root / 'installation.json'))
        marker.unlink()
    except OSError as exc:
        raise UpdateError('Cannot finish the interrupted Setup update. Close CryptoPilot, run Setup again, '
                          'and preserve the backup folder.') from exc
    try:
        staging.rmdir()
    except OSError:
        pass
    return True


def recover_if_needed(root: str | Path) -> bool:
    """Repair an installation whose last update was interrupted.

    A source update (backups/update-*) is rolled back to the previous source. A
    Setup activation (backups/setup-*) is rolled forward, because Setup had already
    replaced the runtime. Caller MUST hold InstallationLock for the complete
    recovery and launch. Old code and failed new code remain in the backup folder.
    """
    root = _installation_root(root)
    marker = root / JOURNAL
    _plain_path(marker)
    if not marker.exists():
        return False
    transaction = _load_json(marker)
    backup, staging = _transaction_paths(root, transaction)
    if transaction['backup'].startswith('backups/setup-'):
        return _finish_setup_activation(root, marker, backup, staging)
    previous = _validate_manifest(_load_json(backup / 'installation.json'))
    application = root / 'app'
    old_application = backup / 'app'
    _plain_path(application)
    _plain_path(old_application)
    try:
        if old_application.exists():
            if application.exists():
                interrupted = backup / ('interrupted-app-' + uuid.uuid4().hex)
                os.replace(application, interrupted)
            os.replace(old_application, application)
        if not application.is_dir():
            raise UpdateError('Interrupted update has no previous application to restore; preserve its backups.')
        _write_json(root / 'installation.json', previous)
        marker.unlink()
    except OSError as exc:
        raise UpdateError('Cannot finish interrupted-update recovery. Close CryptoPilot and preserve the backup folder.') from exc
    return True


def _clean_staging(root: Path, staging: Path) -> None:
    # Never recursively delete a caller-supplied or resolved computed target.
    if staging.parent != root or not re.fullmatch(r'\.update-[a-f0-9]{32}', staging.name):
        raise UpdateError('Refusing to clean a staging folder outside this installation.')
    _plain_path(staging)
    if staging.exists():
        for directory, subdirs, files in os.walk(staging, followlinks=False):
            for name in (*subdirs, *files):
                _plain_path(Path(directory) / name)
        shutil.rmtree(staging)


def update_installation(root: str | Path, source: str | Path, *, replace_local_changes: bool = False) -> dict:
    """Stage an offline source update, back up old source, then replace it.

    Runtime/dependency changes require a new installer. Credentials and data are
    never copied. A modified installed app requires an explicit override, and is
    still preserved in a unique backup before replacement.
    """
    root = _installation_root(root)
    source = _absolute(source)
    _plain_path(source)
    _protect_data(source)
    if _overlap(source, root):
        raise UpdateError('Choose a source folder outside the installed application folder.')
    with InstallationLock(root):
        recover_if_needed(root)
        previous = read_manifest(root)
        application = root / 'app'
        actual = _inventory(application)
        if (actual != previous['app_files'] or _has_untracked_source(application,previous['app_files'])) and not replace_local_changes:
            raise UpdateError('Installed source has local changes. Preserve them or use --replace-local-changes; a backup will be kept.')
        required_hash = hashlib.sha256(_read_file(source / 'requirements-lock.txt')).hexdigest()
        if required_hash != previous['requirements_sha256']:
            raise UpdateError('This source needs a different dependency/runtime bundle. Build and use a new Windows installer.')
        proposed = _inventory(source, compile_sources=True)
        if actual == proposed:
            if actual != previous['app_files']:
                # Local edits that already match this source are now the
                # recorded state, so the next update needs no override.
                _write_json(root / 'installation.json',
                            {**previous, 'app_files': proposed, 'updated_at': datetime.now(timezone.utc).isoformat()})
            return {'updated': False, 'files': len(proposed), 'backup': None, 'changed_files': []}
        staging = root / ('.update-' + uuid.uuid4().hex)
        backup = root / 'backups' / ('update-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex)
        _plain_path(backup)
        staged_app = staging / 'app'
        committed = False
        try:
            staged_app.mkdir(parents=True)
            for name, digest in proposed.items():
                data = _read_file(source / name)
                if hashlib.sha256(data).hexdigest() != digest:
                    raise UpdateError('Source changed while being staged. Save all edits and retry the update.')
                target = staged_app / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            if _inventory(staged_app, compile_sources=True) != proposed:
                raise UpdateError('Staged source failed integrity validation.')
            updated = {**previous, 'app_files': proposed, 'updated_at': datetime.now(timezone.utc).isoformat()}
            backup.mkdir(parents=True)
            _write_json(backup / 'installation.json', previous)
            transaction = {'format': 1, 'product': 'CryptoPilot',
                           'backup': backup.relative_to(root).as_posix(), 'staging': staging.name}
            _write_json(root / JOURNAL, transaction)
            try:
                os.replace(application, backup / 'app')
                os.replace(staged_app, application)
                _write_json(root / 'installation.json', updated)
                if _inventory(application, compile_sources=True) != proposed:
                    raise UpdateError('Installed source failed final integrity validation.')
                (root / JOURNAL).unlink()
                committed = True
            except BaseException:
                recover_if_needed(root)
                raise
            changed = sorted(name for name in set(actual) | set(proposed) if actual.get(name) != proposed.get(name))
            return {'updated': True, 'files': len(proposed), 'backup': str(backup), 'changed_files': changed}
        except OSError as exc:
            raise UpdateError('The source update could not be completed. The previous app was retained or restored; inspect backups if recovery was interrupted.') from exc
        finally:
            # Keep staging if a recovery marker remains; it is recovery evidence.
            if not (root / JOURNAL).exists() and staging.exists():
                try:
                    _clean_staging(root, staging)
                except OSError:
                    # Leftover staging is harmless; it must not replace the
                    # real error (or the success) with a cleanup failure.
                    if committed:
                        pass
                    elif sys.exc_info()[0] is None:
                        raise


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description='Update installed CryptoPilot source offline with a rollback backup.')
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--replace-local-changes', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = update_installation(args.root, args.source, replace_local_changes=args.replace_local_changes)
    except UpdateError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
