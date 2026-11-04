"""Regression tests for the defects confirmed by the 2026-11-04 audit (risk and ledger lenses).

* The runaway exit-rate bound was shared by every pair, so one protective
  exit that the exchange kept rejecting every poll used up the allowance and
  every other position's protective exit was refused.
* A verified dust remainder withheld its pair from automated entries for
  good; only a manual buy could consolidate it.
* The live state snapshot read the paper cash ledger unconditionally, so a
  corrupt paper row took GET /api/state offline in live mode.
* A paper reset kept the consecutive-loss record.

All exchanges are fakes; no network, no credentials.
"""

import time

import ccxt
import pytest

import risk
import storage
import trader
from market_data import MarketData
from test_engine import arm_fake_live, engine  # noqa: F401  (fixture)
from test_risk_governance import candidate, losing_round_trip, observe


def capture(monkeypatch):
    log = []
    monkeypatch.setattr(trader.observability, 'event', lambda kind, **fields: log.append((kind, fields)) or {})
    return log


# ------------------------------------------------------------- exit rate
def test_one_pairs_rejected_exits_do_not_starve_another_pairs_exit(engine):
    assert engine._buy('BTC/USDT', 200, 100, .04)
    observe(engine, 'BTC/USDT')
    assert engine._buy('ETH/USDT', 200, 100, .04)
    engine.cfg['max_orders_per_minute'] = 1                        # exit allowance: 3 per minute per pair
    for _ in range(3):                                             # BTC's exit was refused three times this minute
        intent = storage.create_order_intent(engine.exchange_name, 'BTC/USDT', 'SELL', quantity=1., mode='paper')
        storage.update_order_intent(intent, 'rejected')
    positions = engine._active_positions('paper')
    blocked = engine._authorize('BTC/USDT', 'SELL', 'exit', 100., 100., positions=positions, cash=None, quantity=1.)
    assert not blocked.allowed and blocked.failed[0].startswith('Runaway order rate for BTC/USDT')
    allowed = engine._authorize('ETH/USDT', 'SELL', 'exit', 100., 100., positions=positions, cash=None, quantity=1.)
    assert allowed.allowed
    checks = {c['check']: c for c in allowed.checks}
    assert checks['exit_order_rate']['recent'] == 1                   # ETH's own buy a moment ago


def test_the_pairs_own_runaway_rate_is_still_bounded(engine):
    assert engine._buy('BTC/USDT', 200, 100, .04)
    engine.cfg['max_orders_per_minute'] = 1
    positions = engine._active_positions('paper')
    for _ in range(risk.EXIT_RATE_MULTIPLE):
        intent = storage.create_order_intent(engine.exchange_name, 'BTC/USDT', 'SELL', quantity=1., mode='paper')
        storage.update_order_intent(intent, 'rejected')
    assert not engine._sell(positions[0], 90, 'test')
    assert engine.risk.last_block['reasons'][0].startswith('Runaway order rate')


# ------------------------------------------------------- dust consolidation
def dust_row(engine, symbol='BTC/USDT', qty=.05, entry=300.0):
    with storage.connection() as db:
        db.execute('''INSERT INTO positions(exchange,mode,symbol,qty,entry_price,cost,highest,stop,target,opened_at)
                      VALUES (?,?,?,?,?,?,?,?,?,?)''',
                   (engine.exchange_name, 'paper', symbol, qty, entry, qty * entry, entry, entry * .96, entry * 1.11,
                    trader.now()))


def test_a_scan_entry_consolidates_a_verified_dust_remainder(engine, monkeypatch):
    events = capture(monkeypatch)
    dust_row(engine)                                               # 0.05 BTC: 5 USDT at the current price, below the 10 USDT minimum
    observe(engine, 'BTC/USDT')
    engine.running = True
    engine._enter_candidates([candidate('BTC/USDT')])
    merged = engine._active_positions('paper')
    assert len(merged) == 1 and merged[0]['qty'] > .05
    assert merged[0]['highest'] == pytest.approx(merged[0]['entry_price'])
    assert merged[0]['stop'] < 100 < merged[0]['target']
    trade = storage.rows("SELECT gross, fee FROM trades WHERE side='BUY'")[0]
    assert merged[0]['cost'] == pytest.approx(15 + trade['gross'] + trade['fee'], rel=1e-6)   # blended basis
    assert [f for kind, f in events if kind == 'decision.withheld'] == []
    assert any('anchored on the new fill' in row['message'] for row in storage.rows('SELECT message FROM audit'))


