# Micro-trading mode: validation

What was run for this candidate, on which source, with which result, and what
was not run. Every count below is from a log of an actual run. Focused runs
overlap the full suite and are never added to it.

## Source and environment

| Item | Value |
|---|---|
| Branch | `claude/project-thread-vqle7r`, from main `b1e0217` |
| Code commit tested | `82913ca` (the commits after it change documentation and recorded results only; `git diff 82913ca HEAD -- '*.py' '*.js' '*.html' '*.cjs' '*.yml' '*.iss' '*.txt'` is empty) |
| Host | Linux cloud container, 4 CPUs, Python 3.13.17, Node 22.22.0 |
| Dependencies | `requirements-lock.txt` as on main (ccxt 4.5.90, pandas 3.1.0) |
| Isolation | `tests/conftest.py` sets a new temporary `CRYPTOPILOT_DATA_DIR` and `LOCALAPPDATA`, the null keyring backend and no update checks before any application module is imported, removes every proxy variable, refuses every connection, datagram, bind and name lookup that is not local (loopback, `localhost` or the wildcard address), answers local lookups itself so none reaches a DNS server, and blocks uvloop and winloop, in child Python processes too (`scripts/offline_net.py`). Every pytest and benchmark command below also exported a new `CRYPTOPILOT_DATA_DIR` under `/tmp` and `PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring` first; the soak sets both itself for its `--data-dir` folder |
| Exchange access | None. MEXC and Kraken are unreachable from this host; every exchange interaction is a fixture |
| Credentials | None requested, read or used. No settings file, portfolio, order database or log of a real installation was opened |

## Final runs

```
export CRYPTOPILOT_DATA_DIR=$(mktemp -d /tmp/claude-0/cp-XXXX) PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring
python -m pytest tests -p no:cacheprovider --durations=15 -rfEs
node --test tests/test_dashboard.js
```

| Suite | Passed | Failed | Skipped | Errors | Duration |
|---|---|---|---|---|---|
| `pytest tests` (Python 3.13.17), on `82913ca` (a clean `git worktree` of it) | 1542 | 0 | 0 | 0 | 389.9 s, alongside the research rerun |
| `node --test tests/test_dashboard.js` (Node 22.22.0), on `82913ca` | 19 | 0 | 0 | 0 | 0.2 s |

The two pytest warnings are aiohttp's `enable_cleanup_closed` deprecation
notice from the websocket tests, unrelated to this change.

**Intermittents.** None seen in the final runs or in the focused runs before
them. No test was retried to get a pass, and no timeout was raised. The cycle
tests run on a fixture clock (`FixtureClock`) that adds each tick's real
duration to its fixed step, so a slow host can still change them: review
round 3 found one such test and it was fixed, and every cycle test passed with
each tick slowed by 15 ms (see *Focused runs*).

**Changed expectations.** Three pre-existing test files changed. In
`tests/test_recovery.py` the expected database schema version moved from 3 to
4, for the new micro tables. In `tests/test_launcher.py`,
`test_browser_readiness_uses_loopback_http_without_tls_or_proxies` now
removes `NO_PROXY` and `no_proxy` before it runs, because the suite's
`NO_PROXY=*` would otherwise hide a launcher that used a proxy; its
assertions are unchanged. `tests/test_dashboard.js` gained three micro panel
tests and imports two more helpers; its existing tests are unchanged.
No pre-existing test was deleted, skipped or marked.

## Focused runs during development

These overlap the final full suite and are listed only to show what was run
after each change; they are not added to the totals above.

