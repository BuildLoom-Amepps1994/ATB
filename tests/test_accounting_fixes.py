"""Regression tests for the accounting defects confirmed by the 2026-11-04 audit.

* Finding 13 (hardening review, open since 2026-10-26): a MEXC buy whose
  trade fees cannot be read was booked at the gross quantity with a quote
  estimate, so the ledger tracked units the account never received and the
  exit left a phantom remainder. New buys are booked net of a base-asset
  deduction; a live exit writes off a remainder below the exchange minimum.
* Finding 7: a manual buy that merged a retained remainder anchored the
  merged stop, target and high on the blended average, which could sit
  above the current bid and sold the whole position at the next poll.

All exchanges are fakes; no network, no credentials.
"""
import time

import ccxt
import pytest

import storage
import trader
from test_engine import arm_fake_live, engine  # noqa: F401  (fixture)


def events_of(kind, log):
    return [fields for name, fields in log if name == kind]


@pytest.fixture
def events(monkeypatch):
    log = []
    monkeypatch.setattr(trader.observability, 'event', lambda kind, **fields: log.append((kind, fields)) or {})
    return log


# --------------------------------------------------------------- finding 13
def test_mexc_buy_with_unreadable_fees_is_tracked_net_of_a_base_deduction(engine, events):
    client = arm_fake_live(engine)
    client.has = {'fetchOrderTrades': True}
    client.fill.update(fee=None, fees=[])
    client.fetch_order_trades = lambda *args: (_ for _ in ()).throw(ccxt.NetworkError('fee read failed'))
    assert engine._buy('BTC/USDT', 800, 100, .04)
    position = engine._active_positions('live')[0]
    rate = engine.cfg['fee_pct'] / 100
    assert position['qty'] == pytest.approx(1 - rate)            # never more than the account received
    assert position['cost'] == pytest.approx(100)                # no quote debit was charged
    trade = storage.rows("SELECT * FROM trades WHERE side='BUY'")[0]
    assert trade['quantity'] == pytest.approx(1 - rate) and trade['fee'] == pytest.approx(rate * 100)
    assert events_of('execution.fee_estimated', events)[0]['base_fee'] == pytest.approx(rate)
    assert not engine.live_halted


def test_kraken_buy_with_unreadable_fees_keeps_the_quote_estimate(engine):
    engine.update_config({'exchange': 'kraken'})
    client = arm_fake_live(engine)
    client.fill.update(fee=None, fees=[])
    assert engine._buy('BTC/USDT', 800, 100, .04)
    position = engine._active_positions('live')[0]
    assert position['qty'] == pytest.approx(1)                   # Kraken bills the fee in USDT (fciq)
    assert position['cost'] == pytest.approx(100.1)


def test_mexc_sell_with_unreadable_fees_keeps_the_quote_estimate(engine):
    client = arm_fake_live(engine)
    assert engine._buy('BTC/USDT', 800, 100, .04)
    position = engine._active_positions('live')[0]
    client.fill = {'id': 'sell', 'status': 'closed', 'filled': 1., 'cost': 110., 'average': 110., 'fee': None}
    assert engine._sell(position, 110, 'test')
    trade = storage.rows("SELECT * FROM trades WHERE side='SELL'")[0]
    assert trade['fee'] == pytest.approx(0.11)                   # a sell's fee is charged in the quote asset
    assert trade['realized'] == pytest.approx(110 - 0.11 - 100.1)


def test_live_exit_writes_off_a_shortfall_below_the_exchange_minimum(engine, events):
    client = arm_fake_live(engine)
    assert engine._buy('BTC/USDT', 800, 100, .04)                # tracked 1 BTC at cost 100.1
    # The account holds 0.001 BTC less than tracked: 0.11 USDT, below the 10 USDT minimum, so it could never be sold.
    position = engine._active_positions('live')[0]
    client.fetch_balance = lambda *a: {'free': {'USDT': 10000., 'BTC': .999}}
    client.fill = {'id': 'sell', 'status': 'closed', 'filled': .999, 'cost': 109.89, 'average': 110.,
                   'fee': {'currency': 'USDT', 'cost': .10989}}
    assert engine._sell(position, 110, 'protective exit')
    assert engine._active_positions('live') == []                # no phantom remainder
    trade = storage.rows("SELECT * FROM trades WHERE side='SELL'")[0]
    assert trade['quantity'] == pytest.approx(.999)
    assert trade['realized'] == pytest.approx(109.89 - .10989 - 100.1)     # the whole basis is realised
    written = events_of('ledger.shortfall_written_off', events)
    assert written and written[0]['phantom'] == pytest.approx(.001)
    assert any('written off' in row['message'] for row in storage.rows('SELECT message FROM audit'))
    assert storage.unresolved_orders() == []


