# CryptoPilot 1.3.0: completed improvements and validation

The installed application at **D:\CryptoPilot** was upgraded to **1.3.0** on 23 October 2026. Installed-file fingerprints matched the tested payload. Startup was verified with a separate temporary profile and disabled credential backend: the app was stopped, disconnected and in paper mode, the bid-valuation metadata and settings-health fields were present, and the updated dashboard assets loaded. The test process was then closed. The user's normal portfolio and credential store were not accessed for these checks.

## Concrete improvements

| Problem in 1.2 | Behavior in 1.3 |
| --- | --- |
| A stale last trade could hide a lower selling quote and delay a software stop. | Holdings, liquidation equity and protective checks use a validated bid; Buy preflight/sizing uses the ask. A usable bid can still support an exit if the ask is absent. New buys require both quotes; invalid/crossed/stale responses cannot silently use last trade. Unrealised P/L includes the same modeled exit costs as equity. |
| A tiny remainder beyond its stop could block all new assets or consume a full position slot. | Verified sub-minimum dust retains its exact cost/quantity and remains in exposure/risk calculations, but is excluded from tradable slot counts and pending protective exits. Fresh quotes and known exchange minimums are required. Other eligible coins can trade. Full holdings still block duplicate entries. Same-asset automated dust consolidation remains conservative; manual consolidation is available. |
| Invalid settings could silently restore looser defaults. | Settings maintain a validated last-good copy, preserve readable damaged-file evidence and latch a persistent review state. Last approved limits are recovered where available. Live activation/start and new live buys require review/save; deliberately confirmed exits and ongoing protective checks remain available in an already armed session. A new profile initializes normally in stopped paper mode. |
| A crash could commit a fill but leave its manual request pending. | Fill accounting, manual completion result and candle reentry markers commit in the same SQLite transaction. A crash after commit can replay the known result without another submit. A result-write failure rolls back the ledger; an accepted live intent remains unresolved and halts execution. Post-commit logging errors cannot overwrite a confirmed result as failed. |
| Recovery reports omitted incomplete manual requests. | Offline recovery now reports pending/failed/malformed request counts and safe identifiers, plus settings-review status. Raw request payloads, credential fingerprints, exchange order IDs and free-text errors are excluded. It never clears a halt or retries an order. |
| Reset paper state could be resurrected from retained legacy metadata. | Legacy metadata migration records completion transactionally and does not replay originals after a reset or on upgrade from an already migrated account. Original legacy rows remain as evidence. |
| Research and live entries used different indicator seeds and opening-gap rules. | Shared bounded indicator history uses 110 completed primary candles and 65 completed confirmation candles. Historical and current decisions agree on the same windows. A shared entry-price guard rejects opening quotes beyond the allowed signal/ATR deviation. This changes signal calculations for consistency; it is not parameter optimisation for profit. |
| A single full-window backtest could conceal weak coverage or cost sensitivity. | The dashboard adds a fixed 70/30 chronological earlier/holdout comparison and a full-period replay with doubled fees/slippage. Each segment starts flat, uses only earlier indicator warmup and measures returns/drawdown over its own boundaries. Short samples, low trade counts and unavailable trend coverage are identified. No parameters are fitted or selected. |
| Research evidence was hard to retain; polling rebuilt dropdown options. | Save research report downloads JSON with dates, modeled parameters, coverage, executions, assumptions and candle SHA-256 fingerprints. Raw candles are not archived by this export; hashes identify data but cannot retrieve it. Unchanged options retain their DOM nodes during polling, preserving active selection/navigation. A settings recovery banner leads directly to risk-limit review. |

## Completed checks

