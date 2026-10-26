"""Targeted offline identity, submission chronology, and UI isolation regressions."""
import threading
import time

import pytest

import storage
import trader
from test_engine import FakeExchange, arm_fake_live, engine


def test_credentials_cannot_replace_tracked_live_account(engine, monkeypatch):
    engine.connect('synthetic-account-A', 'synthetic-secret')
    engine.mode, engine.running = 'live', True
    assert engine._buy('BTC/USDT', 800, 100, .04)
    engine.mode, engine.running = 'paper', False
    original = engine._new_exchange
    monkeypatch.setattr(engine, '_new_exchange', lambda *a: pytest.fail('different identity must fail before authentication'))
    with pytest.raises(ValueError, match='identity'):
        engine.connect('synthetic-account-B', 'synthetic-secret')
    monkeypatch.setattr(engine, '_new_exchange', original)
    engine.connect('synthetic-account-A', 'synthetic-secret')
    assert engine.private is not None
    fingerprint = storage.get_meta(engine._key('live_account_fingerprint'))
    assert len(fingerprint) == 64
    assert 'synthetic-account' not in fingerprint


def test_unbound_existing_live_records_block_saved_credentials(engine, monkeypatch):
    arm_fake_live(engine)
    assert engine._buy('BTC/USDT', 800, 100, .04)
    engine.private = None
    engine.api_key, engine.api_secret = 'unverified-account', 'synthetic-secret'
    monkeypatch.setattr(engine, '_new_exchange', lambda *a: pytest.fail('unverified identity must not authenticate'))
    with pytest.raises(ValueError, match='identity'):
        engine._ensure_private()


def test_current_day_live_risk_state_prevents_account_replacement(engine):
    engine.connect('synthetic-account-A', 'synthetic-secret')
    engine._daily_allowed('live', 10000)
    with pytest.raises(ValueError, match='risk state'):
        engine.connect('synthetic-account-B', 'synthetic-secret')


def test_exchange_constructor_failure_cannot_mix_clients_and_ledger(engine, monkeypatch):
    engine.connect('synthetic-account-A', 'synthetic-secret')
    previous_public, previous_private = engine.public, engine.private
    def constructor(*args):
        raise RuntimeError('synthetic constructor failure')
    monkeypatch.setattr(engine, '_new_exchange', constructor)
    with pytest.raises(RuntimeError):
        engine.update_config({'exchange': 'kraken'})
    assert engine.exchange_name == engine.cfg['exchange'] == storage.settings()['exchange'] == 'mexc'
    assert engine.public is previous_public and engine.private is previous_private


def test_stop_during_balance_read_prevents_order_submission(engine):
    client = arm_fake_live(engine)
    entered, release = threading.Event(), threading.Event()
    original = client.fetch_balance
    def balance(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)
    client.fetch_balance = balance
    worker = threading.Thread(target=lambda: engine._buy('BTC/USDT', 800, 100, .04))
    worker.start()
    assert entered.wait(1)
    engine.stop()
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert client.orders == []
    assert storage.unresolved_orders() == []


def test_stop_after_intent_before_send_rejects_known_unsent_order(engine, monkeypatch):
    client = arm_fake_live(engine)
    original = storage.create_order_intent
    def intent(*args, **kwargs):
        identifier = original(*args, **kwargs)
        engine.stop_event.set()
        return identifier
    monkeypatch.setattr(storage, 'create_order_intent', intent)
    assert not engine._buy('BTC/USDT', 800, 100, .04)
    assert client.orders == []
    assert storage.unresolved_orders() == []
    assert storage.rows('SELECT status FROM order_journal')[0]['status'] == 'rejected'


def test_stop_cancels_start_waiting_for_account_read(engine):
    engine.mode, engine.private = 'live', FakeExchange()
    entered, release = threading.Event(), threading.Event()
    original = engine.private.fetch_balance
    def balance(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)
    engine.private.fetch_balance = balance
    starter = threading.Thread(target=engine.start)
    starter.start()
    assert entered.wait(1)
    # Polling state must remain responsive while this private read is blocked.
    assert engine.snapshot()['mode'] == 'live'
    engine.stop()
    release.set()
    starter.join(2)
    assert not starter.is_alive()
    assert not engine.running and engine.thread is None


