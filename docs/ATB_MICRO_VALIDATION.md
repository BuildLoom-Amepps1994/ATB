# Micro-trading mode: validation

What was run for this candidate, on which source, with which result, and what
was not run. Every count below is from a log of an actual run. Focused runs
overlap the full suite and are never added to it.

## Source and environment

| Item | Value |
|---|---|
| Branch | `claude/project-thread-vqle7r`, from main `b1e0217` |
| Code commit tested | `9535a55` (the commits after it change documentation only; `git diff 9535a55 HEAD -- '*.py' '*.js' '*.html' '*.cjs' '*.yml' '*.iss' '*.txt'` is empty) |
| Host | Linux cloud container, 4 CPUs, Python 3.13.17, Node 22.22.0 |
| Dependencies | `requirements-lock.txt` as on main (ccxt 4.5.90, pandas 3.1.0) |
| Isolation | `tests/conftest.py` sets a new temporary `CRYPTOPILOT_DATA_DIR` and `LOCALAPPDATA`, the null keyring backend and no update checks before any application module is imported, and refuses every non-loopback connection. Every pytest and benchmark command below also exported a new `CRYPTOPILOT_DATA_DIR` under `/tmp` and `PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring` first; the soak sets both itself for its `--data-dir` folder |
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
| `pytest tests` (Python 3.13.17) | 1317 | 0 | 0 | 0 | 278.4 s (wall 4 min 40 s) |
| `node --test tests/test_dashboard.js` (Node 22.22.0) | 19 | 0 | 0 | 0 | 0.2 s |

The two pytest warnings are aiohttp's `enable_cleanup_closed` deprecation
notice from the websocket tests, unrelated to this change.

**Intermittents.** None seen in the final runs or in the focused runs before
them. No test was retried to get a pass, and no timeout was raised. The cycle
tests run on a fixture clock (`FixtureClock`) that adds each tick's real
duration to its fixed step, so a slow host can still change them: review
round 3 found one such test and it was fixed, and every cycle test passed with
each tick slowed by 15 ms (see *Focused runs*).

**Changed expectations.** Two pre-existing test files changed. In
`tests/test_recovery.py` the expected database schema version moved from 3 to
4, for the new micro tables. `tests/test_dashboard.js` gained three micro
panel tests and imports two more helpers; its existing tests are unchanged.
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
| Slow-host check of every cycle test on the final code | `tests/test_micro_cycles.py`, each tick slowed by 15 ms of real time | 58 passed, 292.2 s |
| New test for the 25 USDT settings bound | `tests/test_micro_refusal.py` | 15 passed; the new test fails if the bound is raised to 50 or if `MicroManager.budget_cap` stops taking the minimum with the 25 USDT constant |

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

**Second run, on `9535a55`** (the final code), 20:09 to 20:29 UTC:
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

## Research run

`python -m research.micro_research --out research/results/micro_fixtures.json`
on `c043668` (`9535a55` changed only the book cadence in `micro.py`, which the research harness does not use): exit 0, 12 min 12 s wall clock (the report records a run time of 731.1 s). At 25 USDT on the untouched holdout with baseline costs it closed 21 cycles on the ETH mechanics fixture and none on the other four fixtures. Results and their meaning are in
`docs/ATB_MICRO_RESEARCH.md`. Real data: **NOT RUN**.

## Windows

The `windows-installer` workflow runs the full pytest suite, builds the
installer and tests it in an isolated folder on `windows-latest` for every
push to the pull request. Its result for the final commit is in the pull
request description: a file cannot record the run of the commit it is in.

## Source archive

`python scripts/make_source_archive.py --out dist` builds a ZIP of exactly the
files git tracks at the commit, refuses a dirty tree and any file that looks
like a database, a settings file, a key or a credential assignment, and
writes a manifest with every file's SHA-256 and the archive's own. A file
cannot contain the hash of the archive it is in, so the archive of the final
commit and its hash are recorded in the pull request description and the
final report, not here.

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