- Final complete Python suite: **334 passed**, 1 upstream Starlette/httpx deprecation warning, elapsed 37.10s. This includes **49 additional regression cases** beyond the 285-test prior release.
- **11 JavaScript tests passed**. Edge desktop/mobile browser checks covered research tables/export, report escaping, stable dropdowns, recovery warnings, live activation controls and the existing sequential-order/stale-confirmation flows; zero page errors.
- Crash tests injected failures both before ledger commit and immediately after commit, for paper/live and Buy/Sell paths. These used a fake exchange; known fills were not submitted twice and unaccounted accepted intents stayed blocked.
- Settings tests exercised invalid JSON, invalid numeric values, missing risk fields, missing/unreadable files, damaged recovery records, interrupted saves, explicit review and repeated restart/reset migration.
- Research tests checked bounded-window arithmetic against independently recomputed windows, live/historical confirmation agreement, large-gap rejection, future-holdout mutation independence, segment execution boundaries, canonical data fingerprints, cost stress, and zero trend-confirmation coverage.
- Native Windows runtime simulation passed **2 paper Buy/Sell round trips** and **2 simulated live round trips**, plus UUID replay and offline TLS checks. Fake credentials/backend injection existed only in a temporary test install.
- Final Windows Setup passed isolated install/start/edit/restart, refusal to overwrite a running installation, explicit source replacement/backup, source update without executable changes, Setup upgrade/data retention, and uninstall retention.
- Public BTC/USDT ticker reads received valid bid/ask fields from **MEXC and Kraken**, with no credentials or orders. MEXC supplied an exchange timestamp; Kraken did not, so the existing receipt-time freshness policy applies. Public success does not validate any private permission or funded execution path.
- Actual **D:\CryptoPilot** upgrade and isolated-profile startup passed; TLS certificate and hostname verification remained enabled. No normal-profile settings, holdings or keys were used in verification.

The initial repeat of the 1.2 suite had 283 passes and two Windows subprocess cold-start timeouts at 10 seconds. Those isolation/locking tests now allow 60 seconds while preserving their functional assertions. The final suite passes. This is test robustness under Windows startup/security scanning, not a claim of faster application launch.

## Use and maintenance

Open CryptoPilot normally. Paper remains the launch default, with the bot stopped. The installed application files stay editable under `D:\CryptoPilot\app`; close the app before editing and restart to load changes. No background updater or permanent remote access was added.

If a recovery banner appears, inspect every displayed risk limit and click **Save settings** to approve them. `settings.last-good.json` holds validated limits; `settings.recovery.json` records required review. Readable damaged originals are retained as `settings.invalid-*.json`. Do not remove recovery files to bypass an error; preserve the complete data folder and resolve the underlying problem. An unreadable original must become accessible before replacement.

For research, stop the bot, choose a pair/timeframe and run the backtest. Compare earlier/later results, coverage, sample sizes and doubled costs. **Save research report** saves the displayed result locally as JSON. These descriptive fixed-rule checks are not independent prospective validation; future data cannot alter earlier calculations, but known rules were not designed in a blind external study.

Manual orders remain available while stopped. **Start bot** is required for automatic software stops/trailing checks. The 5-second protective polling target, 10-second minimum entry scan and Rapid 1-minute/5-minute profile remain available. Network requests, rate limits, unavailable quotes, illiquidity and in-flight retries can delay checks/fills; this is not HFT or an execution-speed guarantee. Quoted bid/ask is not order-book depth and does not guarantee an executable amount or fill price, as the [CCXT manual](https://github.com/ccxt/ccxt/wiki/manual) explains.

No real keys were requested, no private account permissions or funded order fills were verified, and no real trades were placed. Live remains explicitly opt-in. Market/stop gaps and slippage can exceed modeled risk; uncertain live orders continue to require reconciliation. No profit is guaranteed.

Setup source backup: `D:\CryptoPilot\backups\setup-20261023135319`.

Pre-upgrade installation metadata backup: `D:\CryptoPilot\backups\runtime-repair-20261023T125314947648Z`. The dependency runtime is unchanged from the TLS-fixed 1.2 build; this release updates the editable application and installer metadata. The user's portfolio and OS credential store remain separate from the install folder.

## Build and files

The release provides `CryptoPilot-Setup-1.3.0.exe`, an editable source ZIP, this report, Windows instructions and a SHA-256 manifest. To rebuild, extract source, install 64-bit Python 3.12 and Inno Setup 6 or 7, then run `BUILD_WINDOWS_INSTALLER.bat`. Dependencies are pinned. The runtime and installer are unsigned; Windows may display an unknown-publisher warning. See `WINDOWS_INSTALL.md` in the source for full installation/update/recovery instructions.

Installer SHA-256: `13015783dd0fdceabed047074ff28395f96001853d221b46afd952face8c39e8`.
