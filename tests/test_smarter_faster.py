"""Entry filters, candidate ranking, candle-close scans and overlapping public reads."""
import contextlib
import json
import math
import threading
import time

import ccxt
import pandas as pd
import pytest

import storage
import strategy
import trader
from market_data import MarketData
from test_engine import engine  # noqa: F401  (fixture)
from test_strategy_integrity import CONFIG, HOUR, START, history


def breakout_row(**changes):
    row = dict(close=100., volume=1000, ema20=99., ema50=98., rsi=60., atr=1.,
               vol_avg20=1000, prior_high20=100., efficiency=.5)
    row.update(changes)
    return pd.Series(row)


# --- Trend efficiency and entry filters -------------------------------------

def test_efficiency_is_one_for_a_straight_trend_and_zero_for_a_zigzag():
    rows = history(130)
    for i, row in enumerate(rows):
        price = 100 + i
        row[1:5] = [price, price + 1, price - 1, price]
    straight = strategy.indicators(strategy.frame_from_ohlcv(rows, drop_open_candle=False))
    assert straight.iloc[-1]['efficiency'] == pytest.approx(1.0)
    for i, row in enumerate(rows):
        price = 100 + (i % 2)
        row[1:5] = [price, price + 1, price - 1, price]
    zigzag = strategy.indicators(strategy.frame_from_ohlcv(rows, drop_open_candle=False))
    assert zigzag.iloc[-1]['efficiency'] == 0
    assert math.isnan(zigzag.iloc[strategy.EFFICIENCY_PERIOD - 1]['efficiency'])
    assert zigzag.iloc[strategy.EFFICIENCY_PERIOD]['efficiency'] == 0


def test_flat_prices_count_as_chop_not_as_missing_data():
    frame = strategy.indicators(strategy.frame_from_ohlcv(history(130), drop_open_candle=False))
    assert frame.iloc[-1]['efficiency'] == 0


def test_choppy_breakout_is_skipped_unless_filters_are_disabled():
    assert strategy.entry_signal(breakout_row())[0]
    ok, reason, _ = strategy.entry_signal(breakout_row(efficiency=.29))
    assert not ok and 'choppy market' in reason
    assert strategy.entry_signal(breakout_row(efficiency=.29), filters=False)[0]
    # Without filters the original six checks need no efficiency column.
    legacy = breakout_row()
    del legacy['efficiency']
    assert strategy.entry_signal(legacy, filters=False)[0]
    assert strategy.entry_signal(legacy) == (False, 'Insufficient indicators', 0)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), None, 'x'])
def test_unknown_efficiency_fails_closed(value):
    assert not strategy.entry_signal(breakout_row(efficiency=value))[0]


def test_btc_market_gate_only_applies_when_supplied():
    assert strategy.entry_signal(breakout_row(), market_ok=None)[0]
    assert strategy.entry_signal(breakout_row(), market_ok=True)[0]
    ok, reason, score = strategy.entry_signal(breakout_row(), market_ok=False)
    assert not ok and reason == 'Waiting: BTC market trend' and score == 87


def test_setup_quality_prefers_cleaner_trends_and_caps_volume_spikes():
    clean = strategy.setup_quality(breakout_row(efficiency=.8, volume=1500))
    noisy = strategy.setup_quality(breakout_row(efficiency=.35, volume=1500))
    spike = strategy.setup_quality(breakout_row(efficiency=.35, volume=50_000))
    assert clean > noisy and spike == pytest.approx(.35 * 3)
    assert strategy.setup_quality(breakout_row(efficiency=float('nan'))) == 0
    assert strategy.setup_quality(breakout_row(vol_avg20=0)) == 0


# --- Research replays the same gates ----------------------------------------