| When | Command scope | Result |
|---|---|---|
| After the data-review fixes and the idle-decision fix | `tests/test_micro_cycles.py tests/test_micro_strategy.py tests/test_micro_research.py` | 223 passed |
| After the catalogue deferral | catalogue tests, `tests/test_market_data.py`, `tests/test_review_fixes.py` | 2 passed; 52 passed |
| Full suite at `fa45c70` (before the last fixes) | `tests` | 1307 passed, 0 failed, 0 skipped, 288.6 s |
| Dashboard at `fa45c70` | `node --test tests/test_dashboard.js` | 19 passed |
| Full suite at `c043668` (run alongside the first wall-clock smoke) | `tests` | 1312 passed, 0 failed, 0 skipped, 257.0 s; dashboard 19 passed |
| First version of the idle-cadence fix | `tests` | 1313 passed, 0 failed, 0 skipped, 263.6 s |
| After the second review round of that fix | `tests` | 1317 passed, 0 failed, 0 skipped, 293.4 s |
| Mutation check of the five new idle-cadence tests | the five tests and three neighbours, against 12 variants of `micro.py` | Every variant that removes one part of the fix fails at least one test, except two parts that cover each other (the reset clears and the "no decision yet" rule; removing both fails the reset test). `c043668` fails three of the five (one of them only through its start-up read) |
| Slow-host check of the same five tests | each tick slowed by 0, 3, 6, 8, 10, 15, 20 and 40 ms of real time | All passed at every setting |
| Slow-host check of every cycle test on `9535a55` | `tests/test_micro_cycles.py`, each tick slowed by 15 ms of real time | 58 passed, 292.2 s |
| New test for the 25 USDT settings bound | `tests/test_micro_refusal.py` | 15 passed; the new test fails if the bound is raised to 50 or if `MicroManager.budget_cap` stops taking the minimum with the 25 USDT constant |
| After the final review round's fixes, before their commit | `tests` on the working tree of that moment, five tests short of `7066d6f` (which has 1374) | 1368 passed and 1 failed: the archive self-screen test, which reads HEAD and passed (29 archive tests) once the fixes were committed as `7066d6f`; dashboard 19 passed. No full Linux run of `7066d6f` or `20c7067` was made; the full suite of each ran on Windows (below) |
| Mutation check of the final round's main fixes | the new tests, against the network guard without the `NO_PROXY` setting, without the child-process guard, without the proxy clearing in `conftest.py`, and the research harness with the high fill, the MEXC-only rules and the verdict on closed cycles only | Each variant fails at least one new test |
| Windows CI on `7066d6f` | the full suite on `windows-latest`, Python 3.14.9 | 1365 passed, 2 failed, 7 skipped, 658.8 s. Both failures were archive tests whose fixture wrote its file with `write_text`, which gives CRLF on Windows, so the committed blob was not the bytes the test expected; the fixture now writes bytes (`3c0c45d`), and the Windows test step of `3c0c45d` passed |
| After the second review round's fixes (`20c7067`) | `tests/test_micro_*.py`, `tests/test_money_and_venue_rules.py`, `tests/test_offline_guard.py`, `tests/test_source_archive.py` | 705 passed, 219.3 s |
| Mutation check of the second round's fixes | each new test against its fix reverted: the anchored name screen, `create_system` left to `zipfile`, `getnameinfo` unguarded, the search only when the high fires, and the unbounded rounding loops | Each fails: three name cases, the cross-platform hash, the lookup test, the band test (0.0249 instead of the band's bottom) and both rounding cases (still looping after 10 s) |
| Windows CI on `20c7067` | the full suite on `windows-latest`, Python 3.14.9, then the installer build and its smoke tests | 1374 passed, 0 failed, 7 skipped, 665.2 s; the installer built and both smoke tests passed |
| After the third review round's fixes, before their commit | `tests/test_money_and_venue_rules.py` and every `tests/test_micro_*.py`; then `tests/test_offline_guard.py tests/test_micro_soak.py`; then the launcher, uvicorn, soak and archive tests | 728 passed, 316.1 s; 30 passed; 168 passed |
| Mutation check of the third round's fixes | each new test against its fix reverted: the old local-name list, `localhost` passed to the resolver, reverse lookups passed to the resolver, no uvloop block, no check for an already imported uvloop, `NI_NAMEREQD` kept; the hash without the strategy constants; the old step rounding; the soak without its guard block | Each fails at least one new test (the old rounding fails five cases; the soak variant fails both soak tests that read the flag) |
| DNS probe of the guard, before and after the third and fourth rounds | each local lookup form under `strace` in an empty network namespace, counting connections to port 53 | Before: 6 for each of `localhost` over IPv6, `localhost.localdomain`, `ip6-localhost` and the reverse lookups of `127.0.0.2`, `::1` and `0.0.0.0`. After the third round (`cb7d082`): 0 for every lookup form. With `fb1e544`'s guard and a hosts file that lacks `localhost`: also 0 for the socket methods and `bind` |
| Windows CI on `cb7d082`, `fb1e544` and `ec3c59f` | the full suite on `windows-latest`, Python 3.14.9, then the installer build and its smoke tests | All three succeeded |
| After the fourth and fifth reviews' fixes (`fb1e544`, `ec3c59f`) | `tests/test_offline_guard.py` | 24, then 30 passed; each of the 11 variants that reverts one part of those fixes fails at least one test |
| Full suite at `ec3c59f`, before the account-fee parser | `tests`; `node --test tests/test_dashboard.js` | 1468 passed, 0 failed, 0 skipped, 387.8 s; dashboard 19 passed |
| Account-fee parser, after its first and second review rounds | `tests/test_micro_commission.py tests/test_micro_economics.py tests/test_money_and_venue_rules.py`; then `tests` on the working tree | 286, then 301 passed; 1525, then 1540 passed, 0 failed, 0 skipped |
| Mutation check of the account-fee parser and its review fixes | 32 variants, each removing or weakening one check: the parser's code check, its Decimal number parsing (`parse_float=Decimal`), and its rate, bound, digit, timestamp, symbol, time, length, repeated-key and exponent checks; `cost_model`'s account handling; `money.parse`'s float repr and exact bound; and the static test against a `tradeFee` request, a raw URL, a network import in `venue_rules.py` and an `account` keyword | Each fails at least one test in `tests/test_micro_commission.py` or `tests/test_money_and_venue_rules.py`. One variant, a second catch that the `money.parse` fix had made unreachable for every input then tested, survived until a test with a hostile number type was added |
| Full suite at `6aeeb01` (a clean `git worktree` of it) | `tests`; `node --test tests/test_dashboard.js` | 1540 passed, 0 failed, 0 skipped, 395.5 s, alongside the research rerun; dashboard 19 passed |
| Windows CI on `6aeeb01` | the full suite on `windows-latest`, Python 3.14.9 | 1530 passed, 1 failed, 8 skipped and 2 errors, 823.4 s, all in the new `tests/test_micro_commission.py`: one parametrized case's id was its 200,000-character argument, which pytest puts in `PYTEST_CURRENT_TEST` and Windows refuses above 32767 characters (an error at setup and at teardown), and the static no-caller test walked the runner's build venv inside the checkout (`.build-venv`) and stopped at CCXT's `ccxt/abstract/kucoin.py`, whose KuCoin endpoint name `privateGetTradeFees` contains the forbidden spelling `gettradefee` (the message of `82913ca` names `fetch_trading_fee` instead, in error). Both fixed in `82913ca`, each with a test that fails without its fix (the longest collected id is now 490 characters) |
| Windows CI on `82913ca` | the full suite on `windows-latest`, Python 3.14.9, then the installer build and its smoke tests | 1534 passed, 0 failed, 8 skipped, 698.2 s; the installer built and both smoke tests passed. 1534 plus 8 is the Linux count of 1542. Eight is the number of tests that skip on Windows: the seven cases of the pre-existing `tests/test_updater.py::test_publish_job_never_replaces_or_outranks_a_release` (marked to skip on win32) and this branch's netlink test in `tests/test_offline_guard.py` (no `AF_NETLINK` on Windows); the log, from `pytest -q`, does not name them |
| Check of the fourth wall-clock run's STALE_DATA samples | `scripts/micro_soak.py --minutes 5 --fixture --sample-seconds 1`, side by side on `fb52624` (exported with `git archive`) and `ec3c59f` | Both completed with no fault. Over 5 bar closes each, every pair not holding a cycle showed STALE_DATA starting 0 to 2 s after every close and lasting 5 to 7 s on `fb52624` and 5 to 6 s on `ec3c59f` (13 spells in each run): the documented wait for the new bar's candles |

## Reviews

* **Module reviews**, one independent reviewer per new module before
  integration: the paper venue (fixes in `8d7c77b`, `4798b79`, `0fc0a9f`),
  the economics (`d1e2f76`), the order book (`ea17eb2`) and the strategy
  (`483d204`). Each fix came with a regression test in the module's test file.
* **Integration review** of the engine wiring, by four reviewers with
  separate lenses (accounting, concurrency, boundaries, data), each finding
  then checked by a separate verifier that re-ran the reviewer's reproduction
  against the current tree. Every finding the verifiers confirmed was fixed
  in `e952857`, with the regression tests in `tests/test_micro_cycles.py`
  listed under items 14 to 25 below. The last open one, a slow catalogue
  refresh holding the worker for about 55 s, was fixed after the review
  (deferred while a cycle is held, then one attempt) and is register item 12
  for its 18 s remainder. One verifier noted a small fail-closed effect it
  could not trace (register item 15).
