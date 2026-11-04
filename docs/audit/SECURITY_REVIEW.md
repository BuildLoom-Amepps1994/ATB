# Security review

Scope: the desktop application as shipped (Windows installer, also runs
from source), its local API, credential handling, update channel, logs,
dependencies and the repository. Date: 2026-11-04. The audit ran in an
isolated container with no exchange access and no credentials; no live
system was probed. Findings from the audit's security lens are merged in
section 4.

## 1. Threat model

The operator runs one process on their own PC with exchange API keys that
can place spot orders (withdrawal rights are never needed and must stay
off at the exchange). The assets to protect are those keys, the ability to
place orders, and the ledger and settings the bot trades on. The
adversaries that matter: other software on the same PC (a browser tab, a
malicious page, another local user), a compromised update channel, a
dependency with a vulnerability, and the operator's own mistakes (secrets
in logs or in the repository).

## 2. Controls in place

| Area | Control | Where |
|---|---|---|
| Network exposure | the API binds to the loopback interface only and refuses any request whose client address is not loopback; the browser is opened on `127.0.0.1` | `app.py` loopback guard |
| Cross-site requests | every state-changing route needs the per-session `X-CryptoPilot-Token`, generated with `secrets.token_urlsafe(32)` at start, compared in constant time, and carried only by the served dashboard page; Origin/Host are validated; a Content-Security-Policy with a per-request nonce and `Cross-Origin-Resource-Policy: same-origin` are set | `app.py` middleware |
| Credentials | API key and secret live in the operating-system credential store (Windows Credential Manager, macOS Keychain, Secret Service on Linux) through `keyring`, scoped to the local machine; never in the SQLite file or settings | `credentials.py`; `storage.py` docstring "Exchange secrets never belong here" |
| Secrets in output | every audit line, event and log passes `storage.redact`, which replaces registered secret values and anything that looks like a key, signature, authorization, password or token | `storage.redact`, `observability._redacted` |
| Live trading | paper by default; live needs the exact phrase `ENABLE LIVE TRADING` and an eligibility acknowledgement; Trend and Micro refuse live; a kill switch and durable halts exist | `trader.set_mode`, `risk.py` |
| Update channel | releases are fetched from the project's GitHub Releases over HTTPS only (redirects to non-HTTPS refused); the installer's SHA-256 is checked against the digest GitHub records for the asset and the published `.sha256` file before Setup runs. **The installer is not code-signed**; the hash proves the file is the one GitHub published, not who published it | `updater.py` |
| Source updates | the frozen runtime and the source manifest are matched by hash; a failed or interrupted update keeps runtime and source consistent | `maintenance/update_installation.py`, `windows_runtime.py` |
| Dependencies | pinned lock files for runtime, development and build; `pip-audit` on all three on 2026-11-04: no known vulnerabilities; CCXT 4.5.91, FastAPI, pandas 3 | `requirements*-lock.txt` |
| Repository hygiene | git history scanned for secrets: only test fixtures of the source-archive guard; the archive builder refuses files that look like secrets | `scripts/make_source_archive.py`, `tests/` |
| Static analysis | `bandit`: 0 high, 16 medium, all reviewed: SQL strings assembled from constant table and column names (S608), closures called inside the loop that defines them (B023), the updater's URL opener with HTTPS enforced (S310), `exec` of a generated entry rule in the research harness only (B102, not shipped) | baseline notes |
| GitHub code scanning | CodeQL default setup is enabled on the repository; three alerts on the Micro API were reviewed by the owner and kept (dismissed as won't fix) | repository Security tab |

## 3. Gaps and recommendations

| # | Gap | Risk | Recommendation |
|---|---|---|---|
| S1 | The installer is not code-signed; Windows SmartScreen warns on every install and a compromised GitHub account could publish a malicious release that passes the hash check | medium | an Authenticode certificate for the release workflow; until then, the operator should compare the release page's hash out of band before the first install of a new version |
| S2 | No dependency-update automation (`dependabot.yml`) and no `pip-audit` step in CI; the audit was manual | low | add a weekly `pip-audit` job on the three lock files and Dependabot for pip and GitHub Actions |
| S3 | CI runs only the Windows installer workflow; lint, type and security scans are not enforced on pull requests | low | a Linux job with `ruff`, `bandit -ll`, `pip-audit` and the test suite; see `IMPROVEMENT_ROADMAP.md` |
| S4 | Any local process can read the dashboard page and so obtain the session token (same-user access); this is inherent to a loopback web UI without a login | accepted | document that the PC must be single-user and locked; a login would not stop a same-user process |
| S5 | Exchange API keys should be created without withdrawal rights and with an IP allow-list where the exchange offers it; the app cannot verify this | operational | add to the runbook's live checklist (done in `PRODUCTION_READINESS.md`) |
| S6 | The SQLite file holds the full trade history and settings unencrypted; it holds no secrets | low | rely on disk encryption (BitLocker); no change |

## 4. Security lens findings

The security lens read `app.py`, `credentials.py`, `storage.py`,
`updater.py`, `windows_runtime.py`, `launcher.py`, the maintenance and
build scripts, the installer definition, the release workflow, both
PyInstaller specs, the six requirement files, `observability.py` and the
engine's credential, mode, config, submit-error, manual-trade and snapshot
paths, and grepped the repository for unsafe deserialisation, `eval`/`exec`
and `shell=True` (none found outside the research harness's compilation of
the app's own strategy source). Its seven findings, with the status on this
branch:

| # | Severity | Finding | Status |
|---|---|---|---|
| SL-1 | high (design risk) | The in-app update trusts GitHub alone: the SHA-256 it checks comes from the same release that serves the installer, the installer is not code-signed, and every merge to `main` publishes a release without a human approval step. Whoever can publish a release (a compromised account or token, a compromised third-party Action) reaches every installed copy within the six-hour check interval, and the installed app can read the exchange key from the vault | open; roadmap R-10 (Authenticode signing, a pinned signing key checked by the updater, a manual release approval). Until then: install only after comparing the release hash out of band, keep the API key without withdrawal rights (S1, S5) |
| SL-2 | medium (design risk) | The release pipeline resolves every third-party input by a mutable name: Actions by major tag, Inno Setup through `choco install` without a version or hash, Python dependencies from a lock file without hashes | open; roadmap R-10 (pin Actions to commit SHAs, pin and verify the Inno Setup download, `--require-hashes` lock files, Dependabot for Actions and pip) |
| SL-3 | medium (design risk) | No local authentication: the action token is served to any loopback client, so a second local account or any local process can read the state and place orders within the configured caps | accepted as S4 with a sharper statement: the token is a CSRF defence, not authentication; the PC must be single-user. Roadmap R-11 considers binding the token to the operating-system user (a file readable only by that user) |
| SL-4 | low (confirmed, deferred) | The in-app update's idle check is read without a lock, ignores an active chart, backtest or manual scan, and is not latched, so a Start can slip in between the post-download check and the forced exit (hardening finding 23) | open; roadmap R-12 (set `control_busy` for the install window and refuse Start) |
| SL-5 | low (design risk) | A value pasted as an API key is added to the redaction set before the exchange validates it, so a paste error redacts that string from every later audit line and event in the session | accepted: registering before validation is what keeps a failed connect's trail free of the real secret; the cost is a readable trail after a paste error, until restart |
| SL-6 | low (confirmed) | An integer too large for a float in a settings field raised `OverflowError`, which bypassed the 400 mapping and logged a spurious service failure | fixed on this branch (`storage.validate_settings` treats it as out of bounds; `tests/test_persistence_fixes.py::test_an_oversized_integer_setting_is_a_validation_error`) |
| SL-7 | low (design risk) | A credential pair saved by a build before hardening finding 38 keeps Windows roaming persistence until the operator saves it again | open by choice (HARDENING_REVIEW row 51); roadmap R-11 (re-save once on load with an audit line) |

None of the seven is reachable from outside the PC: the API refuses
non-loopback peers, mismatched Host and Origin headers and cross-site
fetches, and no inbound port is opened. The two medium findings and the
high one are supply-chain and local-trust questions that the brief's
"enterprise grade" bar requires answering before live capital; they are
priced in `IMPROVEMENT_ROADMAP.md` and listed as go/no-go items in
`PRODUCTION_READINESS.md`.
