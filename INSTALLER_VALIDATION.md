# Editable Windows installer validation

Version **1.1.0**, 23 October 2026. This extends the existing CryptoPilot engine with a real per-user Windows Setup executable and separately editable installed application files. No strategy or live-trading activation behavior was changed for this packaging request.

## Delivered behavior

- Setup bundles Python/dependencies, requires no administrator access or separate Python installation, and configures Start menu shortcuts plus optional desktop shortcut.
- Installed application code and dashboard assets remain ordinary files in `%LOCALAPPDATA%\Programs\CryptoPilot\app`. The executable loads that source on each start. Core application modules are excluded from its frozen archive; missing files fail closed rather than falling back to older embedded code.
- The runtime holds an installation-wide OS lock before reading app source. Setup, uninstall and source updates use the same lock, including when the application uses a custom portfolio folder.
- The offline updater checks syntax, required files and the dependency fingerprint. It stages source, detects direct edits, requires explicit replacement for those edits, keeps a backup, and uses a recovery record for interrupted replacement. It never downloads updates or starts trading.
- Installation/updates keep data outside the program folder. Uninstall preserves portfolio/settings/credentials and editable source/backups; it removes runtime files and configured shortcuts.
- Default behavior is still stopped paper mode on every launch. Live activation remains explicit and independent of installation.

## Actual checks completed

**199 automated Python tests passed**, including the previous 149 engine/security/research tests and 50 focused installer, editable-runtime and source-update tests. The focused tests include missing files, syntax errors, same-size/same-time source changes, stale bytecode, source precedence over working-directory copies, locks, malformed metadata, dependency mismatch, junction rejection, data-path overlap, local edits, extra local files, staged-copy validation, failed rename rollback and recovery at three interrupted-update phases.

The **actual final Setup executable** was tested end-to-end using `/TESTINSTALL=1` in a fresh workspace folder. This test flag skips real user uninstall registration/shortcuts; it uses the same runtime, file installation, activation, upgrade, locking and uninstall code. No normal CryptoPilot installation or data folder was replaced.

The complete smoke test passed:

1. Silent isolated installation with a path containing spaces.
2. Frozen installed runtime starts stopped, paper, and disconnected; its dashboard and assets load.
3. A direct edit to installed `app.py` adds a test endpoint; after restart, it returns the edited value and its actual installed source path.
4. While that runtime is active, both source updating and Setup upgrade are blocked; the running app remains usable.
5. Once stopped, a normal update detects the direct edit and refuses to overwrite it.
6. An intentional replacement backs up the edited source and applies another source change. Restart proves the new code executes. The runtime executable's SHA-256 remains unchanged.
7. Setup upgrade backs up those updated source files and activates the release source.
8. A deliberately changed paper cash value, configured scan interval and sentinel data remain intact through source updates, restarts and Setup upgrade.
9. Uninstall removes the runtime and retains editable files, backups and the separate test portfolio/settings.

The smoke test uses a disabled credential backend, no browser auto-open, an isolated portfolio and only local HTTP reads. **No exchange requests, real credential operations or live orders were performed.** Test-only source endpoints were added only inside temporary test folders and are absent from the release payload.

Build stack: Windows x64, Python **3.12.14**, PyInstaller **6.22.3**, Inno Setup **6.7.3**. The Inno compiler came from the official upstream release and its Authenticode signature was verified. The delivered CryptoPilot Setup/runtime themselves are **unsigned**. Optional dependency warnings from the previous build and the upstream Starlette/httpx test warning remain as documented in `VALIDATION.md`.

## Limits and maintenance notes

- Normal Start menu registration/shortcuts were reviewed in the installer definition but intentionally not installed into the real user profile during the test. A clean-machine VM and other Windows builds were not tested.
- Close CryptoPilot before edits or updates; existing imported code is not hot-reloaded. Source updates retain backups, but power-loss/disk-failure immunity is not guaranteed. No real power cut was performed; interruption/recovery was simulated in tests.
- Replacing Python/native dependencies or the dependency lock needs a new installer. The conservative source updater handles the existing eight core modules, the dependency-lock metadata and dashboard assets; new Python modules can be edited/added directly, but are not automatically added to that updater's allowlist.
- Local source/maintenance files are user-owned code. Hashes detect accidental changes; they do not authenticate authors or protect a compromised Windows account. No remote maintenance service, external access endpoint, scheduled updater or autostart was added.
- No authenticated exchange execution, actual OS credential writes or trading performance was tested for this task. Software stops still require a running, connected process. No profitable return is guaranteed.

Build and test commands are provided in `WINDOWS_INSTALL.md`. The opt-in real installer test is `python tests/smoke_installer.py --setup PATH --work EXISTING_SCRATCH_FOLDER`; it retains its isolated logs/data for inspection and never installs to the normal location.