* **Found by the first wall-clock smoke** (on `c043668`): a pair with no
  possible setup recorded STALE_DATA for 15 to 23 s after bar closes, in 4 of
  40 samples, because its new-bar decision was made without a book. The fix
  (`9535a55`) went through three more review rounds, each with independent
  verifiers. Round 1 confirmed three issues in the first version: a forced
  entry read could delay a residual's mark for 30 s, which could let the risk
  gate refuse entries; `reset_memory` kept stale decided inputs; and the new
  test's timing slack. Round 2 confirmed one: the residual's read moved the
  shared schedule and slowed a plausible pair's 3 s reads. Round 3 compared
  read rates, book ages, exit cadence and decision codes against `c043668`
  and found no regression, and confirmed one test that depended on host
  speed. All were fixed before `9535a55` with the five regression tests
  listed under items 21 and 23.
* **Found while measuring and documenting**, after the reviews: the legacy
  monitor's extra statement (`d51ca7f`), the soak summary hashing the source
  at the end instead of the start (`c4587f8`), a test that waited 5 s in
  teardown (`6a99295`), and a research fixture whose price tick decided its
  result (`c043668`).
* **Documentation fact-check** of every `docs/ATB_MICRO_*.md` file, the
  micro parts of `README.md` and `CHANGELOG.md`, and the pull request text:
  five checkers compared each statement with the code, the tests and the
  recorded results, and a second agent tried to refute each finding. 43
  findings survived, all in the documents and none in the code. The two
  serious ones were in `docs/ATB_MICRO_ECONOMICS.md`: it said the paper
  venue re-checks the 25 USDT cap (the risk gate and `account_entry` do; the
  venue does not) and that every number on the page is pinned by a test
  (only the headline ratios are). The others were wrong numbers, ranges,
  test counts and descriptions. All were corrected, and the one untested part
  of a PASS gate, the settings bound on the micro budget, got a test.
* **Final review of the files that had none of their own** (`money.py`,
  `venue_rules.py`, `micro_policy.py`, `tests/conftest.py`, the soak, archive
  and benchmark scripts and the research harness): three reviewers, each
  finding then checked by a separate verifier. No path past the 25 USDT cap
  or the PAPER-only refusal was found. 21 findings were confirmed and all were
  fixed with tests:
  * Isolation: a proxy on loopback (this host has one) carried urllib and
    requests to any remote host past the socket guard; name lookups through
    `gethostbyname`, UDP datagrams, Windows' asyncio proactor connects and
    child processes were not covered. The guard is now
    `scripts/offline_net.py`: proxy variables are removed and `NO_PROXY` is
    `*`, every one of those paths is refused, and child Python processes load
    it through `tests/offline_child/sitecustomize.py`
    (`tests/test_offline_guard.py`).
  * Source archive: it refused this repository itself (its own test held a
    literal key block), so the earlier PASS for that gate was wrong. It also
    followed committed symbolic links, shipped skip-worktree edits, gave a
    different hash for a CRLF checkout, missed several secret forms, and its
    tests used the developer's global git configuration. It now reads the
    commit's own git objects, refuses links and submodules, stores entries
    uncompressed so the hash does not depend on the zlib build, screens more
    names and contents (also UTF-16), and its tests run git without global
    or system configuration (`tests/test_source_archive.py`).
  * Soak harness: the source identity left out the harness itself and said
    "clean" when git could not tell; a profile named by an inherited
    `CRYPTOPILOT_DATA_DIR` was not protected; `--sample-seconds` accepted 0,
    negative and NaN; fixture mode had no network guard
    (`tests/test_micro_soak.py`).
  * Research harness: a profit exit was filled at the bar's high (now at the
    lowest price in the bar at which it fires); the doubled and severe
    stresses did not scale a public taker fee above the configured rate;
    real-data books used a 0.01 tick whatever the market's, which blocked
    sub-dollar pairs and crashed on sub-cent ones; rules were always read as
    MEXC's, so Kraken data never traded; splits too short for their embargo
    were indexed anyway; a holding still open at the end of a split was left
    out of the verdict; the result recorded neither the book model, the
    decision parameters nor the code; and a second fixture clock did not
    replace the first (`tests/test_micro_research.py`). The protocol became
    `micro-research-v2` and the fixture results were re-run (*Research run*).
  * Venue rules: an unrecognised MEXC side rule allowed both sides; it now
    allows sells only, so a holding can close but nothing opens.
  The verifiers judged five more findings unreachable from any real caller.
  Two were hardened anyway: step rounding can no longer cross its input, and
  market rules stamped in the future or read with a bad clock are stale
  (`tests/test_money_and_venue_rules.py`). Not changed: the `DECIMAL_PLACES`
  precision path (neither venue uses it), `money.parse` of a NumPy float (no
  caller passed one then; round 1 of the account-fee parser's review found
  the same crash through that parser, and `money.parse` itself was then
  fixed in `6aeeb01`: it reads a float through `float.__repr__`) and the
  benchmark's request count for the ticker fallback (the benchmark never
  takes that path).
* **Second review of those fixes**: two reviewers (the network guard, archive
  and soak; the research harness, money and venue rules), each finding then
  checked by a separate verifier that reproduced it. Five were confirmed and
  fixed with tests, each of which fails when its fix is reverted:
  * The archive's name screen had become narrower than before: anchoring the
    names let `prod.env` and `app_settings.json` ship. The anchors are gone.
  * The archive hash differed between Windows and Linux, because `zipfile`
    records the creating system. It is now fixed at 3, and a test builds
    with the platform set to Windows and compares the hashes.
  * `socket.getnameinfo` (a reverse DNS query) passed the network guard. It
    is refused for a remote address unless only the numeric form is asked.
  * In the research harness, a bar whose high sat in the hold gap between the
    protection band and the target, and whose close fell into the band, was
    sold at the close, although the band was crossed lower on the way up. The
    whole range is now searched whenever the high is in profit, and the fill
    is the lowest price where a profit exit fires. The test's close-in-band
    case booked 0.0249 USDT before the fix and the band's bottom after it.
  * `money.q_down` and `money.q_up` looped forever on a 35-digit value with a
    step of 3e-30, where adding the step no longer moves a 34-digit number.
    They now make at most two corrections and then refuse.
  The verifiers confirmed every other part of the fixes, including the
  Windows asyncio signatures, the proxy handling of urllib, requests and
  httpx, and the linear time of the secret patterns on 400 KB inputs.