@pytest.mark.parametrize('mutation', [
    {'id': None}, {'filled': 1e-308, 'cost': 1e308, 'average': None},
    {'filled': 1., 'cost': 100., 'average': 1000.},
    {'fee': {'currency': 'USDT', 'cost': -1}}, {'fee': {'currency': 'USDT', 'cost': 'free'}},
])
def test_invalid_fill_identity_price_or_fee_remains_unresolved(engine, mutation):
    # A fee billed in a third asset is no longer in this list: the fill is booked and the
    # fee value flagged instead (tests/test_review_round2.py), so the holding stays protected.
    client = arm_fake_live(engine)
    client.fill.update(mutation)
    assert not engine._buy('BTC/USDT', 800, 100, .04)
    assert engine.live_halted
    assert len(client.orders) == 1
    assert len(storage.unresolved_orders()) == 1
    assert engine._active_positions('live') == []


def test_aggregate_stop_risk_includes_profitable_position_giveback(engine):
    engine.cfg.update({'fee_pct': 0, 'slippage_pct': 0})
    engine.last_prices['BTC/USDT'] = 150
    position = {'symbol': 'BTC/USDT', 'qty': 1, 'entry_price': 100, 'cost': 100, 'stop': 110}
    assert engine._portfolio_risk([position]) == 40


def test_research_and_uncached_chart_cannot_delay_running_worker(engine, monkeypatch):
    engine.running = True
    monkeypatch.setattr(engine.data, 'read', lambda *a, **kw: pytest.fail('UI may not download while worker runs'))
    with pytest.raises(ValueError, match='Stop the bot'):
        engine.research_backtest('BTC/USDT')
    with pytest.raises(ValueError, match='cached chart'):
        engine.history('BTC/USDT')


def test_running_chart_uses_cached_frame_without_network(engine, monkeypatch):
    engine.data.candles('BTC/USDT', '1h', limit=175)
    engine.running = True
    monkeypatch.setattr(engine.data, 'read', lambda *a, **kw: pytest.fail('cached chart may not download'))
    assert engine.history('BTC/USDT')[0]['ema50'] is None


def test_chart_network_path_rechecks_worker_after_start_race(engine, monkeypatch):
    original = engine._acquire_analysis
    def acquire(**kwargs):
        # Simulate Start winning after history's initial active-state check.
        engine.running = True
        return original(**kwargs)
    monkeypatch.setattr(engine, '_acquire_analysis', acquire)
    monkeypatch.setattr(engine.data, 'read', lambda *a, **kw: pytest.fail('chart raced Start and downloaded'))
    with pytest.raises(ValueError, match='Stop the bot'):
        engine.history('BTC/USDT')
    assert not engine.scan_lock.locked()


@pytest.mark.parametrize('invalid_clock', ['wall_rollback', 'monotonic_expiry'])
def test_running_chart_rejects_clock_invalid_cache_without_network(engine, monkeypatch, invalid_clock):
    engine.data.candles('BTC/USDT', '1h', limit=175)
    key = ('BTC/USDT', '1h', 175)
    expiry, frame, fetched, monotonic_expiry = engine.data.cache[key]
    if invalid_clock == 'wall_rollback':
        engine.data.cache[key] = (expiry, frame, time.time() + 10, monotonic_expiry)
    else:
        engine.data.cache[key] = (expiry, frame, fetched, time.monotonic() - 1)
    engine.running = True
    monkeypatch.setattr(engine.data, 'read', lambda *a, **kw: pytest.fail('invalid cached chart may not download'))
    with pytest.raises(ValueError, match='cached chart'):
        engine.history('BTC/USDT')


def test_software_exit_cannot_reenter_same_symbol_in_same_scan(engine, monkeypatch):
    engine._buy('BTC/USDT', 800, 100, .04)
    engine.public.price, engine.running = 112, True
    monkeypatch.setattr(trader, 'entry_signal', lambda *a, **kw: (True, 'forced', 100))
    # The quote is far from the flat fake candles; only the exit may exclude BTC.
    monkeypatch.setattr(trader, 'entry_price_ok', lambda *a, **kw: True)
    seen = []
    monkeypatch.setattr(engine, '_enter_candidates', lambda rows: seen.extend(rows))
    engine.scan_once(allow_orders=True)
    assert not any(p['symbol'] == 'BTC/USDT' for p in engine._active_positions('paper'))
    assert storage.rows("SELECT reason FROM trades WHERE side='SELL'")
    assert seen and not any(row[2] == 'BTC/USDT' for row in seen)
    assert any(row[2] == 'ETH/USDT' for row in seen)


def test_executable_quote_too_far_from_signal_close_blocks_entry(engine):
    engine.running = True
    engine.last_prices['BTC/USDT'], engine.price_times['BTC/USDT'] = 100, time.time()
    engine._enter_candidates([(100, 20_000_000, 'BTC/USDT', 100, 1, 123, 90)])
    assert engine._active_positions('paper') == []