def test_a_scan_entry_still_withholds_a_tracked_position_above_dust(engine, monkeypatch):
    events = capture(monkeypatch)
    assert engine._buy('BTC/USDT', 200, 100, .04)
    observe(engine, 'BTC/USDT')
    engine.running = True
    engine._enter_candidates([candidate('BTC/USDT')])
    assert len(engine._active_positions('paper')) == 1
    assert [f['reason'] for kind, f in events if kind == 'decision.withheld'] == ['held']


# ---------------------------------------------------------------- snapshot
def test_live_snapshot_survives_a_corrupt_paper_ledger(engine):
    arm_fake_live(engine)
    storage.set_meta(engine._key('paper_cash'), 'not a number')
    state = engine.snapshot()
    assert state['mode'] == 'live' and state['paper_cash'] is None


def test_paper_snapshot_still_reports_a_corrupt_ledger(engine):
    storage.set_meta(engine._key('paper_cash'), 'nan')
    with pytest.raises(ValueError):
        engine.snapshot()


# ------------------------------------------------------------- paper reset
def test_paper_reset_clears_the_consecutive_loss_record(engine):
    for _ in range(4):
        losing_round_trip(engine)
    assert engine.risk.loss_streak_stop('paper')
    engine.reset_paper_portfolio()
    assert engine.risk.loss_streak('paper') == {'date': risk._today(), 'count': 0, 'latched_at': None}
    assert engine.risk.loss_streak_stop('paper') is None
    assert engine._buy('BTC/USDT', 200, 100, .04, volume=20000000)


# ------------------------------------------------------- trend trailing
def test_the_monitor_does_not_trail_trend_positions(engine):
    engine.update_config({'time_horizon': 'trend'})
    assert engine._buy('BTC/USDT', 200, 100, .25)                  # Trend's 25 % disaster stop
    engine.data.candles('BTC/USDT', '1d', limit=175)               # the monitor trails from cached candles (ATR 2)
    engine.running = True
    stop = engine._active_positions('paper')[0]['stop']            # the 25 % disaster stop under the fill
    engine.public.price = 110                                      # five ATRs above the fill: a trail would arm
    engine._monitor_if_due(force=True)
    position = engine._active_positions('paper')[0]
    assert position['stop'] == pytest.approx(stop) and stop < 76   # not raised to the bid - 2.5 x ATR
    engine.public.price = 104                                      # a pullback of three ATRs from the high
    engine._monitor_if_due(force=True)
    assert len(engine._active_positions('paper')) == 1             # held: the Trend exit is the close below the average


def test_the_monitor_still_trails_the_other_horizons(engine):
    assert engine._buy('BTC/USDT', 200, 100, .04)
    engine.data.candles('BTC/USDT', '1h', limit=175)
    engine.running = True
    engine.public.price = 110
    engine._monitor_if_due(force=True)
    assert engine._active_positions('paper')[0]['stop'] == pytest.approx(110 * .9999 - 5, abs=.01)   # bid - 2.5 x ATR
    engine.public.price = 104
    engine._monitor_if_due(force=True)
    assert engine._active_positions('paper') == []


# ------------------------------------------------------- exchange clock
class TimedExchange:
    """A public client whose clock runs 120 s behind the PC, with a server-time endpoint."""
    has = {'fetchTime': True}
    options = {}

    def __init__(self, clock, behind=120.0):
        self.clock, self.behind, self.calls, self.fail = clock, behind, 0, False

    def fetch_time(self):
        self.calls += 1
        if self.fail:
            raise ccxt.NetworkError('time endpoint down')
        return int((self.clock[0] - self.behind) * 1000)

    def fetch_ohlcv(self, symbol, tf, limit):
        return [[hour * 3600 * 1000, 100, 101, 99, 100, 1000] for hour in range(1, 12)]


def test_clock_offset_is_measured_from_the_server_time_and_refreshed_every_fifteen_minutes():
    clock, mono = [12 * 3600.0], [1000.0]
    exchange = TimedExchange(clock)
    data = MarketData(exchange, clock=lambda: clock[0], monotonic=lambda: mono[0], wait=lambda _: False)
    assert data.clock_offset() == 0.0                              # nothing measured yet, no CCXT figure
    assert data.refresh_clock_offset() == pytest.approx(120)
    assert data.clock_offset() == pytest.approx(120)
    exchange.behind = 30
    assert data.refresh_clock_offset() == pytest.approx(120) and exchange.calls == 1      # inside the window
    mono[0] += MarketData.OFFSET_REFRESH_SECONDS + 1
    assert data.refresh_clock_offset() == pytest.approx(30) and exchange.calls == 2
    exchange.fail = True
    mono[0] += MarketData.OFFSET_REFRESH_SECONDS + 1
    assert data.refresh_clock_offset() == pytest.approx(30)        # a failed read keeps the last measurement
    assert data.metrics['clock_offset_failures'] == 1
    mono[0] += 1
    data.refresh_clock_offset()
    assert exchange.calls == 3                                     # and is not retried before the next window