* **Third review**: two code reviewers (the network guard, archive and
  soak; the research harness and money) and two fact-checkers (the research
  document; the validation, performance and gates documents and the
  changelog), each finding then checked by a separate verifier that
  reproduced it. Fixed in `cb7d082`, each with a test that fails when its fix
  is reverted:
  * The network guard let local lookups reach the system resolver, which
    asks a DNS server for any name or address the hosts file lacks
    (`localhost` over IPv6, `::1`, `127.0.0.2`, `0.0.0.0`, `ip6-localhost`,
    `localhost.localdomain`). The guard now answers `localhost` and loopback
    addresses itself and refuses the other names.
  * uvloop, which ccxt installs on Linux and uvicorn's default loop picks,
    resolves and connects in C, outside the guard (nothing in the suite ran
    a uvloop loop). The guard now blocks uvloop and winloop from import.
  * The fixture soak's guard had no test of its own: its summary flag was
    read from the `--fixture` option. It now reads the guard, and a test runs
    the soak without the suite's child-process guard.
  * With a step finer than the value's 34-digit precision, step rounding
    could return a number that was not a multiple of the step (the second
    round had stopped only the loop). The multiple is now formed exactly or
    refused.
  * The protocol hash left out `micro_strategy`'s own constants (the stop
    multiple, the protection shares, the regime thresholds), and its fill
    text said "never the high", which is wrong when an exit first fires at
    the high. Protocol version 3 widens the hash and rewords the fill; the
    fixture run was repeated with identical figures (*Research run*).
  The fact-checks corrected these statements: the *Final runs* table still
  showed the `9535a55` run while naming `20c7067` as the code tested, and no
  full Linux run had been made of `7066d6f` or `20c7067` (the full suite now
  runs on the final code, *Final runs*); the
  pre-commit run of the first round was five tests short of its commit;
  version 2's hash changed under one label without saying so; version 1's
  tick and venue handling; the per-cycle values at 10 and 15 USDT; the marked
  figures of the identical validation and holdout windows; the benchmark
  clock-check bullet, which left out the cycle row; register item 17's file
  list; the changelog's account of version 1's hold-gap fill; and the
  archive's cross-platform check. Not changed, because the verifiers found
  no defect: the manifest's line endings on Windows (nothing needs identical
  bytes, and `sha256sum -c` accepts CRLF), the always-enter figures (closed
  net compared with closed net) and the Python version of the Windows runs
  (3.14.9, read again from the job log).
* **Fourth review**, of `cb7d082`: two code reviewers and their verifiers.
  The money and research changes had no defect: 2.04 million value and step
  pairs of the kinds the callers pass gave no refusal and no mismatch
  against an exact reference, and the one finding there, that the protocol
  hash leaves out non-numeric constants such as the exit-trigger order, was
  rejected (the hash claims only the numeric ones; the code identity covers
  the rest). Four gaps in the network guard were confirmed,
  all latent (no code in the repository takes these paths), and fixed in
  `fb1e544`, each with a test that fails when its fix is reverted: `bind`
  resolved a host name in C and was not guarded; `connect`, `connect_ex`,
  `sendto` and `sendmsg` passed the name `localhost` to the C resolver;
  `gethostbyaddr` answered a bytes host with its repr and made up answers for
  bracketed or zoned spellings; and `active()` did not check that uvloop was
  still blocked.
* **Fifth review**, of `fb1e544`'s guard changes: one reviewer and its
  verifier. Everything the suite does locally still worked under the guard
  (socket pairs, ancillary data, Unix, netlink and packet sockets, TLS
  sockets, `multiprocessing` listeners, `socketserver`, `http.server`,
  asyncio servers and datagrams, uvicorn reached by aiohttp, requests and
  urllib), and a logging DNS stub saw no query from any local form. Two low
  findings were confirmed and fixed in `ec3c59f`: the socket methods passed
  a bracketed or zoned loopback spelling to C, which resolves it (with a
  hosts entry for such a name, a packet went to that entry's address; such a
  spelling now gets no answer); and five parts of `fb1e544` had no test that
  failed without them (each now has one). These two fixes have had no review
  of their own (register item 17).
* **Final fact-check** of the documents, the pull request text and the
  report: four checkers, each finding then checked by a separate verifier.
  20 findings survived and were corrected, 6 were rejected. The serious one
  was in the report: it said the parser for MEXC's symbol-commission
  response was implemented and tested on supplied responses, as the brief
  asks, when no such code existed. It was then written (next bullet). The
  others were wrong or out-of-date statements: the changelog's bind wording
  (the wildcard address is allowed), the DNS-probe row, the third review's
  account of when the full Linux suite last ran, the count of changed
  pre-existing test files, two places that named a different final commit,
  the cycle row of the benchmark clock check, register item 17, the protocol
  row's fill wording, and in the report and pull request text a smoke-run
  summary, the 25 USDT gross and fee figures for ADA and XRP, the cause given
  for the earlier +0.3987 (the hold-gap close fill, not the high fill), a
  collection command asking for 200 book snapshots (capped at 50, and the
  harness never reads depth), a real-data command without `--out`, an
  overclaim about revert checks, the expected Windows skips and two Windows
  results not yet read.
