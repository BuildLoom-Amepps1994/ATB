# Installable, editable CryptoPilot for Windows

Download **CryptoPilot-Setup-X.Y.Z.exe** from the [latest release](https://github.com/BuildLoom-Amepps1994/ATB/releases/latest) and run it. It installs for your Windows user without administrator access, includes its Python runtime, and adds Start menu entries. A desktop shortcut is optional. Setup does not start the application or trading. Existing installations, including a custom folder such as `D:\CryptoPilot`, can be upgraded in place while closed.

Version 1.2 fixes the missing TLS trust-policy file in the prior runtime, keeps certificate/hostname checks enabled, and removes unnecessary TLS initialization from dashboard browser startup. `runtime\CryptoPilotRuntime.exe --check-runtime` verifies TLS configuration offline without opening the app or reading credentials.

## Changes in 1.3

Version 1.3 uses bid quotes for holdings/protective exits and asks for Buy checks. Missing, stale or crossed prices cannot silently fall back to a last trade. Ticker quotes still do not guarantee depth or execution prices. Estimated exit costs are included consistently in tracked equity and unrealised profit/loss.

Settings now have a validated `settings.last-good.json` copy. Corrupt, missing or invalid saved limits trigger a persistent review state, preserve readable evidence, and block live activation until the displayed settings are reviewed and saved. A new valid profile still initializes normally. Recovered limits remain usable in paper mode. If a settings file is unreadable, resolve file access before trying to replace it. Back up the entire data folder with the application closed, including these recovery files.

Manual request completion, fill accounting and candle reentry guards are committed together. A crash after commit can replay the known request result without another order. The offline `recovery.py` report now also lists incomplete manual requests; it does not clear or retry them. Verified exchange-minimum dust stays in valuation/risk but no longer blocks otherwise eligible entries into other assets. Automated consolidation into the same dust asset remains conservative; the existing manual consolidation flow is available.

Backtests now share bounded indicator history and entry-gap limits with the trading rules. The dashboard adds chronological earlier/holdout summaries, a full-period doubled-cost scenario and **Save research report**. The JSON records modeled parameters, dates, coverage, executions and candle hashes, but does not archive the raw market candles. A hash identifies the data; it cannot retrieve historical data. See `BOT_IMPROVEMENTS_1.3.0.md` for the completed verification and remaining limits.

## Quick orders and faster monitoring

The **Manual spot order** ticket supports a USDT-budget Buy and a Sell of the app's tracked position while the bot is stopped. Paper mode simulates both. Live mode first needs the existing eligibility acknowledgement/API connection/live activation; each manual order additionally requires **PLACE LIVE BUY** or **PLACE LIVE SELL**. Confirmation names the exchange and is invalidated by account connection, exchange, mode or settings changes, including changes in another tab. Repeated requests with the same identifier and context return the original result rather than submitting again. Partial sells show the remaining quantity, and a later manual buy can consolidate a verified sub-minimum remainder without discarding its cost or quantity.

Manual orders do not start automatic protection. **Start bot** is required for software stops/trailing checks. With the bot running, held-position checks target **5 seconds** by default (configurable 2–60) independently of the full strategy scan. Slow requests, exchange limits, disconnections and illiquidity can delay checks/fills. These are not exchange-native stops or a subsecond execution guarantee.

Choose **Rapid** for completed **1-minute signals / 5-minute trend confirmation**. Strategy scans can be set as low as 10 seconds and reuse unchanged candles. Rapid applies a distinct volatility/breakout/stop profile, includes assumed fees/slippage in sizing, and retains exposure/daily-loss controls. It does not force a buy/sell on every scan or prove a profitable edge. Automated same-candle reentry guards remain; precision dust may need the manual consolidation flow before automated entries resume. Refer to `RAPID_TRADING_VALIDATION.md` in the source/release for test scope and live-execution limits.

Open **CryptoPilot** from Start. The console and local browser dashboard open. Every application start remains **stopped in paper mode**. Live trading still needs explicit connection, confirmation and Start. This release does not validate live exchange execution or guarantee profit.

## Where the files go

The default installation folder is:

```text
%LOCALAPPDATA%\Programs\CryptoPilot\
    app\                         Editable Python and dashboard files
    runtime\CryptoPilotRuntime.exe
    runtime\_internal\            Bundled interpreter and dependencies
    maintenance\                  Local source-update helper
    installation.json             Installed-file/dependency fingerprints
    backups\                      Source backups from updates/upgrades
```

Your settings, SQLite portfolio/trades and existing live-order recovery records remain separately in **%LOCALAPPDATA%\CryptoPilot** (or your configured `CRYPTOPILOT_DATA_DIR`). Saved exchange credentials stay in the OS credential manager. Installing/updating/uninstalling does not delete that data.

The Start menu's **CryptoPilot editable files** entry opens `app`. These are ordinary files owned by your Windows user. Codex can edit them when you request a change in a chat with local file access; no permanent remote access or background updater is added. Close the app before applying edits and restart afterward. Existing imported code is not hot-reloaded while trading.

The executable reads the current `.py` files on each start, including edits made within the same timestamp interval. It does not silently fall back to a hidden frozen copy if an application file is missing. Dashboard assets also come from `app\static`.

## Apply changes from the source project

For future maintenance, keep the editable source project outside the installed folder. After Codex changes/tests that source, close CryptoPilot and double-click **UPDATE_INSTALLED.bat** from that source project. It targets the default installation; a custom installation can be supplied as its first argument.

The equivalent command, requiring no separately installed Python, is:

```powershell
& "$env:LOCALAPPDATA\Programs\CryptoPilot\runtime\CryptoPilotRuntime.exe" --update-source 'C:\path\to\CryptoPilot-source'
```

The updater verifies the runtime dependency fingerprint, checks Python syntax and required assets, stages the new files and creates a unique backup before replacing the editable app. It refuses an active installation even when it uses a custom data folder. It does not connect to an exchange, read credentials, download updates or start the bot.

Direct installed edits are detected. If the app folder differs from its installation record, a normal source update refuses to overwrite it. After reviewing the changes, an intentional replacement can use **--replace-local-changes**; the previous folder is still backed up. Unexpected extra local files are preserved in that backup too.

Interrupted replacements retain a recovery record. The next launcher/update restores the previous source before continuing. If recovery fails, preserve the installation/backups and review the error; do not delete portfolio data to fix an installation problem. A source update is a locked, staged replacement with recovery, not a guarantee against every disk/power failure.

Ordinary Python/dashboard changes do not require reinstalling the runtime. Changes to Python itself, native libraries or the dependency lock require a rebuilt installer. New application modules can be added directly under `app`; the conservative source updater packages the fifteen core modules it lists plus dashboard assets. A source update cannot add a module that the installed updater does not list, so when a release adds modules (the hardening release adds six), upgrade with Setup; the in-app updater always uses Setup.

## In-app updates

The dashboard checks this project's [GitHub Releases](https://github.com/BuildLoom-Amepps1994/ATB/releases) when it opens and then every hour, or straight away with **Check for updates** in the footer. The footer always shows the result of the last check. Copies older than 1.5 (such as 1.4.0) check only when the page opens and reuse that answer for six hours, so on those click **Check for updates** to see a new release straight away. When a newer version is published, a banner offers **Install update**. In-app installs need the copy installed by Setup; a copy started from source with `START_WINDOWS.bat` only links to the installer. Stop the bot first. CryptoPilot then downloads the new Setup, checks its size and SHA-256 against the values GitHub published for that release (a mismatch deletes the file and nothing runs), starts Setup and closes itself. Setup waits for the app to exit, installs over the same folder with a backup of the old editable source, and reopens CryptoPilot in a new browser tab. Trading is never started by an update, and automatic stops are inactive until you start the bot again.

The checksum proves the file is the one published on the release page; it is not a code signature, so Windows may still show an unknown-publisher prompt. Set `CRYPTOPILOT_NO_UPDATE_CHECK=1` to turn the check off. If an update fails, Setup's log is in `%LOCALAPPDATA%\CryptoPilot\updates\setup.log`.

## Publish a release (maintainers)

The **Windows installer** GitHub Actions workflow builds Setup on a Windows runner, runs the test suite and both Windows smoke tests, and uploads the installer as a workflow artifact for every pull request. Releases are automatic: every push to `main` (a merged pull request) builds and tests the same way, then publishes `CryptoPilot-Setup-X.Y.Z.exe` plus its `.sha256` as GitHub Release `vX.Y.Z`, which installed copies then offer as an update. `X.Y` comes from `APP_VERSION` in `updater.py` and `Z` is the number of commits on `main`, so every merge is newer than the one before and nobody has to bump a version or push a tag. Change `APP_VERSION` (and `AppVersion` in `installer/CryptoPilot.iss`, which a test keeps equal) only to start a new series such as 1.6. Pull-request builds are stamped `X.Y.0`, below every release, so a copy installed from a test artifact is still offered the next real release. A release is published only when the whole Windows build and its smoke tests pass. An existing release is never replaced, and re-running an older build never publishes over a newer release.

## Upgrade and uninstall

Close CryptoPilot before running Setup again. Setup backs up the previous editable app before activating the new source. It refuses to stop a running bot for you and does not add startup tasks. Setup keeps the previous runtime aside until the new source is active. If the new source cannot be activated, for example because a Command Prompt or editor has its working folder inside `app`, Setup puts the previous runtime and source back, says why, and exits with code 9; close that window and try again. If an update was cut off while Setup was copying files, for example by a power cut, run Setup again; an in-app update leaves the downloaded `CryptoPilot-Setup-X.Y.Z.exe` in `%LOCALAPPDATA%\CryptoPilot\updates`. If it was cut off during the final source swap, the next start of CryptoPilot or the next run of Setup finishes it. The installer also rejects portfolio-data locations, drive roots, unrelated non-empty folders, symbolic links and junctions as installation targets.

Uninstall through Windows Apps or the Start menu. Runtime files and shortcuts are removed; **editable source/backups and portfolio/settings/OS credentials are preserved**. This prevents uninstalling from destroying local work or trading records. Remove those manually only if you intend to discard them, after reconciling any exchange holdings. Uninstalling does not liquidate positions.

## Build the installer from source

Install 64-bit Python **3.14** (the python.org installer or `py install 3.14` with the Python install manager; 3.12 or 3.13 also works; 3.15 is not supported yet) and [Inno Setup 6 or 7](https://jrsoftware.org/isdl.php). Double-click **BUILD_WINDOWS_INSTALLER.bat**. If necessary, set `INNO_SETUP_COMPILER` to `ISCC.exe`'s full path. The script creates an isolated build environment, builds the dependency runtime with `CryptoPilotEditable.spec`, assembles a new stage and compiles Setup into `dist\installer`.

The dependency runtime is built separately from the editable source. Existing staging folders are retained rather than silently overwritten. Build requirements are version-pinned; pins do not authenticate update authors or provide a vulnerability audit. The delivered Setup and runtime are unsigned, so Windows may display an unknown-publisher warning.

Automated tests use temporary installs/profiles, a disabled credential backend and no live exchange requests. See **INSTALLER_VALIDATION.md** in the source/release for the exact completed install/update checks. Software stops still require the application, PC and fresh quotes to remain available. No profitability is guaranteed.
