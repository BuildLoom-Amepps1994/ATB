"""In-app updates from this project's GitHub Releases.

The dashboard asks for the newest published release. When the installed Windows
build is older, the user can download its Setup file, which is verified against
the SHA-256 that GitHub records for the asset (and the published .sha256 file)
before it runs. Setup waits for this process to exit, replaces the runtime and
app folder (keeping a backup of the old source), then starts CryptoPilot again.

Only the release page of REPOSITORY over HTTPS is trusted. A matching checksum
proves the file is the one published there; it is not a code signature.
Portfolio data and credentials live elsewhere and are never touched.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# Bump together with a release tag vX.Y.Z; the release workflow refuses a mismatch.
APP_VERSION = '1.4.0'
REPOSITORY = 'BuildLoom-Amepps1994/ATB'
LATEST_URL = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
RELEASES_PAGE = f'https://github.com/{REPOSITORY}/releases'
DOWNLOAD_PREFIX = f'https://github.com/{REPOSITORY}/releases/download/'
CHECK_SECONDS = 6 * 3600
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_INSTALLER_BYTES = 400 * 1024 * 1024
LOCK_WAIT_SECONDS = 180
_VERSION = re.compile(r'v?(\d+)\.(\d+)\.(\d+)\Z')
_SHA256 = re.compile(r'[0-9a-f]{64}')


class UpdateError(RuntimeError):
    """A safe update failure message suitable for the dashboard."""


def parse_version(text) -> tuple[int, int, int] | None:
    match = _VERSION.fullmatch(text.strip()) if isinstance(text, str) else None
    return tuple(int(part) for part in match.groups()) if match else None


def installation_root() -> Path | None:
    """The per-user installation folder, only when running from the installed runtime."""
    if not getattr(sys, 'frozen', False) or os.name != 'nt':
        return None
    app_dir = Path(__file__).resolve().parent
    root = app_dir.parent
    if app_dir.name != 'app' or not (root / 'installation.json').is_file():
        return None
    if not (root / 'runtime' / 'CryptoPilotRuntime.exe').is_file():
        return None
    return root


def current_version(root: Path | None = None) -> str:
    if root is not None:
        try:
            installed = json.loads((root / 'installation.json').read_text(encoding='utf-8')).get('installed_version')
            if parse_version(installed):
                return installed
        except (OSError, ValueError, AttributeError):
            pass
    return APP_VERSION


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    # Release downloads redirect to GitHub's asset CDN. Never follow a downgrade to HTTP.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith('https://'):
            raise urllib.error.HTTPError(newurl, code, 'Refusing a non-HTTPS redirect.', headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_opener():
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except (ImportError, OSError, ValueError):
        context = ssl.create_default_context()
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), _HttpsOnlyRedirects())

    def open_url(url: str, accept: str, timeout: float):
        if not url.startswith('https://'):
            raise UpdateError('Update addresses must use HTTPS.')
        request = urllib.request.Request(url, headers={'Accept': accept, 'User-Agent': 'CryptoPilot-Updater/' + APP_VERSION})
        return opener.open(request, timeout=timeout)
    return open_url


def _read_limited(response, limit: int) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise UpdateError('The update server returned an unexpectedly large response.')
    return data


def select_installer(release: dict) -> dict:
    """Pick the Setup asset of a published release and its expected fingerprint."""
    if not isinstance(release, dict) or release.get('draft') or release.get('prerelease'):
        raise UpdateError('No published release is available.')
    tag = release.get('tag_name')
    version = parse_version(tag)
    if version is None:
        raise UpdateError('The latest release tag is not a vX.Y.Z version.')
    text = '.'.join(map(str, version))
    name = f'CryptoPilot-Setup-{text}.exe'
    assets = {asset.get('name'): asset for asset in release.get('assets') or [] if isinstance(asset, dict)}
    prefix = f'{DOWNLOAD_PREFIX}{tag}/'
    setup = assets.get(name)
    if setup is None or setup.get('browser_download_url') != prefix + name:
        raise UpdateError('The latest release has no Windows installer yet.')
    size = setup.get('size')
    if not isinstance(size, int) or not 0 < size <= MAX_INSTALLER_BYTES:
        raise UpdateError('The release installer has an invalid size.')
    digest = setup.get('digest')
    digest = digest[7:].lower() if isinstance(digest, str) and digest.lower().startswith('sha256:') else None
    if digest is not None and not _SHA256.fullmatch(digest):
        digest = None
    checksum = assets.get(name + '.sha256')
    checksum_url = prefix + name + '.sha256' if checksum and checksum.get('browser_download_url') == prefix + name + '.sha256' else None
    if digest is None and checksum_url is None:
        raise UpdateError('The release installer has no published checksum, so it will not be used.')
    notes = release.get('body') if isinstance(release.get('body'), str) else ''
    page = release.get('html_url') if isinstance(release.get('html_url'), str) and release['html_url'].startswith(RELEASES_PAGE) else RELEASES_PAGE
    return {'version': text, 'tag': tag, 'name': name, 'url': prefix + name, 'size': size,
            'sha256': digest, 'checksum_url': checksum_url, 'notes': notes[:4000], 'page': page,
            'published_at': release.get('published_at')}


class Updater:
    def __init__(self, open_url=None, download_dir: Path | None = None, root: Path | None = ...,
                 launch=None, clock=time.time):
        self.open_url = open_url or _default_opener()
        self.root = installation_root() if root is ... else root
        self.download_dir = download_dir
        self.launch = launch or launch_installer
        self.clock = clock
        self.lock = threading.Lock()
        self.install_lock = threading.Lock()
        self.checked_at = 0.0
        self.release: dict | None = None
        self.error: str | None = None
        self.phase = 'idle'

    def _status(self) -> dict:
        current = current_version(self.root)
        latest = self.release['version'] if self.release else None
        available = bool(latest and parse_version(latest) > parse_version(current))
        return {'current_version': current, 'latest_version': latest, 'available': available,
                'can_install': available and self.root is not None,
                'installed': self.root is not None, 'phase': self.phase,
                'checked_at': self.checked_at or None, 'error': self.error,
                'notes': self.release['notes'] if available else '',
                'release_page': self.release['page'] if self.release else RELEASES_PAGE,
                'size': self.release['size'] if available else None,
                'disabled': os.environ.get('CRYPTOPILOT_NO_UPDATE_CHECK') == '1'}

    def check(self, force: bool = False) -> dict:
        with self.lock:
            if os.environ.get('CRYPTOPILOT_NO_UPDATE_CHECK') == '1':
                return self._status()
            if not force and self.checked_at and self.clock() - self.checked_at < CHECK_SECONDS:
                return self._status()
            self.checked_at = self.clock()
            try:
                with self.open_url(LATEST_URL, 'application/vnd.github+json', 15) as response:
                    release = json.loads(_read_limited(response, MAX_METADATA_BYTES).decode('utf-8'))
                self.release = select_installer(release)
                self.error = None
            except urllib.error.HTTPError as exc:
                self.error = 'No published release was found.' if exc.code == 404 else 'The update server is unavailable. Try again later.'
            except UpdateError as exc:
                self.error = str(exc)
            except (OSError, ValueError, UnicodeError):
                self.error = 'Could not reach GitHub to check for updates.'
            return self._status()

    def _download(self, release: dict) -> Path:
        if self.download_dir is None:
            raise UpdateError('No download folder is configured.')
        expected = release['sha256']
        if release['checksum_url']:
            with self.open_url(release['checksum_url'], 'application/octet-stream', 30) as response:
                found = _SHA256.search(_read_limited(response, 4096).decode('ascii', 'replace').lower())
            if found is None:
                raise UpdateError('The published checksum file is not valid.')
            if expected is not None and expected != found.group(0):
                raise UpdateError('The release checksums disagree. The update was not installed.')
            expected = found.group(0)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        target = self.download_dir / release['name']
        partial = target.with_name(target.name + '.part')
        hasher, total = hashlib.sha256(), 0
        try:
            with self.open_url(release['url'], 'application/octet-stream', 60) as response, partial.open('wb') as handle:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > release['size']:
                        raise UpdateError('The downloaded installer is larger than published.')
                    hasher.update(chunk)
                    handle.write(chunk)
            if total != release['size']:
                raise UpdateError('The installer download was incomplete. Try again.')
            if hasher.hexdigest() != expected:
                raise UpdateError('The downloaded installer failed its checksum and was deleted.')
            os.replace(partial, target)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        return target

    def install(self, idle, shutdown) -> dict:
        """Download, verify and start Setup, then ask the app to exit so Setup can proceed."""
        if self.root is None:
            raise UpdateError('In-app installs need the Windows installed version. Download the update from the releases page.')
        busy = UpdateError('Stop the bot and wait for any order to finish before updating.')
        if not idle():
            raise busy
        if not self.install_lock.acquire(blocking=False):
            raise UpdateError('An update is already in progress.')
        try:
            status = self.check(force=True)
            if not status['can_install']:
                raise UpdateError(status['error'] or 'CryptoPilot is already up to date.')
            self.phase = 'downloading'
            try:
                installer = self._download(self.release)
            except urllib.error.URLError:
                raise UpdateError('The installer download failed. Check the connection and try again.') from None
            # The download can take a while; refuse if trading started meanwhile.
            if not idle():
                raise busy
            self.phase = 'installing'
            self.launch(installer, self.root, self.download_dir / 'setup.log')
            if shutdown is not None:
                threading.Timer(1.5, shutdown).start()
            return {'ok': True, 'version': self.release['version']}
        except BaseException:
            self.phase = 'idle'
            raise
        finally:
            self.install_lock.release()


def launch_installer(installer: Path, root: Path, log: Path) -> None:
    """Start Setup detached; it waits for this app to release the installation lock."""
    if os.name != 'nt':
        raise UpdateError('Automatic installs are only available on Windows.')
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(installer), '/SILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                      f'/WAITLOCK={LOCK_WAIT_SECONDS}', '/RELAUNCH=1', f'/DIR={root}', f'/LOG={log}'],
                     cwd=str(installer.parent), creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