def test_live_partial_exit_with_a_shortfall_keeps_the_remainder_until_the_account_is_emptied(engine, events):
    client = arm_fake_live(engine)
    assert engine._buy('BTC/USDT', 800, 100, .04)
    position = engine._active_positions('live')[0]
    client.fetch_balance = lambda *a: {'free': {'USDT': 10000., 'BTC': .999}}
    client.fill = {'id': 'sell', 'status': 'canceled', 'filled': .5, 'cost': 55., 'average': 110.,
                   'fee': {'currency': 'USDT', 'cost': .055}}
    assert engine._sell(position, 110, 'partial')
    remainder = engine._active_positions('live')[0]
    assert remainder['qty'] == pytest.approx(.5)
    assert remainder['cost'] == pytest.approx(100.1 * .5)
    assert events_of('ledger.shortfall_written_off', events) == []


def test_a_larger_shortfall_stays_tracked_after_the_exit(engine, events):
    # 0.4 BTC short (40 USDT, above the minimum): units may sit in another order, so the ledger keeps them.
    client = arm_fake_live(engine)
    assert engine._buy('BTC/USDT', 800, 100, .04)
    position = engine._active_positions('live')[0]
    client.fetch_balance = lambda *a: {'free': {'USDT': 10000., 'BTC': .6}}
    client.fill = {'id': 'sell', 'status': 'closed', 'filled': .6, 'cost': 66., 'average': 110.,
                   'fee': {'currency': 'USDT', 'cost': .066}}
    assert engine._sell(position, 110, 'test')
    remainder = engine._active_positions('live')[0]
    assert remainder['qty'] == pytest.approx(.4)
    assert events_of('ledger.shortfall_written_off', events) == []
    assert any('below the tracked quantity' in row['message'] for row in storage.rows('SELECT message FROM audit'))


def test_paper_exits_never_write_off(engine, events):
    assert engine._buy('BTC/USDT', 800, 100, .04)
    position = engine._active_positions('paper')[0]
    assert engine._sell(position, 110, 'test')
    assert events_of('ledger.shortfall_written_off', events) == []


# ---------------------------------------------------------------- finding 7
def dust_row(engine, symbol='BTC/USDT', qty=.05, entry=300.0):
    with storage.connection() as db:
        db.execute('''INSERT INTO positions(exchange,mode,symbol,qty,entry_price,cost,highest,stop,target,opened_at)
                      VALUES (?,?,?,?,?,?,?,?,?,?)''',
                   (engine.exchange_name, 'paper', symbol, qty, entry, qty * entry, entry, entry * .96, entry * 1.11,
                    trader.now()))


def test_merged_dust_anchors_stop_target_and_high_on_the_new_fill(engine, events):
    dust_row(engine)                                             # 0.05 BTC from an entry at 300: 5 USDT, below the 10 USDT minimum
    engine._observe_ticker('BTC/USDT', {'bid': 99.99, 'ask': 100.01, 'last': 99.99, 'quoteVolume': 20000000,
                                        'timestamp': int(time.time() * 1000)})
    assert engine._buy('BTC/USDT', 200, 100.01, .04, merge_dust=True, kind='manual_entry', reason='manual')
    merged = engine._active_positions('paper')[0]
    fill = merged['entry_price']
    assert fill == pytest.approx(100.01 * (1 + engine.cfg['slippage_pct'] / 100))
    assert merged['highest'] == pytest.approx(fill)
    assert merged['stop'] == pytest.approx(fill * .96)
    assert merged['target'] == pytest.approx(fill * engine._target_multiple(.04))
    assert merged['cost'] == pytest.approx(215)                   # the blended basis is kept for P/L
    assert merged['qty'] == pytest.approx(.05 + 200 / (fill * (1 + engine.cfg['fee_pct'] / 100)))
    assert merged['stop'] < 99.99
    # The next protective poll at the same bid leaves the merged position alone.
    engine.running = True
    engine._protect_positions('BTC/USDT', 99.99)
    assert len(engine._active_positions('paper')) == 1
    assert events_of('decision.exit', events) == []
    assert any('anchored on the new fill' in row['message'] for row in storage.rows('SELECT message FROM audit'))


def test_merged_position_realises_the_blended_basis_on_exit(engine):
    dust_row(engine)
    engine._observe_ticker('BTC/USDT', {'bid': 99.99, 'ask': 100.01, 'last': 99.99, 'quoteVolume': 20000000,
                                        'timestamp': int(time.time() * 1000)})
    assert engine._buy('BTC/USDT', 200, 100.01, .04, merge_dust=True, kind='manual_entry', reason='manual')
    merged = engine._active_positions('paper')[0]
    assert engine._sell(merged, 120, 'test')
    trade = storage.rows("SELECT * FROM trades WHERE side='SELL'")[0]
    proceeds = merged['qty'] * 120 * (1 - engine.cfg['slippage_pct'] / 100) * (1 - engine.cfg['fee_pct'] / 100)
    assert trade['realized'] == pytest.approx(proceeds - 215, rel=1e-6)