def recording_signals(monkeypatch):
    calls = []
    def fake(row, regime_ok=True, **kwargs):
        calls.append(kwargs)
        return row['timestamp'] == START + 115 * HOUR and regime_ok and kwargs.get('market_ok', True), 'x', 100

    def fixed_indicators(frame, **kwargs):
        frame = frame.copy()
        for name, value in {'ema20': 90, 'ema50': 80, 'ema100': 70, 'rsi': 60, 'atr': 1.,
                            'vol_avg20': 1000, 'prior_high20': 100, 'efficiency': .5}.items():
            frame[name] = value
        return frame
    monkeypatch.setattr(strategy, 'indicators', fixed_indicators)
    monkeypatch.setattr(strategy, 'entry_signal', fake)
    return calls


def higher(count=80):
    return history(count, duration=4 * HOUR, start=START - 70 * 4 * HOUR)


def test_backtest_replays_btc_gate_from_completed_btc_candles(monkeypatch):
    calls = recording_signals(monkeypatch)
    monkeypatch.setattr(strategy, '_historical_regimes',
                        lambda primary, frame, *a: [frame is not None and len(frame) != 79] * len(primary))
    kwargs = dict(timeframe='1h', higher_rows=higher(), higher_timeframe='4h', now_ms=START + 200 * HOUR)
    gated = strategy.backtest(history(200), CONFIG, market_rows=higher(79), **kwargs)
    assert gated['closed_trades'] == 0 and not gated['executions']
    assert calls and all(call['market_ok'] is False for call in calls)
    assert gated['assumptions']['entry_filters'] == {'trend_efficiency_min': .3,
                                                     'btc_market_gate': 'exact_completed_higher_timeframe'}
    calls.clear()
    off = strategy.backtest(history(200), {**CONFIG, 'market_filters': False}, market_rows=higher(79), **kwargs)
    assert all(call == {'filters': False} for call in calls)
    assert off['executions'] and off['assumptions']['entry_filters']['btc_market_gate'] == 'disabled'


def test_backtest_without_btc_rows_reports_the_gate_as_not_modeled():
    result = strategy.backtest(history(200), CONFIG)
    assert result['assumptions']['entry_filters'] == {'trend_efficiency_min': .3, 'btc_market_gate': 'not_modeled'}
    with pytest.raises(ValueError, match='confirmation timeframe'):
        strategy.backtest(history(200), CONFIG, market_rows=higher())
    with pytest.raises(ValueError, match='market_filters'):
        strategy.backtest(history(200), {**CONFIG, 'market_filters': 'yes'})


def test_research_fingerprints_the_btc_candles(monkeypatch):
    recording_signals(monkeypatch)
    report = strategy.research_report(history(400), CONFIG, timeframe='1h', higher_rows=higher(150),
                                      higher_timeframe='4h', now_ms=START + 400 * HOUR, market_rows=higher(150))
    prints = report['research_report']['data_fingerprints']
    assert prints['market_sha256'] == prints['higher_sha256']
    assert report['research_report']['parameters']['entry_filters']['btc_market_gate'] == 'exact_completed_higher_timeframe'


# --- Settings ------------------------------------------------------------------

def test_market_filters_default_on_and_must_be_boolean():
    assert storage.validate_settings({})['market_filters'] is True
    assert storage.validate_settings({'market_filters': False})['market_filters'] is False
    with pytest.raises(storage.ConfigValidationError):
        storage.validate_settings({'market_filters': 'false'})


def test_profile_saved_before_market_filters_needs_no_review(engine):
    saved = json.loads(storage.CONFIG_PATH.read_text())
    saved.pop('market_filters')
    storage.CONFIG_PATH.write_text(json.dumps(saved))
    assert storage.settings_health()['review_required'] is False
    assert storage.settings()['market_filters'] is True
    saved.pop('risk_pct')
    storage.CONFIG_PATH.write_text(json.dumps(saved))
    assert storage.settings_health()['review_required'] is True


# --- Live scan wiring ------------------------------------------------------------

def tag_frames(engine, monkeypatch, downloads):
    original = engine.data.candles
    def candles(symbol, timeframe, limit=175, **kwargs):
        downloads.append((symbol, timeframe, limit))
        frame = original(symbol, timeframe, limit, **kwargs)
        frame.attrs['symbol'] = symbol
        return frame
    monkeypatch.setattr(engine.data, 'candles', candles)