def test_clock_offset_falls_back_to_ccxts_figure_without_a_server_time_endpoint():
    class Plain:
        has = {'fetchTime': False}
        options = {'timeDifference': 45_000}
        def fetch_time(self):
            raise AssertionError('must not be called')
    data = MarketData(Plain(), wait=lambda _: False)
    assert data.refresh_clock_offset() is None
    assert data.clock_offset() == pytest.approx(45)
    class Bare:
        has = {}
    assert MarketData(Bare(), wait=lambda _: False).clock_offset() == 0.0


def test_candle_completion_is_judged_on_exchange_time():
    # 60 s after the 11:00 boundary on the PC clock, but the exchange is still at 10:59: its 10:00 candle is open.
    clock = [11 * 3600 + 60.0]
    data = MarketData(TimedExchange(clock), clock=lambda: clock[0], wait=lambda _: False)
    frame = data.candles('BTC/USDT', '1h', limit=11)
    assert int(frame.iloc[-1]['timestamp']) == 9 * 3600 * 1000
    assert data.metrics['clock_offset_measurements'] == 1


def test_engine_scan_timing_and_exit_marker_use_the_measured_offset(engine):
    engine.data._clock_measured = 120.0
    assert engine._clock_offset() == pytest.approx(120)
    duration = trader.TIMEFRAME_SECONDS[trader.HORIZONS[engine.cfg['time_horizon']][0]]
    expected = duration - (time.time() - 120) % duration + engine.CLOSE_GRACE_SECONDS
    assert engine._seconds_to_next_close() == pytest.approx(expected, abs=1)
    assert engine._buy('BTC/USDT', 200, 100, .04)
    position = engine._active_positions('paper')[0]
    assert engine._sell(position, 110, 'test')
    primary_tf = trader.HORIZONS[engine.cfg['time_horizon']][0]
    saved = storage.get_meta(engine._key(f'last_exit.paper.BTC/USDT.{primary_tf}'))
    step = duration * 1000
    assert int(float(saved)) == (int((time.time() - 120) * 1000) // step - 1) * step


def test_a_rejected_exchange_timestamp_remeasures_the_clock_offset(engine):
    # The PC clock was corrected forward by two minutes; every exchange-stamped quote now looks stale.
    engine.cfg['data_max_age_seconds'] = 30
    engine.public.has = {'fetchTime': True}
    engine.public.fetch_time = lambda: int((time.time() - 120) * 1000)
    stamp = int((time.time() - 120) * 1000)
    with pytest.raises(ValueError, match='stale'):
        engine._observe_ticker('BTC/USDT', {'bid': 100, 'ask': 100.1, 'timestamp': stamp})
    assert engine.data.clock_offset() == pytest.approx(120, abs=1)    # re-measured on the rejection
    price, _, _ = engine._observe_ticker('BTC/USDT', {'bid': 100, 'ask': 100.1, 'timestamp': stamp})
    assert price == 100 and engine._fresh('BTC/USDT')
    calls = engine.data.metrics['clock_offset_measurements']
    with pytest.raises(ValueError, match='stale'):
        engine._observe_ticker('BTC/USDT', {'bid': 100, 'ask': 100.1, 'timestamp': stamp - 3_600_000})
    assert engine.data.metrics['clock_offset_measurements'] == calls   # at most one forced measurement a minute


# ------------------------------------------------------- monitor errors
def test_an_exit_failure_in_the_protective_tick_is_reported_as_such(engine, monkeypatch):
    events = capture(monkeypatch)
    assert engine._buy('BTC/USDT', 200, 100, .04)
    engine.running = True
    monkeypatch.setattr(engine, '_protect_positions', lambda *a, **k: (_ for _ in ()).throw(ValueError('adapter refused')))
    engine.public.price = 90
    engine._monitor_if_due(force=True)
    assert engine.monitor_error.startswith('Protective exit failed for BTC/USDT: ValueError')
    assert engine.last_successful_monitor                          # the quote itself was fine
    assert any(f['symbol'] == 'BTC/USDT' for kind, f in events if kind == 'monitor.exit_error')
    assert any('Protective exit failed' in row['message'] for row in storage.rows("SELECT message FROM audit WHERE level='error'"))


def test_a_quote_failure_in_the_protective_tick_is_still_reported_as_quotes(engine):
    assert engine._buy('BTC/USDT', 200, 100, .04)
    engine.running = True
    engine.public.fetch_ticker = lambda symbol: (_ for _ in ()).throw(ccxt.NetworkError('down'))
    engine._monitor_if_due(force=True)
    assert engine.monitor_error == 'Protective quotes unavailable for 1 held asset(s)'


# ------------------------------------------------------- scan tickers
def test_the_bulk_ticker_read_only_asks_for_listed_pairs(engine):
    asked = []
    def fetch_tickers(symbols=None, params=None):
        asked.append(list(symbols or []))
        if any(s not in engine.public.markets for s in symbols):
            raise ccxt.BadSymbol('unknown symbol')
        return {s: engine.public.fetch_ticker(s) for s in symbols}
    engine.public.fetch_tickers = fetch_tickers
    engine.public.has = {'fetchTickers': True}
    engine.cfg['watchlist'] = ['BTC/USDT', 'ETH/USDT', 'XYZ/USDT']
    engine.scan_once(allow_orders=False)
    assert asked and all('XYZ/USDT' not in symbols for symbols in asked)
    assert 'BTC/USDT' in asked[0]


# ------------------------------------------------------- prefetch stop
def test_prefetch_stops_downloading_once_cancelled():
    calls = []
    class Exchange:
        has = {}
        options = {}
        def fetch_ohlcv(self, symbol, tf, limit):
            calls.append(symbol)
            return [[hour * 3600 * 1000, 100, 101, 99, 100, 1000] for hour in range(1, 130)]
    data = MarketData(Exchange(), wait=lambda _: False, concurrency=2)
    stop = []
    workers = data.prefetch([(f'S{i}/USDT', '1h', 175) for i in range(20)], cancelled=lambda: bool(stop))
    stop.append(True)
    for worker in workers:
        worker.join(5)
    assert len(calls) < 20


# ------------------------------------------------------- live exits share a balance read
def test_protective_exits_of_one_tick_share_a_single_balance_read(engine):
    client = arm_fake_live(engine)
    assert engine._buy('BTC/USDT', 800, 100, .04)
    observe(engine, 'BTC/USDT')
    assert engine._buy('ETH/USDT', 800, 100, .04)
    reads = []                                                     # orders sent at the time of each balance read
    original = client.fetch_balance
    client.fetch_balance = lambda *a: reads.append(len(client.orders)) or original()
    client.fill = {'id': 'sell', 'status': 'closed', 'filled': 1., 'cost': 90., 'average': 90., 'fee': {'currency': 'USDT', 'cost': .09}}
    engine.running = True
    engine.public.price = 90                                       # both stops breached
    engine._monitor_if_due(force=True)
    assert engine._active_positions('live') == []
    assert sum(1 for order in client.orders if order[1][2] == 'sell') == 2
    assert reads == [2, 4]      # after the two buys: one read before the first exit served both; the valuation read came after the fills


def test_a_booked_sell_lowers_the_cached_free_amount_of_its_own_asset(engine):
    client = arm_fake_live(engine)
    assert engine._buy('BTC/USDT', 800, 100, .04)
    position = engine._active_positions('live')[0]
    client.fetch_balance = lambda *a: {'free': {'USDT': 10000., 'BTC': 1.}}
    client.fill = {'id': 'sell', 'status': 'canceled', 'filled': .4, 'cost': 44., 'average': 110.,
                   'fee': {'currency': 'USDT', 'cost': .044}}
    assert engine._sell(position, 110, 'partial')
    cached, _ = engine._tick_balance
    assert cached['free']['BTC'] == pytest.approx(.6)
    remainder = engine._active_positions('live')[0]
    assert remainder['qty'] == pytest.approx(.6)
    client.fill = {'id': 'sell2', 'status': 'closed', 'filled': .6, 'cost': 66., 'average': 110.,
                   'fee': {'currency': 'USDT', 'cost': .066}}
    assert engine._sell(remainder, 110, 'rest')                    # the cached read still serves within the window
    assert client.orders[-1][1][3] == pytest.approx(.6)
    assert engine._active_positions('live') == []