* **Account-fee parser** (`venue_rules.parse_mexc_symbol_commission` and
  the `account` argument of `micro_economics.cost_model`, in `6aeeb01`):
  three review rounds, each finding then checked by a separate verifier that
  reproduced it. Every confirmed finding was fixed with a test before the
  commit.
  * Round 1, two reviewers (the parser; its integration with the cost model
    and the rest of the code), 7 confirmed: a JSON exponent too large for
    Decimal escaped as `InvalidOperation`; NumPy's `float64` crashed
    `money.parse` through its repr; `received_at` and `age_seconds` raised on
    a huge integer or a non-number (two findings); JSON that repeats a key was read with the last copy, so a
    refusal code could be overridden; and the test that nothing calls the
    endpoint missed CCXT's `fetch_trading_fee` and
    `spot_private_get_tradefee` spellings (two findings). Rejected: a
    hand-built `AccountCommission` skipping the bounds, and the symbol's
    format: a caller that follows the docstring ("passes the one it asked
    for") may hold MEXC's native market id `ETHUSDT`, which `cost_model`
    refuses because it wants the unified symbol `ETH/USDT` (this fails
    closed).
  * Round 2, three reviewers (fail-closed parsing; the cost model and
    money; the tests), 5 confirmed: a long JSON number became a Decimal whose
    stored form could not be read back, where the same digits as a string
    were refused; the 1 % bound used `abs()`, which rounds to the caller's
    decimal context (with a precision of 1, a 1.4 % taker rate passed); a
    5000-digit integer code raised a plain `ValueError` while being
    formatted; the static test missed CCXT's generic `request('tradeFee',
    ...)` and a raw URL; and no test made the public rate the binding term
    with an account rate passed. `money.parse` had the same `abs()` rounding
    against its magnitude bound and was fixed too. Five findings were
    rejected: no upper bound on the venue timestamp and `functools.partial`
    call forms (not changed), and an infinite age from readings of minus and
    plus 1e308 and two missing test cases, negative timestamps and infinite
    times (all three added anyway).
  * Round 3, two reviewers (the round 2 parser fixes; the `money.parse`
    changes and every caller of `money.parse`): no finding.
* **Fact-check of the final documentation changes** (everything above about
  the parser, the research rerun and the final runs): two checkers, each
  finding then checked by a separate verifier. 16 findings survived, none
  was rejected, and all were corrected in these documents: the file the
  Windows no-caller failure stopped at (`ccxt/abstract/kucoin.py`, not
  `fetch_trading_fee`), what the second rejected finding of parser round 1
  was about, a rejection listed under the third review that came from the
  fourth, an unclear "sixth review", the list of the final fact-check's
  corrections, the mutation variants, register item 17 and the review row
  (which left out `82913ca`), three statements in the performance document
  about what changed after the benchmarked commits, the STALE_DATA timing
  after a bar close (about 6 to 9 s until an entry decision, and about 7 s
  for the fourth run's samples), and the changelog, contract and
  architecture wording that said the parser refuses anything but the
  documented shape (it does not check `msg` or extra keys, and accepts rates
  as strings).
* **Fact-check of the final report and pull request description** (written
  from these documents): three checkers, each finding then checked by a
  separate verifier. 18 findings survived and 3 were rejected. Two reached
  `docs/ATB_MICRO_*.md` and are corrected here: two wall-clock runs were left out
  (now under *Wall-clock smoke*, *Two runs not counted above*), and the
  performance document's heading called `483d204` "before the reviews",
  though it already has the module-review fixes. One, the report's
  statement that the source archive is attached to the thread, needed no
  change in wording: it holds once the message that publishes the report
  attaches the archive, its manifest and `SHA256SUMS`. The rest were corrected in
  the report and the description: an incomplete list of open gates; "Windows-only"
  for the no-caller failure, whose cause, a virtual environment inside the
  checkout, would fail on any system; "no test was skipped" (no pre-existing
  test was); the names of the two smoke tests; the third review round's 16
  findings (10 were document statements); the research result file's own
  SHA-256 beside the protocol's; that no ETH research cycle reaches the
  target (all close by profit protection); version 2's fill wording; the
  clean-machine gate's edit step; which run built the hashed installer; the
  two engine-wide changes legacy paths also get (a 300 s retry after a failed
  catalogue refresh, and a submit-time check that the mode has not changed);
  and the top-of-book size limit on safety exits without a depth book. Those
  last two also stood in `CHANGELOG.md` and `README.md`, as the checks below
  found; they are corrected there.
* **Check of those corrections** (two checkers on the changed text, each
  finding then checked by a separate verifier): 6 confirmed, 1 rejected. One
  showed that the legacy-path and safety-exit wording corrected in the
  description also stood in `CHANGELOG.md` (*Unchanged*: legacy strategies
  and live mode "as before") and `README.md` (safety exits that need no
  buy-side depth); both are corrected there. Three concerned the archive
  statement: it is not true until the files are attached, the description
  called all 18 findings corrected, and the entry above, as first committed
  in `014a647`, left it out of its list (now stated there). The other two were in
  the report: it said every review's findings were checked by a separate
  verifier, but the module reviews had none (the agent fixing each module's
  findings first checked them, reproducing 29 and confirming 3 missing
  tests, and fixed all 32), and it said no
  research cycle reaches the target, which holds for ETH (the strategy and
  the always-enter baseline) but not for the always-enter baseline on ADA
  and BTC, which closed some cycles by take profit.

## Brief test matrix (items 1 to 30)

Each row names the tests that exercise the item. A test can serve several
rows. "Fixture" means a synthetic market or a scripted paper venue, never a
real exchange.

| # | Item | Tests |
|---|---|---|
| 1 | 25.00 USDT all-in cap with quote fees and rounding | `test_micro_economics.py::test_all_in_cap_holds_with_quote_fees_and_rounding_across_prices_steps_and_fees`, `::test_entry_accounting_refuses_a_debit_above_the_reservation`; `test_micro_strategy.py::test_an_eligible_sizing_must_keep_the_all_in_invariant_under_the_micro_cap`; `test_micro_refusal.py::test_a_micro_entry_needs_an_eligible_sizing_that_matches_the_debit` |
| 2 | Base-asset fees reduce sellable ownership | `test_micro_economics.py::test_base_fee_reduces_sellable_ownership_without_oversell_and_conserves_basis`, `::test_a_base_fee_that_leaves_dust_below_the_minimum_quantity_is_refused_before_exposure`; `test_micro_venue.py::test_base_fee_buy_delivers_fewer_coins_and_quote_fee_bills_usdt` |
| 3 | Third-asset, missing, zero, estimated and confirmed fees stay distinct | `test_micro_economics.py::test_fee_statuses_stay_distinct_and_a_missing_fee_is_never_filled_from_a_configured_rate`, `::test_cost_model_takes_the_larger_fee_and_converts_units` |
| 4 | Venue minimum above the budget: reason, no upsizing | `test_micro_economics.py::test_venue_minimum_above_the_budget_is_rejected_with_a_separate_minimum_and_no_upsizing`, `::test_no_compounding_and_no_upsizing_from_cash_or_allocation`, `::test_a_shortfall_is_blamed_only_when_the_cap_sized_order_would_be_eligible` |
| 5 | Amount, price and cost precision; order-type minimums | `test_micro_economics.py::test_market_order_minimum_not_the_limit_minimum_governs_and_quantities_sit_on_venue_steps`; `test_money_and_venue_rules.py::test_mexc_market_minimum_comes_from_the_market_order_field_when_published`, `::test_missing_market_minimum_falls_back_to_the_limit_minimum_and_says_so`, `::test_decimal_places_precision_mode_becomes_a_step`, `::test_quantize_down_and_up_at_explicit_steps`; `test_micro_venue.py::test_precision_helpers_truncate_and_their_output_is_accepted` |
| 6 | Entry allowed but exit below minimum or dust, before exposure | `test_micro_economics.py::test_entry_allowed_but_exit_below_the_minimum_amount_is_refused_before_exposure`, `::test_venue_rule_codes_pass_through_and_an_entry_needs_a_possible_exit` |
| 7 | Flat executable prices lose with positive costs | `test_micro_economics.py::test_flat_executable_prices_lose_with_positive_costs`, `::test_flat_net_matches_an_independent_round_trip_at_an_unchanged_price`; `test_micro_cycles.py::test_the_maximum_hold_closes_a_flat_position_at_a_labeled_loss` |
| 8 | A gross gain below costs is not a net-profit exit | `test_micro_economics.py::test_a_gross_gain_below_costs_is_not_a_net_profit`; `test_micro_strategy.py::test_a_gross_gain_below_costs_never_takes_profit` |
| 9 | Break-even and target math in every unit, no double counting | `test_micro_economics.py::test_simple_break_even_and_target_ratios_match_the_brief_in_every_unit`, `::test_sensitivity_rows_are_exact_strings_in_percent_bps_and_ratio`, `::test_a_depth_walk_adds_no_flat_slippage_and_bids_are_not_charged_the_spread_again`, `::test_latency_margin_is_applied_once_per_leg` |
| 10 | Partial-close P/L plus residual basis conserves ownership and cash | `test_micro_economics.py::test_partial_closes_conserve_ownership_basis_and_cash_exactly`, `::test_an_exit_estimate_keeps_the_residual_and_its_share_of_the_basis`; `test_micro_cycles.py::test_a_partial_entry_owns_exactly_what_filled_and_the_exit_sells_exactly_that` |
| 11 | Bounded Decimal parsing, adversarial inputs | `test_money_and_venue_rules.py::test_parse_refuses_bool_missing_nonfinite_huge_and_malformed`, `::test_error_messages_never_echo_the_value`; `test_micro_book.py::test_nonfinite_negative_zero_bool_and_garbage_levels_are_refused`; `test_micro_economics.py::test_adversarial_book_levels_are_refused` |
| Budget revision | 25 is a cap, not a floor; budgets scale; no research change to the cap | `test_micro_economics.py::test_the_cap_is_not_a_venue_floor_and_a_smaller_budget_stays_bounded`, `::test_metadata_can_show_a_minimum_below_or_above_the_cap`, `::test_proportional_break_even_is_budget_independent_and_absolute_losses_scale`, `::test_minimum_fee_rounding_and_depth_impact_make_small_and_large_budgets_differ`, `::test_no_research_comparison_can_raise_the_cap`, `::test_no_compounding_and_no_upsizing_from_cash_or_allocation` |
| 12 | Favorable fixture completes buy, hold, sell and a distinct rebuy through the real path (mechanics fixture) | `test_micro_cycles.py::test_mechanics_fixture_runs_buy_hold_sell_and_a_distinct_rebuy`, `::test_one_start_runs_cycles_with_no_further_input` |
| 13 | No-edge fixture abstains, no forced trades | `test_micro_cycles.py::test_markets_without_a_setup_abstain_and_nothing_is_forced`; `test_micro_strategy.py::test_a_random_walk_abstains_on_the_vast_majority_of_bars`; `test_micro_research.py` |
| 14 | Duplicate events and UI retries: no duplicate submit or cycle | `test_micro_cycles.py::test_duplicate_ticks_and_ui_retries_create_no_second_order_or_cycle`, `::test_repeated_exit_all_requests_send_one_sell`; `test_micro_venue.py::test_duplicate_client_order_id_same_terms_returns_the_stored_order_without_a_second_fill`, `::test_concurrent_creates_with_one_client_order_id_fill_once` |
| 15 | Timeout after acceptance: read-only resolution, no resend | `test_micro_cycles.py::test_a_response_lost_after_acceptance_is_resolved_read_only_without_a_resend`; `test_micro_venue.py::test_dropped_response_is_resolved_by_a_read_only_lookup_without_a_resend` |
| 16 | Partial fills, cancelled remainder, cancel and late-fill race, orphan conflict | `test_micro_cycles.py::test_a_partial_entry_owns_exactly_what_filled_and_the_exit_sells_exactly_that`, `::test_a_late_fill_racing_the_cancel_is_booked_from_the_final_read`, `::test_a_fill_reported_after_booking_sends_the_cycle_to_review_and_recovery_books_only_the_extra`, `::test_a_late_fill_after_the_cycle_closed_blocks_until_booked_and_never_ages_out`, `::test_a_late_sale_fill_corrects_the_cycle_totals_and_the_results`, `::test_an_orphan_venue_order_blocks_new_entries_on_that_pair`; `test_micro_venue.py::test_exhausted_depth_cancels_the_remainder_with_an_exact_partial_fill`, `::test_late_fill_reports_a_cancel_first_and_a_larger_cumulative_fill_later` |
| 17 | Reentry needs a settled prior state and a fresh opportunity, across restarts | `test_micro_cycles.py::test_reentry_needs_a_settled_exit_a_new_bar_and_survives_a_restart`, `::test_cooldown_spaces_a_rebuy_on_the_same_pair`, `::test_a_cached_frame_from_the_previous_minute_is_not_used_for_the_new_bar` |
| 18 | Crash before submit, after acceptance, after ledger commit; start-up stays STOPPED and PAPER | `test_micro_cycles.py::test_a_crash_before_the_order_left_releases_the_reservation_at_startup`, `::test_a_crash_after_the_venue_accepted_keeps_the_outcome_blocked_until_reviewed`, `::test_a_crash_after_the_ledger_commit_needs_no_recovery`, `::test_an_exit_that_dies_before_its_intent_never_rebooks_the_earlier_partial_sale`, `::test_a_cycle_interrupted_mid_exit_can_be_recovered_while_stopped`, `::test_the_worker_exit_settles_interrupted_cycles_and_ends_an_exit_all_request` |
| 19 | Concurrent symbols cannot spend the same reserved cash or exceed caps | `test_micro_cycles.py::test_concurrent_entries_cannot_reserve_the_same_paper_cash`, `::test_concurrent_entries_cannot_exceed_the_cycle_capacity`, `::test_capacity_and_allocation_hold_across_pairs`, `::test_a_reservation_stranded_by_a_failure_is_released_on_the_next_pass` |
| 20 | Loss exits need no profit, entry signal or buy depth | `test_micro_cycles.py::test_a_stop_loss_exit_needs_no_profit_no_entry_signal_and_no_depth_book`; `test_micro_strategy.py::test_safety_exits_fire_without_depth_and_without_any_profit`; `test_micro_economics.py::test_exhausted_bid_depth_in_a_crash_still_gives_safety_exits_a_conservative_loss`, `::test_unknown_depth_blocks_entries_but_exits_use_the_flat_fallback` |
| 21 | A removed pair's holding stays monitored | `test_micro_cycles.py::test_a_pair_removed_from_the_selection_keeps_its_holding_managed`, `::test_a_residual_on_a_pair_that_left_the_selection_keeps_a_fresh_mark`, `::test_a_residual_on_a_selected_pair_with_no_setup_keeps_its_mark_inside_the_age_limit`, `::test_a_residual_on_a_plausible_pair_leaves_its_3_s_book_cadence_alone` |
| 22 | Kill, loss, drawdown, reject and order-rate stops cannot self-clear | `test_micro_cycles.py::test_consecutive_losses_latch_and_only_the_phrase_clears_it`, `::test_the_kill_switch_blocks_micro_entries_until_released`, `::test_the_daily_micro_loss_limit_latches`, `::test_a_loss_limit_reached_just_before_a_crash_latches_at_the_next_start`, `::test_a_loss_limit_check_that_failed_after_a_close_runs_before_the_next_entry`, `::test_the_micro_stop_is_carried_to_another_exchange`; `test_risk_gate.py::test_order_rate_limit_blocks_entries_and_only_runaway_exits` and the rest of that file |
| 23 | Stale, crossed, empty books; clock rollback; future timestamps; out of order; reconnect reset | `test_micro_book.py` (46 tests), for example `::test_crossed_and_locked_books_are_refused`, `::test_future_venue_timestamps_are_refused_after_the_clock_offset`, `::test_wall_clock_rollback_while_monotonic_advances_invalidates_every_symbol`, `::test_lower_update_id_is_out_of_order_and_the_stored_book_is_kept`, `::test_invalidate_on_reconnect_stops_serving_and_starts_the_evidence_over`; `test_micro_cycles.py::test_a_pair_with_no_possible_setup_reads_its_book_for_each_new_decision_and_otherwise_every_30_s`, `::test_a_forced_read_reuses_only_a_book_under_half_its_age_limit`, `::test_after_a_memory_reset_an_idle_pair_reads_its_book_for_its_next_decision`. A real sequence-gap check is not implemented: depth comes from REST snapshots (register item 7) |
| 24 | Backpressure, feed loss, REST fallback, rate limits, safe-read retries; Stop keeps confirming an accepted order | `test_quote_feed.py` (for example `::test_unread_quotes_are_coalesced_not_queued`, `::test_feed_is_unhealthy_after_a_heartbeat_lapse_and_refuses_old_quotes`, `::test_feed_start_failure_leaves_rest_polling_in_charge`); `test_market_data.py::test_rate_limit_enters_cooldown_without_retry_burst`; `test_micro_cycles.py::test_stop_during_an_accepted_order_still_books_its_fill`, `::test_a_venue_that_publishes_the_bar_late_is_retried_at_a_bounded_rate` |
| 25 | Slow chart, backtest or catalogue work cannot starve protection | `test_micro_cycles.py::test_slow_candle_downloads_do_not_starve_the_exit_checks`, `::test_a_held_cycle_defers_the_catalogue_refresh_then_makes_one_attempt_after_its_exit_check`, `::test_without_a_holding_a_failing_catalogue_refresh_keeps_its_retries_and_waits_before_the_next`; `test_engine_integrity.py` (chart and backtest reads refused while the worker runs). Residual: one catalogue attempt can take 18 s (register item 12) |
| 26 | No maker fill on touch; takers never charged maker fees; explicit adverse assumptions | `test_micro_venue.py::test_liquidity_is_always_taker_and_a_lower_maker_rate_is_never_charged`, `::test_adverse_bps_moves_every_level_against_the_order`, `::test_adverse_bps_also_moves_the_deeper_levels_an_order_walks_into`. The micro mode never simulates maker orders |
| 27 | No private endpoint, normal keyring or production profile reachable; canaries redacted | `test_micro_refusal.py::test_the_offline_campaign_cannot_reach_a_network_keyring_or_real_profile`, `::test_secret_canaries_are_redacted_from_activity_and_micro_status`; `test_micro_venue.py::test_no_network_and_no_exchange_client_are_used`; `tests/conftest.py` (set before any import) |
| 28 | Micro LIVE start, direct buy and final submit denied, no bypass | `test_micro_refusal.py` (the first nine tests, including `::test_the_refusal_has_no_override_parameter_or_environment_switch` and `::test_the_final_submit_refuses_a_live_micro_order_even_with_a_valid_looking_authorization`) |
| 29 | Loopback, Origin, token, CSP, malformed settings, UI escaping, diagnostic export | `test_security_storage.py::test_exact_origin_required`, `::test_local_action_token_and_security_headers`, `::test_audit_redacts_registered_keys_and_signed_headers`; `test_manual_api.py::test_manual_order_requires_action_token`; `test_micro_refusal.py::test_micro_actions_need_the_action_token_and_strict_input`; `test_settings_recovery.py`; `tests/test_dashboard.js` (escaping) |
| 30 | Windows source edit and restart, missing module, TLS policy file, duplicate process and port, update, backup, rollback, profile isolation | `test_editable_runtime.py::test_source_edit_takes_effect_on_restart_even_with_stale_pyc`, `::test_missing_source_and_assets_fail_closed`, `::test_new_editable_modules_and_packages_can_be_imported`, `::test_runtime_tls_check_requires_certificate_and_hostname_verification`; `test_tls_packaging.py::test_editable_spec_preserves_optional_interpreter_trust_policy`; `test_launcher.py::test_instance_lock_prevents_second_process_and_releases`, `::test_loopback_port_is_exclusive`; `test_source_updates.py::test_interrupted_update_restores_previous_source`, `::test_running_install_blocks_updates_even_with_a_different_data_profile`; `test_installer.py`. These run on Linux here and on the Windows CI runner; a clean-machine install was **NOT RUN** |

## Wall-clock smoke (real engine thread, fixture markets)

```
python scripts/micro_soak.py --data-dir <a new folder> --minutes 20 --fixture --sample-seconds 30
```

The harness refuses a folder that is not empty and any folder that overlaps the normal profile, starts the
real engine thread in PAPER on three fixture markets (the ETH mechanics
fixture's price swings, a flat BTC, a seeded SOL random walk) that advance on the real clock,
with streaming off, and samples status, metrics and the process every 30 s.
These are synthetic markets: the runs prove the engine runs unattended
through bars, cycles and exits, not anything about a market. Twenty minutes
is a smoke run, not a soak.

**Fourth run, on `ec3c59f`** (whose engine paths are the final code's but
for `money.parse`: the final code adds, in `6aeeb01`, the account-fee parser, a
`cost_model` argument no engine path passes, and in `money.parse` a float
read through `float`'s own repr and an exact magnitude bound, which give the
same result for every plain float and in-range value; the working tree held only uncommitted edits to these
documents and the research result file, so the summary records it as dirty), 01:14 to 01:34 UTC on 27 October: completed,
1200.1 s, 40 samples, no faults, PAPER throughout, worker alive in every
sample, no risk block, no latch, no review block, no error and no refused
book. The same cycle closed on the mechanics fixture by take profit after
653 s, net +0.0723 USDT, fees 0.0501 USDT, as in the earlier runs. Book reads
per minute: BTC 14.31, ETH 31.59, SOL 10.57. Two threads in every sample;
maximum resident memory 126.9 MB at the first sample and 132.8 MB at the
last. The summary records the source as `ec3c59f`, 37 files with SHA-256
`3a766f0c...4b08f9`, unchanged during the run, and `remote_network_refused`
true. Six pair samples showed STALE_DATA (ETH 1, BTC 2, SOL 3), at three
sample times each about 7 s after a bar close, 5.7 to 6.0 s into the wait:
the documented wait for
the new bar's candles (`docs/ATB_MICRO_ARCHITECTURE.md`, *Data path and
cadence*), which this run's sample times happened to catch. Two 5-minute
runs with 1-second samples, side by side on `fb52624` and `ec3c59f`, showed
the same wait, starting 0 to 2 s after every bar close and lasting 5 to 7 s,
on every pair not holding a cycle (`docs/ATB_MICRO_PERFORMANCE.md`). A documentation fact-check ran alongside
its last ten minutes.

**Third run, on `fb52624`** (the code of `20c7067`, after both review rounds
of the final fixes), 23:28 to 23:48 UTC: completed, 1200.0 s, 40 samples, no
faults, PAPER throughout, worker alive in every sample, no risk block, no
latch, no review block, no error, no STALE_DATA and no refused book. The
same cycle closed on the mechanics fixture by take profit after 654 s, net
+0.0723 USDT, fees 0.0501 USDT, as in both earlier runs. Book reads per
minute: BTC 14.36, ETH 31.59, SOL 10.15. Two threads in every sample; maximum
resident memory 126.5 MB at the first sample and 132.2 MB at the last. The
summary records the source as `fb52624`, clean, 37 files with SHA-256
`bca1b748...b309f1`, unchanged during the run, and `remote_network_refused`
true. Its latencies are higher than the second run's
(`docs/ATB_MICRO_PERFORMANCE.md`).

**Second run, on `9535a55`**, 20:09 to 20:29 UTC:
completed, 1200.0 s, 40 samples, no faults, PAPER throughout, worker alive in
every sample, no risk block, no latch, no review block, no error, and no
STALE_DATA on any pair. The SOL pair, which showed it four times in the first
run, waited on WEAK_TREND in all 40 samples. The same cycle closed on the
mechanics fixture as in the first run, by take profit after 654 s, net
+0.0723 USDT, fees 0.0501 USDT; the fixture markets are the same series, so
the identical result is expected and says nothing about a market. Book reads
per minute: BTC 14.26 and ETH 31.49 (as in the first run), SOL 10.77 (11.44
before); no book was refused. Two threads in every sample; maximum resident memory
127.3 MB at the first sample and 133.0 MB at the last. The summary records
the source as `9535a55`, clean, code files SHA-256 `4d26cf24...a2ceca`, and
unchanged during the run. A 1200 s run on synthetic markets is a smoke run:
the 24 to 72 hour soak on Windows with real public data is **NOT RUN**.

**First run, on `c043668`**, 17:35 to 17:55 UTC: completed, 1200.0 s, 40
samples, no faults, PAPER throughout, worker alive in every sample, no risk
block, no latch, no review block, no error. One cycle closed on the mechanics
fixture by take profit after 654 s: net +0.0723 USDT, fees 0.0501 USDT on
50.06 USDT of turnover. Two threads in every sample; maximum resident memory
127.1 MB at the first sample and 132.6 MB at the last. In the last two
minutes the SOL pair showed STALE_DATA in 4 of 40 samples, each time
15 to 23 s after a bar close or after its candles became current, with a book
25 to 28 s old: the defect described under *Reviews*, fixed in `9535a55`. The
summary records the source at the start (`c043668` with an edited README,
code files SHA-256 `2e863a39...e364ec`). It also reports the source as
changed during the run, because a documentation-only commit (`d76f404`) was
made while it ran; the code files' hash at the end was the same.

**Two runs not counted above.** An earlier 20-minute run, 16:35 to 16:55 UTC
on 26 October, ran on an uncommitted development tree that was still being
committed while it ran. The harness then hashed the source when a run ended
(`c4587f8` moved that to the start), so its summary records `fa45c70`, dirty,
which is not the code it started on. It completed, 1200.0 s, 40 samples, no
faults, PAPER throughout, no STALE_DATA, and closed the same mechanics-fixture
cycle by take profit (net +0.0723 USDT); because its code cannot be
identified, it is not counted. A run on `fb52624`, started at 23:10 UTC, was
stopped by a session interruption after 34 samples (1020 s; no fault and no
STALE_DATA in them) and wrote no summary; the third run above is its rerun.

## Research run

`python -m research.micro_research --out <scratch folder>/micro_fixtures.json`
in a clean `git worktree` of `6aeeb01` (protocol `micro-research-v3`; the
result file records the commit, that the decision and research files were
unmodified, and their hash, `4d8beb5b...7ab3297`, which `82913ca` keeps since
it changes only a test file), run inside an empty network namespace
(`unshare -n`), so it could not reach any network: exit 0, 03:01 to 03:30 UTC
on 27 October, 28 min 38 s wall clock (the report records 1716.9 s); the
file was then copied to `research/results/micro_fixtures.json`. Two full test
suites ran alongside it, so its run time says nothing about the code.
Compared field by field with the first version 3 run, on `cb7d082` (00:13 to
00:41 UTC, 1704.2 s), every figure is equal; only the code identity and the
run times differ. That run in turn equalled version 2's run on `20c7067` in
every figure. At 25 USDT on the untouched holdout with baseline costs it
closed 21 cycles on the ETH mechanics fixture, each by profit protection at
about 0.0126 USDT, and none on the other four fixtures. Results and their
meaning are in `docs/ATB_MICRO_RESEARCH.md`, including why every earlier
fixture run is superseded. Real data: **NOT RUN**.

## Windows

The `windows-installer` workflow runs the full pytest suite, builds the
installer and tests it in an isolated folder on `windows-latest` for every
push to the pull request. Its result for the final commit is in the pull
request description: a file cannot record the run of the commit it is in.

## Source archive

`python scripts/make_source_archive.py --out dist` builds a ZIP of exactly the
files of the HEAD commit, read from git's objects rather than the working
tree. It refuses a dirty tree, a committed link or submodule, and any file
that looks like a database (by name or content), a settings, environment or
log file, a key or a credential assignment, and it writes a manifest with
every file's SHA-256 and the archive's own. Entries are stored uncompressed
with a fixed time, permissions and creating system, so the same commit gives
the same archive hash on Windows and Linux (a test builds it with the
platform set to Windows and compares the hashes; no archive built on a
Windows host has been compared with a Linux one yet).
Until the final review it refused
this repository itself (see *Reviews*), so no archive existed before then. A
file cannot contain the hash of the archive it is in, so the archive of the
final commit and its hash are recorded in the pull request description and
the final report, not here.

## Not run

* Anything against a real exchange: no public-data collection, no real
  order book, no account, no order. The exchanges are unreachable from this
  host, and the brief forbids authenticated calls.
* A 24 to 72 hour soak, on Windows or anywhere. The wall-clock smoke above
  is minutes long on synthetic markets; it is not a soak.
* A clean-machine Windows install, update, edit, rollback and uninstall of
  this candidate.
* Latency over a real network or on a Windows host.
* Sleep, hibernation and network loss on a real PC.