def test_alt_entries_wait_for_btc_uptrend_and_btc_is_read_once(engine, monkeypatch):
    downloads, seen = [], []
    tag_frames(engine, monkeypatch, downloads)
    monkeypatch.setattr(trader, 'regime_signal', lambda frame: frame.attrs.get('symbol') != 'BTC/USDT')
    monkeypatch.setattr(trader, 'entry_signal', lambda last, **kw: seen.append(kw) or (False, 'x', 0))
    engine.scan_once()
    by_market = [kw['market_ok'] for kw in seen]
    assert by_market[0] is None  # BTC/USDT itself relies on its own regime.
    assert by_market[1:] == [False] * 7
    assert downloads.count(('BTC/USDT', '4h', 85)) == 2  # its own row, then the gate
    # Catalogue, eight quotes and one download per frame: BTC's 4h is reused.
    assert engine.data.metrics['requests'] == 1 + 8 + 16
    rows = {row['symbol']: row for row in engine.market_rows}
    assert rows['ETH/USDT']['market'] == 'weak' and rows['ETH/USDT']['trend_efficiency'] == 0


def test_missing_btc_data_blocks_alt_entries_but_not_exits(engine, monkeypatch):
    seen = []
    original = engine.data.candles
    def candles(symbol, timeframe, limit=175, **kwargs):
        if symbol == 'BTC/USDT':
            raise ccxt.NetworkError('btc unavailable')
        return original(symbol, timeframe, limit, **kwargs)
    monkeypatch.setattr(engine.data, 'candles', candles)
    monkeypatch.setattr(trader, 'entry_signal', lambda last, **kw: seen.append(kw) or (False, 'x', 0))
    engine._buy('ETH/USDT', 800, 100, .04)
    engine.public.price, engine.running = 90, True
    engine.scan_once(allow_orders=True)
    assert all(kw['market_ok'] is False for kw in seen) and len(seen) == 7
    assert engine._active_positions('paper') == []  # The ETH stop still fired.
    assert {row['symbol']: row for row in engine.market_rows}['DOGE/USDT']['market'] == 'unavailable'


def test_disabled_filters_skip_the_btc_read(engine, monkeypatch):
    downloads, seen = [], []
    tag_frames(engine, monkeypatch, downloads)
    monkeypatch.setattr(trader, 'entry_signal', lambda last, **kw: seen.append(kw) or (False, 'x', 0))
    engine.cfg['market_filters'] = False
    engine.cfg['watchlist'] = ['ETH/USDT']
    engine.scan_once()
    assert seen == [{'regime_ok': False, 'rapid': False, 'market_ok': None, 'filters': False}]
    assert ('BTC/USDT', '4h', 85) not in downloads


def test_candidates_rank_by_setup_quality_before_turnover(engine, monkeypatch):
    quality = {'ETH/USDT': .9, 'SOL/USDT': .4}
    monkeypatch.setattr(trader, 'entry_signal', lambda *a, **kw: (True, 'forced', 100))
    monkeypatch.setattr(trader, 'entry_price_ok', lambda *a, **kw: True)
    original = engine.data.candles
    def named(symbol, timeframe, limit=175, **kwargs):
        # Label each row with its symbol so the quality stub can score it.
        frame = original(symbol, timeframe, limit, **kwargs)
        frame.index = [symbol] * len(frame)
        return frame
    monkeypatch.setattr(engine.data, 'candles', named)
    monkeypatch.setattr(trader, 'setup_quality', lambda last: quality.get(last.name, 0))
    seen = []
    monkeypatch.setattr(engine, '_enter_candidates', lambda rows: seen.extend(rows))
    engine.running = True
    engine.cfg['watchlist'] = ['SOL/USDT', 'ETH/USDT']
    engine.scan_once(allow_orders=True)
    assert [row[2] for row in seen] == ['ETH/USDT', 'SOL/USDT']
    assert [row[0] for row in seen] == [.9, .4]


