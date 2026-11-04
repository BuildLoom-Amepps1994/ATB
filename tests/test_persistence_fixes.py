"""Regression tests for the persistence findings of the 2026-11-04 audit.

* ``--clear-halt`` reconciled a live intent the exchange had filled but the
  ledger never booked, lifting the halt with the holding untracked.
* A settings file from an older build or a hand edit that lacked a changed
  risk cap silently put that cap back to its default once a last-good copy
  existed.
* The consecutive-loss streak was a separate counter written after the sell
  committed, so a crash in between lost a loss for good.
* Live positions were invisible and unwarned in paper mode (hardening
  review finding 20).
* A live intent that never left the process halted live trading until the
  offline tool was run.
* A damaged ledger was opened and migrated without an integrity check.
* An exchange switch left the target exchange's stale paper intents open.

All exchanges are fakes; no network, no credentials.
"""

import json

import pytest

import launcher
import recovery
import risk
import storage
import trader
from test_engine import arm_fake_live, engine  # noqa: F401  (fixture)
from test_risk_governance import losing_round_trip


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(storage, 'DB_PATH', tmp_path / 'portfolio.sqlite3')
    monkeypatch.setattr(storage, 'CONFIG_PATH', tmp_path / 'settings.json')
    monkeypatch.setattr(storage, '_SETTINGS_CACHE', None)
    monkeypatch.setenv('CRYPTOPILOT_DATA_DIR', str(tmp_path))
    storage.init_db()
    return tmp_path


def _filled_unbooked(exchange='mexc', symbol='BTC/USDT', side='BUY'):
    """The state a crash leaves between the journal's 'filled' write and the ledger transaction."""
    intent = storage.create_order_intent(exchange, symbol, side, cost=130, intended_price=65000)
    storage.update_order_intent(intent, 'pending', submitted_at=storage._now())
    storage.update_order_intent(intent, 'acked', order_id='exchange-order-1')
    storage.update_order_intent(intent, 'filled', filled_quantity=0.002, filled_price=65000.0)
    return intent


def _last_audit():
    return storage.rows('SELECT message FROM audit ORDER BY id DESC LIMIT 1')[0]['message']


# ---------------------------------------------------------------- clear-halt
def test_clear_halt_refuses_a_filled_but_unbooked_live_intent(data, capsys):
    intent = _filled_unbooked()
    uncertain = storage.create_order_intent('mexc', 'ETH/USDT', 'SELL', quantity=1)
    storage.update_order_intent(uncertain, 'uncertain')
    storage.set_meta('live_halt.mexc', '1')
    before = storage.DB_PATH.read_bytes()
    with pytest.raises(ValueError) as refused:
        recovery.clear_halt(data, 'mexc', recovery.confirmation_phrase('mexc'))
    message = str(refused.value)
    assert 'filled at the exchange but never booked' in message
    assert 'BUY BTC/USDT 0.002 at 65000 (intent ' + intent + ')' in message
    assert 'acknowledge-unbooked-fills' in message and 'exchange-order-1' not in message
    assert recovery.main(['--clear-halt', 'mexc', '--confirm', recovery.confirmation_phrase('mexc')]) == 1
    assert 'Nothing was changed' in capsys.readouterr().err
    assert storage.DB_PATH.read_bytes() == before
    assert storage.get_meta('live_halt.mexc') == '1' and len(storage.unresolved_orders()) == 2