def test_next_scan_lands_just_after_the_primary_candle_close(engine, monkeypatch):
    now = [1_700_000_000.0 + 3600 - 100]  # 100 s before an hourly close
    now[0] -= now[0] % 3600 - 3500
    monkeypatch.setattr(trader.time, 'time', lambda: now[0])
    assert engine._seconds_to_next_close() == pytest.approx(100 + engine.CLOSE_GRACE_SECONDS)
    engine.cfg['time_horizon'] = 'rapid'
    assert engine._seconds_to_next_close() == pytest.approx(60 - now[0] % 60 + engine.CLOSE_GRACE_SECONDS)
    engine.public.options = {'timeDifference': 2000}  # PC clock 2 s ahead of the exchange
    engine.cfg['time_horizon'] = 'balanced'
    assert engine._seconds_to_next_close() == pytest.approx(102 + engine.CLOSE_GRACE_SECONDS)


def test_worker_scans_at_the_close_when_it_precedes_the_interval(engine, monkeypatch):
    clock, scans = [100.0], []
    class Timer:
        def set(self):
            clock[0] = 1000
        def is_set(self):
            return clock[0] >= 400
        def wait(self, delay):
            clock[0] += delay
            return self.is_set()
    monkeypatch.setattr(trader.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(engine, '_monitor_if_due', lambda **kw: set())
    monkeypatch.setattr(engine, 'scan_once', lambda **kw: scans.append(round(clock[0], 6)))
    monkeypatch.setattr(engine, '_seconds_to_next_close', lambda: 42.0)
    engine.stop_event, engine.running = Timer(), True
    engine.cfg['scan_seconds'] = 180
    engine._loop()
    assert scans[:3] == [100, 142, 184]


def test_unchanged_trailing_stop_is_not_rewritten(engine, monkeypatch):
    engine._buy('BTC/USDT', 800, 100, .04)
    position = engine._active_positions('paper')[0]
    writes = []
    original = storage.connection
    @contextlib.contextmanager
    def counting():
        with original() as db:
            class Recorder:
                def execute(self, sql, *args):
                    if sql.lstrip().startswith('UPDATE'):
                        writes.append(sql)
                    return db.execute(sql, *args)
            yield Recorder()
    monkeypatch.setattr(storage, 'connection', counting)
    engine._mark_positions('BTC/USDT', position['highest'] * .99, 0.0, False)
    assert writes == []
    engine._mark_positions('BTC/USDT', position['highest'] * 1.01, 1.0, False)
    assert writes and engine._active_positions('paper')[0]['highest'] > position['highest']


# --- Overlapping public reads ------------------------------------------------------

class SlowExchange:
    has = {'fetchTickers': True}
    rateLimit = 50

    def __init__(self, delay=.05):
        self.delay, self.lock = delay, threading.Lock()
        self.active = self.peak = self.calls = 0
        self.failing = set()

    def fetch_ohlcv(self, symbol, timeframe, limit):
        with self.lock:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
            if symbol in self.failing:
                raise ccxt.BadSymbol('gone')
            end = int(time.time() // 3600) * 3600
            return [[(end - (limit - i) * 3600) * 1000, 100, 101, 99, 100, 1000] for i in range(limit)]
        finally:
            with self.lock:
                self.active -= 1


def wait_for(workers):
    for worker in workers:
        worker.join(5)


def test_prefetch_overlaps_downloads_and_the_scan_reuses_them():
    exchange = SlowExchange()
    data = MarketData(exchange, concurrency=4)
    wanted = [(f'C{i}/USDT', '1h', 175) for i in range(6)]
    started = time.perf_counter()
    wait_for(data.prefetch(wanted + wanted[:2]))
    elapsed = time.perf_counter() - started
    assert exchange.peak == 3 and exchange.calls == 6 and elapsed < 6 * exchange.delay
    for symbol, timeframe, limit in wanted:
        data.candles(symbol, timeframe, limit)
    assert exchange.calls == 6 and data.metrics['candle_cache_hits'] == 6


def test_serialized_client_never_prefetches():
    exchange = SlowExchange()
    data = MarketData(exchange)
    assert data.prefetch([('A/USDT', '1h', 175)]) == []
    assert exchange.calls == 0


def test_foreground_read_waits_for_the_same_download_in_flight():
    exchange = SlowExchange(delay=.15)
    data = MarketData(exchange, concurrency=4)
    workers = data.prefetch([('A/USDT', '1h', 175)])
    time.sleep(.03)
    frame = data.candles('A/USDT', '1h', 175)
    wait_for(workers)
    assert exchange.calls == 1 and len(frame) == 175


def test_background_failure_is_reported_once_without_a_second_request():
    exchange = SlowExchange()
    exchange.failing.add('GONE/USDT')
    data = MarketData(exchange, concurrency=4, wait=lambda _: False)
    wait_for(data.prefetch([('GONE/USDT', '1h', 175)]))
    with pytest.raises(ccxt.BadSymbol):
        data.candles('GONE/USDT', '1h', 175)
    assert exchange.calls == 1
    with pytest.raises(ccxt.BadSymbol):
        data.candles('GONE/USDT', '1h', 175)  # The next scan asks the exchange again.
    assert exchange.calls == 2


def test_exchange_rate_limits_choose_concurrency():
    assert trader.TradingEngine._public_concurrency(ccxt.mexc()) == 4
    assert trader.TradingEngine._public_concurrency(ccxt.kraken()) == 1
    assert trader.TradingEngine._public_concurrency(object()) == 1


def real_ccxt_client(delay):
    """A real CCXT MEXC client whose HTTP layer is replaced; nothing leaves this process."""
    client = ccxt.mexc({'enableRateLimit': True})
    starts, lock = [], threading.Lock()
    def fetch(url, method='GET', headers=None, body=None):
        with lock:
            starts.append((time.monotonic(), url))
        time.sleep(delay)
        return {}
    client.fetch = fetch
    return client, starts


def test_real_ccxt_requests_stay_paced_while_overlapping():
    client, starts = real_ccxt_client(.2)
    MarketData(client, concurrency=4)
    threads = [threading.Thread(target=client.spotPublicGetPing) for _ in range(4)]
    began = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    times = sorted(start for start, _ in starts)
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert len(times) == 4 and min(gaps) >= client.rateLimit / 1000 * .9
    # Four 200 ms round trips overlapped instead of taking 800 ms in a row.
    assert time.monotonic() - began < .6


def test_quote_reads_go_ahead_of_queued_candle_requests():
    client, starts = real_ccxt_client(.05)
    client.rateLimit = 150
    data = MarketData(client, concurrency=4)
    def bulk():
        data._priority.urgent = False
        client.spotPublicGetPing()
    def urgent():
        data._priority.urgent = True
        client.spotPublicGetTime()
    threads = [threading.Thread(target=bulk) for _ in range(3)]
    for thread in threads:
        thread.start()
        time.sleep(.01)
    quote = threading.Thread(target=urgent)
    quote.start()
    for thread in threads + [quote]:
        thread.join(5)
    order = [url.rsplit('/', 1)[1] for _, url in sorted(starts)]
    # The second request was already pacing; the quote overtakes the third.
    assert order == ['ping', 'ping', 'time', 'ping']


def test_a_paper_entry_is_not_sold_by_the_trail_on_an_unchanged_market(engine):
    # Paper fills add the configured slippage above the ask. The trail must not
    # start from that price while the market has not moved (fast timeframes have
    # an ATR smaller than the slippage).
    engine.cfg['slippage_pct'] = .2
    assert engine._buy('BTC/USDT', 800, 100, .008)
    position = engine._active_positions('paper')[0]
    assert position['entry_price'] > 100
    engine.running = True
    engine._mark_positions('BTC/USDT', 99.99, .03, False)
    assert len(engine._active_positions('paper')) == 1
    assert engine._active_positions('paper')[0]['stop'] == position['stop']