def test_clear_halt_reconciles_an_acknowledged_unbooked_fill_and_records_it(data, capsys):
    _filled_unbooked()
    storage.set_meta('live_halt.mexc', '1')
    assert recovery.main(['--clear-halt', 'mexc', '--confirm', recovery.confirmation_phrase('mexc'),
                          '--acknowledge-unbooked-fills']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['reconciled_orders'] == 1 and result['unbooked_fills_acknowledged'] == 1 and result['halt_flag_removed']
    assert storage.unresolved_orders() == []
    assert '1 filled-but-unbooked intent(s) acknowledged by the operator' in _last_audit()
    # The flag means nothing on its own.
    assert recovery.main(['--acknowledge-unbooked-fills']) == 1
    assert 'only applies with --clear-halt' in capsys.readouterr().err


def test_clear_halt_without_unbooked_fills_is_unchanged(data):
    uncertain = storage.create_order_intent('mexc', 'ETH/USDT', 'SELL', quantity=1)
    storage.update_order_intent(uncertain, 'uncertain')
    result = recovery.clear_halt(data, 'mexc', recovery.confirmation_phrase('mexc'))
    assert result['reconciled_orders'] == 1 and result['unbooked_fills_acknowledged'] == 0
    assert 'acknowledged' not in _last_audit()


def test_recovery_report_shows_when_an_intent_left_and_what_it_filled(data):
    filled = _filled_unbooked()
    never_sent = storage.create_order_intent('mexc', 'SOL/USDT', 'BUY', cost=20)
    report = recovery.report(data)
    rows = {row['intent_id']: row for row in report['unresolved_orders']}
    assert rows[filled]['status'] == 'filled'
    assert rows[filled]['filled_quantity'] == 0.002 and rows[filled]['filled_price'] == 65000.0
    assert rows[filled]['submitted_at'] and rows[filled]['acked_at']
    assert rows[never_sent]['status'] == 'pending'
    assert (rows[never_sent]['submitted_at'], rows[never_sent]['acked_at'],
            rows[never_sent]['filled_quantity'], rows[never_sent]['filled_price']) == (None, None, None, None)
    assert report['unbooked_fill_count'] == 1 and report['unresolved_order_count'] == 2
    assert 'exchange-order-1' not in json.dumps(report)


# ------------------------------------------------------------------ settings
@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(storage, 'DB_PATH', tmp_path / 'portfolio.sqlite3')
    monkeypatch.setattr(storage, 'CONFIG_PATH', tmp_path / 'settings.json')
    monkeypatch.setattr(storage, '_SETTINGS_CACHE', None)
    return tmp_path


def _reread():
    storage._SETTINGS_CACHE = None
    return storage.settings(), storage.settings_health()


def test_a_settings_file_that_drops_a_changed_cap_requires_review(profile):
    approved = {**storage.DEFAULTS, 'max_order_notional_usdt': 100.0, 'max_consecutive_losses': 2}
    storage.save_settings(approved)
    assert storage.settings()['max_order_notional_usdt'] == 100.0
    # An older build saves only the keys it knows; a hand edit can drop a key the same way.
    older = {key: value for key, value in approved.items()
             if key not in ('max_order_notional_usdt', 'max_consecutive_losses', 'max_entry_atr_pct')}
    storage.CONFIG_PATH.write_text(json.dumps(older), encoding='utf8')
    current, health = _reread()
    assert current['max_order_notional_usdt'] == 100.0 and current['max_consecutive_losses'] == 2
    assert health['review_required'] and health['using_last_known_good'] and health['reason'] == 'invalid_values'


def test_a_file_missing_only_default_valued_later_fields_is_adopted_without_review(profile):
    older = {key: value for key, value in storage.DEFAULTS.items() if key not in storage.LATER_FIELDS}
    storage.CONFIG_PATH.write_text(json.dumps(older), encoding='utf8')
    current, health = _reread()
    assert current['max_consecutive_losses'] == storage.DEFAULTS['max_consecutive_losses'] and health['status'] == 'ok'
    # The adopted last-good copy carries the defaults, so the same file stays clean on a re-read.
    assert _reread()[1]['status'] == 'ok'
    # After a save that tightened a cap, the same older file is a rollback and needs review.
    storage.save_settings({**storage.DEFAULTS, 'max_asset_notional_usdt': 2000.0})
    storage.CONFIG_PATH.write_text(json.dumps(older), encoding='utf8')
    current, health = _reread()
    assert current['max_asset_notional_usdt'] == 2000.0 and health['review_required']


# --------------------------------------------------------------- loss streak
def _book_sell(engine, realized, symbol='ETH/USDT'):
    """A sell the ledger holds but observe_exit never saw (a crash in between)."""
    with storage.connection() as db:
        db.execute('INSERT INTO trades(created_at,exchange,mode,symbol,side,price,quantity,gross,fee,realized,reason,order_id) '
                   'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                   (trader.now(), engine.exchange_name, 'paper', symbol, 'SELL', 90.0, 1.0, 90.0, .045, realized, 'stop', None))


def test_the_loss_streak_is_read_from_the_trade_ledger(engine):
    for _ in range(3):
        losing_round_trip(engine)
    storage.set_meta(engine.risk._key('loss_streak', 'paper'), '{not json')       # no counter to lose
    assert engine.risk.loss_streak('paper')['count'] == 3
    _book_sell(engine, -10.0)
    assert engine.risk.loss_streak('paper')['count'] == 4
    # The streak is at the limit although no exit was observed: the check latches it.
    assert engine.risk.loss_streak_stop('paper')
    assert not engine._buy('BTC/USDT', 200, 100, .04, volume=20000000)
    assert engine.risk.last_block['reasons'][0].startswith('Consecutive-loss stop latched')
    assert 'consecutive-loss stop latched: 4 losing exits' in storage.rows(
        "SELECT message FROM audit WHERE level='error' ORDER BY id DESC LIMIT 1")[0]['message']


def test_a_winning_sell_in_the_ledger_ends_the_streak_and_clearing_restarts_it(engine):
    for _ in range(3):
        losing_round_trip(engine)
    _book_sell(engine, 2.5)
    assert engine.risk.loss_streak('paper')['count'] == 0
    _book_sell(engine, -1.0)
    assert engine.risk.loss_streak('paper')['count'] == 1
    for _ in range(3):
        losing_round_trip(engine)
    assert engine.risk.loss_streak_stop('paper')
    engine.risk.clear_loss_streak_stop('paper', risk.RELEASE_PHRASE)
    assert engine.risk.loss_streak('paper')['count'] == 0 and engine.risk.loss_streak_stop('paper') is None
    _book_sell(engine, -1.0)                                           # only sells after the clearing count
    assert engine.risk.loss_streak('paper')['count'] == 1


# ---------------------------------------------------- live holdings in paper
def test_live_positions_are_counted_and_warned_about_in_paper_mode(engine):
    arm_fake_live(engine)
    assert engine._buy('BTC/USDT', 200, 100, .04)
    assert engine.snapshot()['live_positions'] == 1
    engine.running = False                                             # arm_fake_live marks the worker as running
    engine.set_mode('paper')
    state = engine.snapshot()
    assert state['mode'] == 'paper' and state['positions'] == [] and state['live_positions'] == 1
    assert '1 live position(s) are stored for MEXC while the app is in paper mode' in _last_audit()
    storage.audit('info', 'something else')
    trader.TradingEngine()                                             # a restart says so before anything else
    assert 'stored stops are not monitored' in _last_audit()


def test_no_warning_without_live_positions(engine):
    engine.set_mode('paper')
    assert 'live position(s)' not in _last_audit() and engine.snapshot()['live_positions'] == 0


# -------------------------------------------------------------- never sent
def test_live_intents_that_never_left_are_closed_at_startup(engine):
    exchange = engine.exchange_name
    never_sent = storage.create_order_intent(exchange, 'SOL/USDT', 'BUY', cost=20)
    sent = storage.create_order_intent(exchange, 'BTC/USDT', 'BUY', cost=20)
    storage.update_order_intent(sent, 'pending', submitted_at=storage._now())
    legacy = storage.create_order_intent(exchange, 'ETH/USDT', 'BUY', cost=20)
    with storage.connection() as db:
        db.execute('UPDATE order_journal SET client_order_id=NULL WHERE intent_id=?', (legacy,))
    paper = storage.create_order_intent(exchange, 'SOL/USDT', 'BUY', cost=20, mode='paper')
    with storage.connection() as db:                                   # a paper intent is handled by its own rule
        db.execute("UPDATE order_journal SET decision_id='cyc_1' WHERE intent_id=?", (paper,))
    restarted = trader.TradingEngine()
    rows = {row['intent_id']: row for row in storage.rows('SELECT * FROM order_journal')}
    assert rows[never_sent]['status'] == 'rejected' and rows[never_sent]['note'] == 'never sent; closed at startup'
    assert rows[sent]['status'] == 'pending' and rows[legacy]['status'] == 'pending' and rows[paper]['status'] == 'pending'
    assert restarted.live_halted and {row['intent_id'] for row in storage.unresolved_orders()} == {sent, legacy}
    assert '1 live order intent(s) that were never handed to the exchange were closed at startup' in _last_audit()


# ------------------------------------------------------------ integrity
def test_a_filled_but_unbooked_live_fill_is_named_at_startup(engine):
    intent = storage.create_order_intent(engine.exchange_name, 'BTC/USDT', 'BUY', cost=130, mode='live')
    storage.update_order_intent(intent, 'pending', submitted_at=storage._now())
    storage.update_order_intent(intent, 'acked', order_id='venue-7')
    storage.update_order_intent(intent, 'filled', filled_quantity=.002, filled_price=65000.0)
    trader.TradingEngine()
    lines = [row['message'] for row in storage.rows('SELECT message FROM audit ORDER BY id')]
    assert any(line.startswith(f'LIVE BUY BTC/USDT filled at the exchange but never booked: 0.002 at 65000.0 (intent {intent}); '
                               'no stop is monitored for it.') for line in lines)


def test_a_damaged_ledger_is_refused_before_any_migration(data, capsys):
    for _ in range(40):
        storage.create_order_intent('mexc', 'BTC/USDT', 'BUY', cost=10, mode='paper')
    raw = bytearray(storage.DB_PATH.read_bytes())
    page = int.from_bytes(raw[16:18], 'big') or 4096
    pages = len(raw) // page
    assert pages >= 3
    for number in range(2, min(pages, 6) + 1):                        # cell pointers of the b-tree pages
        offset = (number - 1) * page + 12
        raw[offset:offset + 96] = b'\xff' * 96
    storage.DB_PATH.write_bytes(bytes(raw))
    with pytest.raises(storage.DatabaseDamaged, match='integrity check') as failure:
        storage.init_db()
    assert storage.DB_PATH.read_bytes() == bytes(raw)
    launcher.report_startup_failure(failure.value)
    out = capsys.readouterr().out
    assert 'could not start: DatabaseDamaged' in out and 'Restore portfolio.sqlite3 from a backup' in out


def test_an_intact_ledger_passes_the_check(data):
    storage.init_db()
    assert storage.rows('PRAGMA quick_check(1)')[0]['quick_check'] == 'ok'


# ------------------------------------------------------------ exchange switch
def test_an_exchange_switch_closes_the_target_exchanges_stale_paper_intents(engine):
    stale = storage.create_order_intent('kraken', 'BTC/USD', 'BUY', cost=10, mode='paper')
    engine.update_config({'exchange': 'kraken'})
    row = storage.order_intent(stale)
    assert row['status'] == 'rejected' and row['note'] == 'abandoned at startup'


def test_an_oversized_integer_setting_is_a_validation_error(profile):
    with pytest.raises(storage.ConfigValidationError, match='max_positions must be between'):
        storage.validate_settings({'max_positions': 10 ** 400})
