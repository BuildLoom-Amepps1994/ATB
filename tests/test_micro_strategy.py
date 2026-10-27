"""Micro-mode features, regime, entry candidates and exit policy.

Every fixture is synthetic: closes are generated here, turned into candles,
validated by ``strategy.frame_from_ohlcv`` and aggregated into 5m bars from
the same 1m bars, so the two frames share one timeline. Nothing touches the
network, the clock or storage; ``now`` is always explicit.
"""
import dataclasses
import json
import math
import sys
import types
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import micro_strategy as ms
import money
import strategy

D = Decimal
START_MS = 1_761_436_800_000          # a 5-minute boundary
NOW = START_MS / 1000 + 86_400.25     # used where no features fix the time (exits, missing features)
PARAMS = ms.MicroParams(target_net_bps=20, max_loss_bps=80, max_hold_s=1800, uncertainty_floor_bps=5,
                        max_spread_bps=10, max_vol_bps=60, min_efficiency_trend=0.3, max_efficiency_range=0.25,
                        edge_ratio=0.5)
GATES = dict(selected=True, capacity=True, cooldown_remaining_s=0, fresh_after_exit=True, review_block=False,
             risk_block=None, latched=None)
BOOK = dict(best_bid='99.99', best_ask='100.01', spread_bps='2', generation=7, sequence=123, age_s=0.4,
            source='fixture')
# Round-trip costs of a 25 USDT cycle at a 0.1% taker fee per leg, 3 bps latency
# per leg and a 20 bps net target: about 46 bps before the bid may be sold.
REALISTIC_REQUIRED_MOVE = '46'


# ------------------------------------------------------------------ fixtures
def candles(closes, *, wick_bps=2.0, volume=100.0):
    rows, previous = [], closes[0]
    for i, close in enumerate(closes):
        high = max(previous, close) * (1 + wick_bps / 1e4)
        low = min(previous, close) * (1 - wick_bps / 1e4)
        rows.append([START_MS + i * 60_000, previous, high, low, close, volume])
        previous = close
    return rows


def five_minute(rows):
    out = []
    for k in range(0, len(rows) - 4, 5):
        group = rows[k:k + 5]
        out.append([group[0][0], group[0][1], max(r[2] for r in group), min(r[3] for r in group), group[-1][4],
                    sum(r[5] for r in group)])
    return out


def frames(closes, **kwargs):
    rows = candles(closes, **kwargs)
    return (strategy.frame_from_ohlcv(rows, drop_open_candle=False),
            strategy.frame_from_ohlcv(five_minute(rows), drop_open_candle=False))


def closed_at(n):
    """A quarter second after the n-th 1m bar of the fixtures (``rows[:n]``) closed."""
    return START_MS / 1000 + n * 60 + 0.25


def fresh(features):
    """A quarter second after the features' decision bar closed: the time a live scan would decide."""
    return (features.bar_ts_ms + ms.BAR_MS_1M) / 1000 + 0.25


def path(steps_bps, start=100.0):
    return list(start * np.exp(np.cumsum(np.asarray(steps_bps, dtype=float) / 1e4)))


def uptrend(tail=(-7, -7, -7, -5, 5), bars=400):
    """A steady 4 bps/bar climb; the default tail is a shallow pullback and an up close."""
    return path([4.0] * (bars - len(tail)) + list(tail))


def ranging(seed=7, dip=(0.999, 0.998, 0.9975), bars=400):
    """Noise around 100 (not a random walk), ending in a short dip to a low z-score."""
    rng = np.random.default_rng(seed)
    closes = list(100 * (1 + 0.001 * rng.standard_normal(bars - len(dip))))
    anchor = closes[-1]
    return closes + [anchor * factor for factor in dip]


def sizing(required='15', **changes):
    """The shape of micro_economics.SizingResult.as_dict() for an eligible 25 USDT cycle.

    A low-cost venue: the break-even bid is 5 bps above the best bid, so the
    stop keeps room beyond the loss carried at entry. ``contract_sizing``
    below derives every figure from the contract's formulas instead.
    """
    record = dict(symbol='ETH/USDT', eligible=True, code=None, detail='eligible', checks=[{'check': 'budget'}],
                  budget_cap='25', target_budget='25', gross_quote='24.97', fee_reserve='0.03', max_debit='25.00',
                  expected_base='0.2496', expected_fee_base='0.0002496', sellable_base='0.2493',
                  predicted_residual_base='0.0000504', entry_vwap='100.03', break_even_bid='100.04',
                  target_bid='100.16', required_move_bps=required, minimum_feasible_budget='5.03',
                  minimum_feasible_detail='', cost={'taker_fee': '0.001'})
    record.update(changes)
    return record


def contract_sizing(*, target_bps, budget=D('25'), bid=D('99.99'), ask=D('100.01'), fee=D('0.001'),
                    latency_bps=D('3'), cost_step=D('0.01'), amount_step=D('0.0001'), tick=D('0.01')):
    """An eligible sizing record computed with the contract's sizing formulas (quote-billed fees, deep book)."""
    gross = money.q_down(money.div(budget, money.add(money.ONE, fee)), cost_step)
    reserve = money.q_up(money.mul(gross, fee), cost_step)
    while money.add(gross, reserve) > budget:
        gross = money.sub(gross, cost_step)
        reserve = money.q_up(money.mul(gross, fee), cost_step)
    max_debit = money.add(gross, reserve)
    latency = money.bps(latency_bps)
    base = money.div(gross, money.mul(ask, money.add(money.ONE, latency)))
    sellable = money.q_down(base, amount_step)
    keep = money.mul(sellable, money.sub(money.ONE, latency), money.sub(money.ONE, fee))
    target_net = money.mul(budget, money.bps(D(target_bps)))
    break_even = money.q_up(money.div(max_debit, keep), tick)
    target_bid = money.q_up(money.div(money.add(max_debit, target_net), keep), tick)
    required = money.mul(money.sub(money.div(target_bid, ask), money.ONE), money.BPS)
    record = sizing(required=money.text(required), budget_cap=money.text(budget), target_budget=money.text(budget),
                    gross_quote=money.text(gross), fee_reserve=money.text(reserve), max_debit=money.text(max_debit),
                    expected_base=money.text(base), expected_fee_base='0', sellable_base=money.text(sellable),
                    predicted_residual_base=money.text(money.sub(base, sellable)),
                    break_even_bid=money.text(break_even), target_bid=money.text(target_bid))
    # What an exit estimate says the moment the position is bought, on the same book.
    immediate = money.sub(money.mul(sellable, bid, money.sub(money.ONE, latency), money.sub(money.ONE, fee)),
                          max_debit)
    return record, immediate


def features_of(closes, symbol='ETH/USDT'):
    f1, f5 = frames(closes)
    features, reason = ms.compute_features(symbol, f1, f5)
    assert features is not None, reason
    return features


def decide(features, *, symbol='ETH/USDT', regime=None, spread_bps=None, depth_ok=True, data_fresh=True,
           params=PARAMS, gates=None, book=None, sizing_record=None, now=None, feature_reason=None):
    if now is None:
        now = fresh(features) if isinstance(features, ms.Features) else NOW
    book = dict(BOOK if book is None else book)
    if spread_bps is not None:
        book['spread_bps'] = spread_bps
    if regime is None:
        regime, reasons = ms.classify_regime(features, spread_bps=book.get('spread_bps'), depth_ok=depth_ok,
                                             data_fresh=data_fresh, params=params)
    else:
        reasons = ()
    return ms.evaluate_entry(symbol=symbol, features=features, regime=regime, regime_reasons=reasons,
                             sizing=sizing() if sizing_record is None else sizing_record, book_summary=book,
                             params=params, gates=dict(GATES) if gates is None else gates, now=now,
                             feature_reason=feature_reason)


def assert_abstains(decision, code):
    assert decision.action == 'abstain' and decision.proposal is None
    assert decision.code == code and code in decision.codes
    failed = [check for check in decision.checks if not check['passed']]
    assert decision.reasons == tuple(check['detail'] for check in failed)
    assert decision.codes == tuple(check['code'] for check in failed)
    assert all(reason and '\n' not in reason for reason in decision.reasons)
    assert decision.status.startswith(ms.STATUS_LABELS[code])


@pytest.fixture(scope='module')
def momentum():
    return features_of(uptrend())


@pytest.fixture(scope='module')
def ranging_features():
    return features_of(ranging())


# ------------------------------------------------- features: no look-ahead
def test_walk_forward_decisions_replay_exactly_from_truncated_history():
    """A decision made live at bar N is reproduced later from the first N rows, whatever came after."""
    rng = np.random.default_rng(3)
    closes = uptrend(bars=300) + path(6 * rng.standard_normal(200), start=uptrend(bars=300)[-1])
    rows = candles(closes)
    full_1m = strategy.frame_from_ohlcv(rows, drop_open_candle=False)
    full_5m = strategy.frame_from_ohlcv(five_minute(rows), drop_open_candle=False)
    live = {}
    for n in range(ms.WINDOW_1M, len(rows) + 1, 7):
        # What the feed had at the close of bar n: those 1m rows and the 5m bars closed by then.
        seen_1m = strategy.frame_from_ohlcv(rows[:n], drop_open_candle=False)
        seen_5m = strategy.frame_from_ohlcv(five_minute(rows[:n]), drop_open_candle=False)
        features, reason = ms.compute_features('ETH/USDT', seen_1m, seen_5m)
        live[n] = decide(features, now=closed_at(n), feature_reason=reason)
    for n, made in live.items():
        # Later: the full history exists. Truncating 1m to n rows and passing the
        # whole 5m frame (future 5m bars included) gives the same decision.
        features, reason = ms.compute_features('ETH/USDT', full_1m.iloc[:n], full_5m)
        again = decide(features, now=closed_at(n), feature_reason=reason)
        assert again == made and again.decision_id == made.decision_id
    assert len({d.decision_id for d in live.values()}) == len(live)


def test_future_bars_appended_or_rewritten_change_nothing_about_an_earlier_decision():
    closes = uptrend()
    f1, f5 = frames(closes)
    n = 300
    features, _ = ms.compute_features('ETH/USDT', f1.iloc[:n], f5)
    before = decide(features)
    # Rewrite every bar after n into a crash: the 5m frame now carries a very different future.
    crashed = closes[:n] + path([-80.0] * (len(closes) - n), start=closes[n - 1])
    g1, g5 = frames(crashed)
    rewritten, _ = ms.compute_features('ETH/USDT', g1.iloc[:n], g5)
    assert rewritten == features
    assert decide(rewritten) == before
    # Appending one more 5m bar that closes after bar n is ignored as well.
    later_5m = strategy.frame_from_ohlcv(five_minute(candles(closes + path([50.0] * 10, start=closes[-1]))),
                                         drop_open_candle=False)
    assert ms.compute_features('ETH/USDT', f1.iloc[:n], later_5m)[0] == features


def test_the_last_row_is_the_decision_bar_and_older_history_beyond_the_window_is_ignored():
    closes = ranging(bars=1000)
    f1, f5 = frames(closes)
    features, _ = ms.compute_features('ETH/USDT', f1, f5)
    assert features.bar_ts_ms == int(f1['timestamp'].iloc[-1]) and features.close == closes[-1]
    assert features.bars_1m == ms.WINDOW_1M and features.bars_5m == ms.WINDOW_5M
    # Fetching less (but at least the window) or more history yields the same features.
    assert ms.compute_features('ETH/USDT', f1.iloc[-ms.WINDOW_1M:], f5.iloc[-ms.WINDOW_5M:])[0] == features
    # The decision bar itself is read: a different last close is a different feature set.
    bumped = f1.copy()
    bumped.loc[bumped.index[-1], ['close', 'high']] = [closes[-1] * 1.003, closes[-1] * 1.004]
    other, _ = ms.compute_features('ETH/USDT', bumped, f5)
    assert other.close != features.close and other.zscore > features.zscore and other.last_change_bps > 0


def test_feature_definitions_hold_on_hand_checkable_series():
    flat = features_of([100.0] * 400)
    assert flat.efficiency == 0.0 and flat.zscore == 0.0 and flat.vol_bps == 0.0 and flat.drop_bps == 0.0
    assert flat.down_bars == 0 and flat.last_change_bps == 0.0 and flat.volume_ratio == 1.0
    assert flat.atr_bps == pytest.approx(4.0, rel=1e-9)        # only the 2 bps wicks either side
    # ATR is the simple mean of the last 14 true ranges: a wide bar 15th from the end no longer
    # counts at all (Wilder smoothing would still carry it), while one bar earlier it did.
    spike = candles([100.0] * 400)
    spike[-15][2], spike[-15][3] = 101.0, 99.0
    f1 = strategy.frame_from_ohlcv(spike, drop_open_candle=False)
    f5 = strategy.frame_from_ohlcv(five_minute(spike), drop_open_candle=False)
    assert ms.compute_features('ETH/USDT', f1, f5)[0].atr_bps == pytest.approx(4.0, rel=1e-9)
    assert ms.compute_features('ETH/USDT', f1.iloc[:-1], f5)[0].atr_bps > 4.0
    climb = features_of(path([4.0] * 400))
    assert climb.efficiency == pytest.approx(1.0) and climb.down_bars == 0 and climb.drop_bps == 0.0
    assert climb.slope_bps > 0 and climb.higher_trend_up and climb.higher_slope_bps > 0
    assert climb.zscore > 1.5                                 # stretched far above its 20-bar mean
    fall = features_of(path([4.0] * 390 + [-10.0] * 10))
    assert fall.down_bars == 10 and fall.drop_bps == pytest.approx((math.exp(-0.01) - 1) * 1e4)
    assert fall.last_change_bps < 0
    assert ms.Features(**climb.as_dict()) == climb


def test_short_or_unusable_frames_return_a_reason_instead_of_features():
    f1, f5 = frames(uptrend())
    assert ms.compute_features('ETH/USDT', f1.iloc[:ms.WARMUP_BARS_1M - 1], f5)[1].startswith('WARMUP: 59 of 60')
    assert ms.compute_features('ETH/USDT', f1, f5.iloc[:ms.WARMUP_BARS_5M - 1])[1].startswith('WARMUP: 49 of 50')
    assert ms.compute_features('ETH/USDT', None, f5)[1].startswith('WARMUP: 0 of')
    # 1m bars that end before the 5m history has 50 closed bars: the newer 5m bars do not count.
    assert ms.compute_features('ETH/USDT', f1.iloc[:200], f5)[1].startswith('WARMUP: 40 of 50 completed 5m')
    assert ms.compute_features('ETH/USDT', f1.drop(columns=['volume']), f5)[1] == 'ERROR: the 1m candles lack volume'
    shuffled = f1.iloc[::-1].reset_index(drop=True)
    assert ms.compute_features('ETH/USDT', shuffled, f5)[1].startswith('ERROR: the 1m candle timestamps')
    broken = f1.copy()
    broken.loc[broken.index[-1], 'close'] = float('nan')
    assert ms.compute_features('ETH/USDT', broken, f5) == (None, 'ERROR: the 1m candles hold non-finite values')


def test_a_5m_feed_that_stopped_is_stale_not_a_higher_timeframe_trend():
    f1, f5 = frames(uptrend())                      # 400 1m bars, 80 5m bars, both ending at the same close
    assert ms.compute_features('ETH/USDT', f1, f5)[0].bars_5m == 80
    # One 5m bar behind (the 5m frame refreshed a moment later than the 1m frame) is tolerated ...
    assert ms.compute_features('ETH/USDT', f1, f5.iloc[:79])[0] is not None
    # ... a feed that stopped earlier is not: its trend and slope are old news.
    late = ms.compute_features('ETH/USDT', f1, f5.iloc[:78])
    assert late[0] is None and late[1].startswith('STALE: the newest 5m bar closed 10 min')
    features, reason = ms.compute_features('ETH/USDT', f1, f5.iloc[:60])
    assert features is None and reason.startswith('STALE: the newest 5m bar closed 100 min')
    decision = decide(None, feature_reason=reason)
    assert_abstains(decision, ms.STALE_DATA)
    assert [c['check'] for c in decision.checks if not c['passed']][0] == 'features'


@pytest.mark.parametrize('field,value', [('vol_bps', float('nan')), ('drop_bps', float('nan')),
                                         ('atr_bps', float('inf')), ('last_change_bps', float('nan')),
                                         ('zscore', '1.0'), ('higher_trend_up', 1), ('bars_1m', 1.5),
                                         ('down_bars', True), ('close', 0.0), ('atr_bps', -1.0), ('symbol', None),
                                         ('bar_ts_ms', 0), ('efficiency', None)])
def test_features_refuse_non_finite_or_mistyped_fields(momentum, field, value):
    """A NaN fails every comparison: vol_bps NaN would pass the volatility gate and break the JSON record."""
    with pytest.raises(ValueError):
        dataclasses.replace(momentum, **{field: value})


def test_valid_features_survive_construction_and_a_strict_json_record(momentum):
    assert dataclasses.replace(momentum, last_change_bps=None).last_change_bps is None
    assert dataclasses.replace(momentum, atr_bps=9).atr_bps == 9.0 and isinstance(
        dataclasses.replace(momentum, atr_bps=9).atr_bps, float)
    assert dataclasses.replace(momentum, down_bars=np.int64(2)).down_bars == 2
    record = json.loads(json.dumps(decide(momentum).as_dict(), allow_nan=False))
    assert ms.Features(**record['inputs']['features']) == momentum


# ------------------------------------------------------------------- regime
def test_regime_precedence_puts_safety_first(momentum):
    assert ms.classify_regime(momentum, spread_bps=2, depth_ok=True, data_fresh=True, params=PARAMS)[0] == 'trending_up'
    stale = ms.classify_regime(momentum, spread_bps=50, depth_ok=False, data_fresh=False, params=PARAMS)
    assert stale == ('stale_uncertain', (ms.REASON_NOT_FRESH,))
    assert ms.classify_regime(None, spread_bps=2, depth_ok=True, data_fresh=True, params=PARAMS) == \
        ('stale_uncertain', (ms.REASON_NO_FEATURES,))
    # Missing is unknown, never zero: an unknown or non-finite spread is illiquid.
    for spread in (None, 'nan', float('inf'), -1, 'garbage'):
        regime, reasons = ms.classify_regime(momentum, spread_bps=spread, depth_ok=True, data_fresh=True, params=PARAMS)
        assert regime == 'illiquid' and reasons == (ms.REASON_SPREAD_UNKNOWN,)
    assert ms.classify_regime(momentum, spread_bps=2, depth_ok=None, data_fresh=True, params=PARAMS) == \
        ('illiquid', (ms.REASON_DEPTH,))
    assert ms.classify_regime(momentum, spread_bps=10.5, depth_ok=True, data_fresh=True, params=PARAMS)[0] == 'illiquid'
    volatile = dataclasses.replace(momentum, vol_bps=61.0)
    assert ms.classify_regime(volatile, spread_bps=2, depth_ok=True, data_fresh=True, params=PARAMS)[0] == \
        'excessively_volatile'
    # A collapse outranks an up-trend whose slow EMA is still rising.
    collapsed = dataclasses.replace(momentum, drop_bps=-4.0 * momentum.atr_bps)
    assert ms.classify_regime(collapsed, spread_bps=2, depth_ok=True, data_fresh=True, params=PARAMS)[0] == \
        'trending_down'
    no_higher = dataclasses.replace(momentum, higher_trend_up=False)
    assert ms.classify_regime(no_higher, spread_bps=2, depth_ok=True, data_fresh=True, params=PARAMS)[0] == \
        'no_reliable_edge'


def test_ranging_needs_low_efficiency_and_a_flat_higher_timeframe(ranging_features):
    assert ms.classify_regime(ranging_features, spread_bps=2, depth_ok=True, data_fresh=True,
                              params=PARAMS)[0] == 'ranging'
    tilted = dataclasses.replace(ranging_features, higher_slope_bps=-10.5)
    assert ms.classify_regime(tilted, spread_bps=2, depth_ok=True, data_fresh=True, params=PARAMS)[0] == \
        'no_reliable_edge'


# -------------------------------------------------------------- candidates
def test_momentum_pullback_enters_a_clean_uptrend_after_a_pullback(momentum):
    decision = decide(momentum)
    assert decision.action == 'enter' and decision.code is None and decision.reasons == ()
    assert decision.candidate == 'momentum_pullback' and decision.regime == 'trending_up'
    assert all(check['passed'] for check in decision.checks)
    proposal = decision.proposal
    assert proposal['opportunity_key'] == f'ETH/USDT|momentum_pullback|{momentum.bar_ts_ms}'
    # Targets and stops are in quote, scaled from the 25 USDT cap; the stop never exceeds the loss cap.
    assert D(proposal['target_net']) >= D('25') * D('20') / 10000
    assert D('0') < D(proposal['stop_net']) <= D('25') * D('80') / 10000
    # The stop is the loss carried at entry (5 bps: break-even 100.04 against the 99.99 bid) plus the ATR move.
    cost = ms.entry_cost_bps(budget_cap='25', max_debit='25.00', break_even_bid='100.04', best_bid='99.99')
    assert 4.9 < cost < 5.1
    assert proposal['stop_net_bps'] == ms.stop_net_bps(momentum, PARAMS, cost) == cost + ms.stop_move_bps(momentum)
    assert proposal['max_hold_s'] == 1800 and proposal['required_move_bps'] == '15'
    assert proposal['expected_range_bps'] == pytest.approx(momentum.atr_bps * math.sqrt(30))
    assert proposal['heuristic_rank'] >= 1 / PARAMS.edge_ratio and 'not calibrated' in proposal['heuristic_note']
    # The record ranks with an uncalibrated heuristic and never claims a probability or a confidence.
    assert set(proposal) == {'opportunity_key', 'target_net', 'stop_net', 'stop_net_bps', 'max_hold_s',
                             'expected_range_bps', 'required_move_bps', 'heuristic_rank', 'heuristic_note'}
    assert not {'confidence', 'probability', 'score'} & set(decision.as_dict())


def test_momentum_does_not_chase_a_stretched_uptrend():
    stretched = features_of(uptrend(tail=(4, 4, 4)))
    assert stretched.zscore > ms.MOMENTUM_MAX_ZSCORE
    decision = decide(stretched)
    assert_abstains(decision, ms.WEAK_TREND)
    assert [c['check'] for c in decision.checks if not c['passed']] == ['pullback']
    falling_last = features_of(uptrend(tail=(-7, -7, -7, -5, -1)))
    assert 'last_bar_up' in [c['check'] for c in decide(falling_last).checks if not c['passed']]


def test_momentum_does_not_enter_when_costs_consume_the_expected_move(momentum):
    limit = PARAMS.edge_ratio * momentum.atr_bps * math.sqrt(PARAMS.max_hold_s / 60)
    assert decide(momentum, sizing_record=sizing(required=f'{limit * 0.99:.4f}')).action == 'enter'
    costly = decide(momentum, sizing_record=sizing(required=f'{limit * 1.01:.4f}'))
    assert_abstains(costly, ms.NO_ECONOMIC_EDGE)
    assert costly.codes == (ms.NO_ECONOMIC_EDGE,)
    assert_abstains(decide(momentum, sizing_record=sizing(required=REALISTIC_REQUIRED_MOVE)), ms.NO_ECONOMIC_EDGE)
    # A spread that is a large share of the required move is a cost problem even inside the spread limit.
    assert_abstains(decide(momentum, spread_bps='6'), ms.FEES)


def test_range_reversion_enters_a_range_at_a_low_zscore(ranging_features):
    assert ranging_features.zscore <= ms.RANGE_MAX_ZSCORE
    decision = decide(ranging_features)
    assert decision.action == 'enter' and decision.candidate == 'range_reversion' and decision.regime == 'ranging'
    assert decision.proposal['opportunity_key'].startswith('ETH/USDT|range_reversion|')
    # The same range before the dip is not oversold: no entry.
    calm = features_of(ranging(dip=()))
    assert calm.zscore > ms.RANGE_MAX_ZSCORE
    assert_abstains(decide(calm), ms.WEAK_TREND)


@pytest.mark.parametrize('rate,noise,seed', [(-15, 3, 21), (-25, 5, 21), (-10, 2, 21), (-30, 0, 21),
                                             (-12, 10, 11), (-12, 10, 13), (-20, 8, 5)])
def test_range_reversion_does_not_keep_buying_through_a_collapse(rate, noise, seed):
    """Once a decline is sustained, no bar is a reversion entry, even if the regime were misread as ranging."""
    rng = np.random.default_rng(seed)
    base = list(100 * (1 + 0.001 * rng.standard_normal(400)))
    closes = base + path(rate + noise * rng.standard_normal(120), start=base[-1])
    f1, f5 = frames(closes)
    entries, forced_entries = [], []
    for n in range(401, len(closes) + 1):
        features, _ = ms.compute_features('ETH/USDT', f1.iloc[:n], f5)
        if decide(features, now=closed_at(n)).action == 'enter':
            entries.append(n - 400)
        if decide(features, regime='ranging', now=closed_at(n)).action == 'enter':
            forced_entries.append(n - 400)
    # The first few bars of a decline look like any dip; after more than four
    # lower closes or a four-ATR drop the guards hold on every remaining bar.
    assert all(bar <= ms.RANGE_MAX_DOWN_BARS + 1 for bar in entries + forced_entries), (entries, forced_entries)
    assert len(entries) <= ms.RANGE_MAX_DOWN_BARS


def test_the_reversion_guards_alone_refuse_a_falling_knife(ranging_features):
    knife = dataclasses.replace(ranging_features, zscore=-3.0, down_bars=5)
    assert_abstains(decide(knife, regime='ranging'), ms.WEAK_TREND)
    deep = dataclasses.replace(ranging_features, zscore=-3.0, drop_bps=-4.0 * ranging_features.atr_bps)
    assert_abstains(decide(deep, regime='ranging'), ms.WEAK_TREND)
    sliding = dataclasses.replace(ranging_features, zscore=-3.0, higher_slope_bps=-5.5)
    assert_abstains(decide(sliding, regime='ranging'), ms.WEAK_TREND)


@pytest.mark.parametrize('changes,problem', [
    (dict(budget_cap='1000', target_budget='1000', gross_quote='998.00', fee_reserve='2.00', max_debit='1000.00'),
     'outside the micro all-in cap'),
    (dict(budget_cap='25.01', target_budget='25.01', gross_quote='24.98', fee_reserve='0.03', max_debit='25.01'),
     'outside the micro all-in cap'),
    (dict(budget_cap='0', target_budget='0'), 'outside the micro all-in cap'),
    (dict(max_debit='400'), 'is not gross'),
    (dict(fee_reserve='0.02'), 'is not gross'),
    (dict(gross_quote='399.97', max_debit='400.00'), 'does not hold'),
    (dict(target_budget='26'), 'does not hold'),
    (dict(target_budget='24.99'), 'does not hold'),
    (dict(max_debit=None, gross_quote=None), 'gross_quote, max_debit missing'),
    (dict(target_budget='lots'), 'target_budget missing'),
])
def test_an_eligible_sizing_must_keep_the_all_in_invariant_under_the_micro_cap(momentum, changes, problem):
    """The proposal's target and stop are scaled from the cap: a defective record must not enter."""
    decision = decide(momentum, sizing_record=sizing(**changes))
    assert_abstains(decision, ms.ERROR)
    failed = [c for c in decision.checks if not c['passed']]
    assert failed[0]['check'] == 'sizing' and problem in failed[0]['detail']
    assert 'stop_beyond_entry_cost' not in [c['check'] for c in decision.checks]   # no cap, no scaled amounts


def test_a_smaller_consistent_sizing_still_enters_and_scales_from_its_own_cap(momentum):
    small = sizing(budget_cap='10', target_budget='10', gross_quote='9.98', fee_reserve='0.02', max_debit='10.00')
    decision = decide(momentum, sizing_record=small)
    assert decision.action == 'enter'
    # The 25 bps move plus the 5 bps entry cost, both of the 10 USDT cap.
    assert D(decision.proposal['target_net']) == D('0.02') and D('0.0299') < D(decision.proposal['stop_net']) <= D('0.03')
    assert decide(momentum).proposal['target_net'] == '0.050'      # the 25 USDT cap itself is allowed


def test_a_sizing_result_for_another_symbol_is_never_used(momentum):
    foreign = decide(momentum, sizing_record=sizing(symbol='BTC/USDT'))
    assert_abstains(foreign, ms.ERROR)
    assert [c['check'] for c in foreign.checks if not c['passed']] == ['sizing']
    assert 'BTC/USDT' in foreign.reasons[0]
    # Another market's required move never reaches the economic gate, and the record shows the mix-up.
    assert not {'economic_edge', 'fees', 'stop_beyond_entry_cost'} & {c['check'] for c in foreign.checks}
    assert foreign.inputs['sizing']['symbol'] == 'BTC/USDT'
    unnamed = {key: value for key, value in sizing().items() if key != 'symbol'}
    assert_abstains(decide(momentum, sizing_record=unnamed), ms.ERROR)
    # An ineligible record for another pair does not lend its code either.
    other_refusal = decide(momentum, sizing_record=sizing(symbol='BTC/USDT', eligible=False,
                                                          code='BUDGET_BELOW_VENUE_MINIMUM'))
    assert other_refusal.codes == (ms.ERROR,)
    assert decide(momentum).inputs['sizing']['symbol'] == 'ETH/USDT'


def test_the_stop_is_the_loss_carried_at_entry_plus_an_adverse_move(momentum):
    """A stop that ignored the round-trip cost would fire on the first exit check while the book is unchanged."""
    params = dataclasses.replace(PARAMS, target_net_bps=5)
    record, immediate = contract_sizing(target_bps=5)
    assert D('33') < D(record['required_move_bps']) < D('34.2')
    calm = dataclasses.replace(momentum, atr_bps=12.5)              # 2 ATR = 25 bps: the floor
    limit = params.edge_ratio * calm.atr_bps * math.sqrt(params.max_hold_s / 60)
    assert float(record['required_move_bps']) <= limit                # the economic gate passes
    # The contract's original stop, clamp(2 ATR, 25, max_loss) = 25 bps, sits inside the cost: selling at once hits it.
    old_stop = D('25') * D('25') / 10000
    exit_now = exit_for(estimate(money.text(immediate), uncertainty='0.0125'), target=D('0.0125'), stop=old_stop)
    assert (exit_now.action, exit_now.trigger) == ('exit', 'stop_loss')
    decision = decide(calm, params=params, sizing_record=record)
    assert decision.action == 'enter'
    stop = next(c for c in decision.checks if c['check'] == 'stop_beyond_entry_cost')
    assert 30 < stop['entry_cost_bps'] < 31 and stop['move_bps'] == 25.0
    assert decision.proposal['stop_net_bps'] == pytest.approx(stop['entry_cost_bps'] + 25.0)
    stop_net = D(decision.proposal['stop_net'])
    assert -stop_net < immediate                                         # the unchanged book does not trigger it
    held = exit_for(estimate(money.text(immediate), uncertainty='0.0125'), target=D('0.0125'), stop=stop_net)
    assert held.action == 'hold'
    # The same move beyond a cost that leaves no room under the loss cap is refused, never entered with a smaller stop.
    tight = dataclasses.replace(params, max_loss_bps=50)
    refused = decide(calm, params=tight, sizing_record=record)
    assert_abstains(refused, ms.NO_ECONOMIC_EDGE)
    assert [c['check'] for c in refused.checks if not c['passed']] == ['stop_beyond_entry_cost']
    # The move alone beyond the cap is a volatility refusal, reported before the cost is even counted.
    wild = decide(dataclasses.replace(momentum, atr_bps=40.5), sizing_record=record)
    assert_abstains(wild, ms.VOLATILITY)
    assert 'stop_beyond_entry_cost' not in [c['check'] for c in wild.checks]
    # An eligible sizing that hides its break-even bid cannot show what the entry costs.
    hidden = decide(momentum, sizing_record=sizing(break_even_bid=None))
    assert_abstains(hidden, ms.ERROR)
    assert [c['check'] for c in hidden.checks if not c['passed']] == ['stop_beyond_entry_cost']


def test_a_thin_top_bid_cannot_hide_the_exit_cost_from_the_stop_or_the_economic_gate(momentum):
    """The loss carried at entry is what selling into the bids returns, not what the best bid alone suggests."""
    import micro_economics as me
    import venue_rules
    rules = venue_rules.VenueRules(
        exchange='mexc', symbol='ETH/USDT', base='ETH', quote='USDT', active=True, spot=True, status='online',
        side_permission='all', market_orders=True, market_buy_by_cost=True, amount_step=D('0.0001'),
        price_tick=D('0.01'), cost_step=D('0.01'), min_amount=D('0.0001'), max_amount=None, min_cost_market=D('1'),
        min_cost_limit=D('5'), max_cost_market=D('100000'), max_cost_limit=D('2000000'), taker_fee=D('0.0005'),
        maker_fee=D('0'), fee_source='fixture', buy_fee_currency='quote', sell_fee_currency='quote',
        loaded_at=1000.0, source='fixture')
    cost = me.CostModel(taker_fee=D('0.001'), fee_source='test', latency_bps=D('3'), fallback_slippage_bps=D('20'),
                        buy_fee_currency='quote', sell_fee_currency='quote')

    def sized(bids):
        book = types.SimpleNamespace(bids=bids, asks=((D('100.01'), D('1000')),), depth_known=True,
                                     best_bid=bids[0][0], best_ask=D('100.01'), symbol='ETH/USDT', exchange='mexc')
        result = me.size_entry(rules=rules, book=book, cost=cost, budget_cap=D('25'), cash_available=D('1000'),
                               allocation_remaining=D('1000'), target_net=D('0.05'), exit_stress_bps=D('120'),
                               now_wall=2000.0)
        assert result.eligible, result.detail
        owned = money.add(result.sellable_base, result.predicted_residual_base)
        now = me.estimate_exit(owned_base=owned, basis_quote=result.max_debit, rules=rules, book=book, cost=cost,
                               target_net=D('0.05'), uncertainty_bps=D('5'))
        return result.as_dict(), now
    params = dataclasses.replace(PARAMS, max_loss_bps=120.0)
    lively = dataclasses.replace(momentum, atr_bps=20.0)           # a 40 bps adverse move
    deep, _ = sized(((D('99.99'), D('1000')),))
    assert decide(lively, params=params, sizing_record=deep).action == 'enter'
    thin, now = sized(((D('99.99'), D('0.02')), (D('99.59'), D('0.03')), (D('99.29'), D('0.05')),
                       (D('98.39'), D('4')), (D('98.29'), D('4'))))
    loss_now_bps = -float(now.est_net_pnl) / 25 * 1e4               # what the first exit check would see
    assert loss_now_bps > 120 - 40                                  # entered, the stop would fire at once
    decision = decide(lively, params=params, sizing_record=thin)
    assert_abstains(decision, ms.NO_ECONOMIC_EDGE)
    stop = next(c for c in decision.checks if c['check'] == 'stop_beyond_entry_cost')
    assert not stop['passed'] and stop['entry_cost_bps'] >= loss_now_bps - 1e-6
    edge = next(c for c in decision.checks if c['check'] == 'economic_edge')
    assert not edge['passed']                                        # the required move counts the walk too


def test_a_loss_cap_below_the_stop_floor_is_reported_as_the_floor_not_as_volatility(momentum):
    """A cap under the 25 bps minimum stop never fits a stop; the market is not to blame and the figures say so."""
    calm = dataclasses.replace(momentum, atr_bps=5.0)               # 2 ATR = 10 bps, well inside a 22 bps cap
    decision = decide(calm, params=dataclasses.replace(PARAMS, max_loss_bps=22.0))
    check = next(c for c in decision.checks if c['check'] == 'stop_within_loss_cap')
    assert not check['passed'] and check['code'] == ms.NO_ECONOMIC_EDGE and ms.VOLATILITY not in decision.codes
    assert check['move_bps'] == 25.0
    assert 'the 25 bps minimum stop exceeds the 22 bps loss cap' in check['detail'], check['detail']
    assert '2 ATR is 10.0 bps' in check['detail'] and 'Too volatile' not in decision.status
    # When two ATR alone passes the cap, the market is too volatile, and the stop figure stated is the stop used.
    for atr, cap, figure in ((11.0, 20.0, '25.0 bps adverse-move stop (2 ATR = 22.0 bps'), (40.5, 80.0, '81.0 bps')):
        wild = decide(dataclasses.replace(momentum, atr_bps=atr), params=dataclasses.replace(PARAMS, max_loss_bps=cap))
        check = next(c for c in wild.checks if c['check'] == 'stop_within_loss_cap')
        assert not check['passed'] and check['code'] == ms.VOLATILITY and figure in check['detail'], check['detail']


def test_entry_cost_is_measured_from_the_break_even_bid_or_not_at_all():
    assert ms.entry_cost_bps(budget_cap='25', max_debit='25', break_even_bid='100', best_bid='100') == 0.0
    assert ms.entry_cost_bps(budget_cap='25', max_debit='25', break_even_bid='100', best_bid='101') == 0.0
    assert ms.entry_cost_bps(budget_cap='25', max_debit='25', break_even_bid='100', best_bid='99') == \
        pytest.approx(100.0)
    for bad in (dict(break_even_bid=None), dict(break_even_bid='0'), dict(best_bid='-1'), dict(budget_cap='0'),
                dict(max_debit='nan'), dict(best_bid='junk')):
        args = {**dict(budget_cap='25', max_debit='25', break_even_bid='100', best_bid='99'), **bad}
        assert ms.entry_cost_bps(**args) is None


@pytest.mark.parametrize('seed', [1, 2, 3, 4, 5])
def test_a_random_walk_abstains_on_the_vast_majority_of_bars(seed):
    """Brief test 13: no edge in the data, no forced trades. Zero entries is a valid outcome."""
    rng = np.random.default_rng(seed)
    closes = path(10 * rng.standard_normal(900))
    f1, f5 = frames(closes)
    for required, ceiling in ((REALISTIC_REQUIRED_MOVE, 0.01), ('20', 0.03)):
        decisions = []
        for n in range(310, len(closes) + 1):
            features, reason = ms.compute_features('ETH/USDT', f1.iloc[:n], f5)
            decisions.append(decide(features, sizing_record=sizing(required=required), now=closed_at(n),
                                    feature_reason=reason))
        entered = sum(d.action == 'enter' for d in decisions)
        assert entered <= ceiling * len(decisions), (required, entered)
        assert all(d.code in ms.CODE_PRIORITY and d.reasons for d in decisions if d.action == 'abstain')


# ------------------------------------------------------------------- gates
GATE_FAILURES = [
    ('review', {'gates': {'review_block': 'cycle 12 is in REVIEW'}}, ms.REVIEW),
    ('risk', {'gates': {'risk_block': 'daily loss limit reached'}}, ms.RISK),
    ('latch', {'gates': {'latched': 'kill switch'}}, ms.RISK),
    ('not_selected', {'gates': {'selected': False}}, ms.NOT_SELECTED),
    ('no_capacity', {'gates': {'capacity': 0}}, ms.CAPACITY),
    ('cooldown', {'gates': {'cooldown_remaining_s': 30}}, ms.COOLDOWN),
    ('same_bar_as_exit', {'gates': {'fresh_after_exit': False}}, ms.COOLDOWN),
    ('stale', {'data_fresh': False}, ms.STALE_DATA),
    ('no_book', {'book': {'spread_bps': '2'}}, ms.STALE_DATA),
    ('price_unknown', {'sizing': {'eligible': False, 'code': 'PRICE_UNKNOWN'}}, ms.STALE_DATA),
    ('warmup', {'features': None, 'feature_reason': 'WARMUP: 41 of 60 completed 1m bars'}, ms.WARMUP),
    ('wide_spread', {'spread_bps': '10.5'}, ms.SPREAD),
    ('unknown_spread', {'book': {k: v for k, v in BOOK.items() if k != 'spread_bps'}}, ms.SPREAD),
    ('thin_book', {'depth_ok': False}, ms.INSUFFICIENT_DEPTH),
    ('depth_unknown', {'sizing': {'eligible': False, 'code': 'DEPTH_UNKNOWN'}}, ms.INSUFFICIENT_DEPTH),
    ('ask_depth', {'sizing': {'eligible': False, 'code': 'INSUFFICIENT_ASK_DEPTH'}}, ms.INSUFFICIENT_DEPTH),
    ('bid_depth', {'sizing': {'eligible': False, 'code': 'INSUFFICIENT_BID_DEPTH'}}, ms.INSUFFICIENT_DEPTH),
    ('venue_minimum', {'sizing': {'eligible': False, 'code': 'BUDGET_BELOW_VENUE_MINIMUM',
                                  'required_move_bps': None}}, ms.BUDGET_MINIMUM),
    ('exit_minimum', {'sizing': {'eligible': False, 'code': 'EXIT_BELOW_VENUE_MINIMUM'}}, ms.BUDGET_MINIMUM),
    ('cash', {'sizing': {'eligible': False, 'code': 'CASH_INSUFFICIENT'}}, ms.BUDGET_MINIMUM),
    ('allocation', {'sizing': {'eligible': False, 'code': 'ALLOCATION_EXHAUSTED'}}, ms.BUDGET_MINIMUM),
    ('volatile', {'params': {'max_vol_bps': 1.0}}, ms.VOLATILITY),
    ('stop_beyond_cap', {'params': {'max_loss_bps': 20.0}, 'feature_changes': {'atr_bps': 11.0}}, ms.VOLATILITY),
    ('down_trend', {'regime': 'trending_down'}, ms.WEAK_TREND),
    ('no_edge_regime', {'regime': 'no_reliable_edge'}, ms.WEAK_TREND),
    ('fees', {'spread_bps': '5.5'}, ms.FEES),
    ('fee_unknown', {'sizing': {'eligible': False, 'code': 'FEE_UNKNOWN'}}, ms.FEES),
    ('no_edge', {'sizing': {'required_move_bps': '40'}}, ms.NO_ECONOMIC_EDGE),
    ('venue_refused', {'sizing': {'eligible': False, 'code': 'MARKET_INACTIVE'}}, ms.ERROR),
    ('sizing_missing', {'sizing_record': 'not a dict'}, ms.ERROR),
    ('negative_move', {'sizing': {'required_move_bps': '-3'}}, ms.ERROR),
    ('unknown_regime', {'regime': 'sideways'}, ms.ERROR),
    ('wrong_symbol', {'symbol': 'BTC/USDT'}, ms.ERROR),
    ('foreign_sizing', {'sizing': {'symbol': 'BTC/USDT'}}, ms.ERROR),
    ('sizing_above_cap', {'sizing': {'budget_cap': '1000', 'target_budget': '1000', 'gross_quote': '998.00',
                                     'fee_reserve': '2.00', 'max_debit': '1000.00'}}, ms.ERROR),
    ('crossed_quote_spread', {'book': {**BOOK, 'best_bid': '90'}}, ms.SPREAD),
    ('open_candle', {'now_offset': -45}, ms.ERROR),
    ('old_bar', {'now_offset': 6 * 3600}, ms.STALE_DATA),
    ('bad_candles', {'features': None, 'feature_reason': 'ERROR: the 1m candles lack volume'}, ms.ERROR),
]


def scenario(features, change: dict):
    change = dict(change)
    kwargs = {'gates': {**GATES, **change.pop('gates', {})}}
    if 'sizing' in change:
        kwargs['sizing_record'] = sizing(**change.pop('sizing'))
    if 'params' in change:
        kwargs['params'] = dataclasses.replace(PARAMS, **change.pop('params'))
    if 'features' in change:
        features = change.pop('features')
    if 'feature_changes' in change:
        features = dataclasses.replace(features, **change.pop('feature_changes'))
    if 'now_offset' in change:          # seconds from the decision bar's close
        kwargs['now'] = (features.bar_ts_ms + ms.BAR_MS_1M) / 1000 + change.pop('now_offset')
    kwargs.update(change)
    return decide(features, **kwargs)


@pytest.mark.parametrize('name,change,code', GATE_FAILURES, ids=[case[0] for case in GATE_FAILURES])
def test_every_failed_gate_abstains_with_its_code(momentum, name, change, code):
    assert decide(momentum).action == 'enter'          # the baseline passes every check
    assert_abstains(scenario(momentum, change), code)


@pytest.mark.parametrize('key', ms.GATE_KEYS)
def test_a_gate_that_was_not_supplied_is_unknown_and_fails(momentum, key):
    gates = {name: value for name, value in GATES.items() if name != key}
    decision = decide(momentum, gates=gates)
    expected = {'selected': ms.NOT_SELECTED, 'capacity': ms.CAPACITY, 'cooldown_remaining_s': ms.COOLDOWN,
                'fresh_after_exit': ms.COOLDOWN, 'review_block': ms.REVIEW, 'risk_block': ms.RISK,
                'latched': ms.RISK}[key]
    assert_abstains(decision, expected)


@pytest.mark.parametrize('value', [float('nan'), D('NaN'), float('inf'), 0, 1.5, D('0'), np.float64('nan')],
                         ids=['nan', 'decimal_nan', 'inf', 'zero', 'number', 'decimal_zero', 'numpy_nan'])
@pytest.mark.parametrize('key,code', [('review_block', ms.REVIEW), ('risk_block', ms.RISK), ('latched', ms.RISK)])
def test_an_unreadable_block_value_blocks_and_still_blocks_after_a_json_round_trip(momentum, key, code, value):
    decision = decide(momentum, gates={**GATES, key: value})
    assert_abstains(decision, code)
    stored = decision.inputs['gates'][key]
    assert isinstance(stored, str) and stored.startswith('unreadable')
    replayed = decide(momentum, gates=json.loads(json.dumps(decision.inputs['gates'])))
    assert replayed == decision
    # Only None, False or a reason string are block states; a numpy False is still False.
    assert decide(momentum, gates={**GATES, key: np.bool_(False)}).action == 'enter'


EXPECTED_MOMENTUM_CHECKS = ['review', 'risk', 'latch', 'selected', 'capacity', 'cooldown', 'fresh_after_exit',
                            'features', 'book', 'spread', 'regime', 'sizing', 'above_slow_ema', 'pullback',
                            'last_bar_up', 'no_collapse', 'stop_within_loss_cap', 'stop_beyond_entry_cost',
                            'economic_edge', 'fees']
EXPECTED_RANGE_CHECKS = ['review', 'risk', 'latch', 'selected', 'capacity', 'cooldown', 'fresh_after_exit',
                         'features', 'book', 'spread', 'regime', 'sizing', 'oversold', 'down_bars', 'no_collapse',
                         'higher_slope', 'stop_within_loss_cap', 'stop_beyond_entry_cost', 'economic_edge', 'fees']


def test_every_check_is_recorded_in_contract_order(momentum, ranging_features):
    assert [c['check'] for c in decide(momentum).checks] == EXPECTED_MOMENTUM_CHECKS
    assert [c['check'] for c in decide(ranging_features).checks] == EXPECTED_RANGE_CHECKS


def test_all_failed_checks_are_listed_in_order_and_the_dominant_code_follows_the_priority(momentum):
    layers = [  # (code, change, the checks this layer fails), highest priority first
        (ms.REVIEW, {'gates': {'review_block': True}}, ['review']),
        (ms.RISK, {'gates': {'risk_block': 'drawdown'}}, ['risk']),
        (ms.NOT_SELECTED, {'gates': {'selected': False}}, ['selected']),
        (ms.CAPACITY, {'gates': {'capacity': False}}, ['capacity']),
        (ms.COOLDOWN, {'gates': {'cooldown_remaining_s': 12.5}}, ['cooldown']),
        (ms.STALE_DATA, {'data_fresh': False}, ['regime']),
        (ms.SPREAD, {'spread_bps': '12'}, ['spread', 'regime']),
        (ms.BUDGET_MINIMUM, {'sizing': {'eligible': False, 'code': 'BUDGET_BELOW_VENUE_MINIMUM'}}, ['sizing']),
        (ms.VOLATILITY, {'params': {'max_loss_bps': 20.0}, 'feature_changes': {'atr_bps': 11.0}},
         ['stop_within_loss_cap']),
        (ms.NO_ECONOMIC_EDGE, {'sizing': {'required_move_bps': '200'}}, ['economic_edge']),
    ]
    always = ['review', 'risk', 'latch', 'selected', 'capacity', 'cooldown', 'fresh_after_exit', 'features', 'book',
              'spread', 'regime', 'sizing', 'stop_within_loss_cap', 'economic_edge', 'fees']
    for start in range(len(layers) + 1):
        active = layers[start:]
        change: dict = {}
        for _, layer, _ in reversed(active):    # higher-priority layers win where they touch the same input
            for key, value in layer.items():
                change[key] = {**change.get(key, {}), **value} if isinstance(value, dict) else value
        decision = scenario(momentum, change)
        order = [check['check'] for check in decision.checks]
        failed = [check['check'] for check in decision.checks if not check['passed']]
        # Recorded checks follow the contract order (an explicit list, not one built from
        # this decision), the core checks are always recorded, and exactly the active
        # layers' checks fail, in that order.
        assert order == [name for name in EXPECTED_MOMENTUM_CHECKS if name in order]
        assert set(always) <= set(order)
        own = {name for _, _, names in active for name in names}
        assert failed == [name for name in EXPECTED_MOMENTUM_CHECKS if name in own]
        if not active:
            assert decision.action == 'enter' and order == EXPECTED_MOMENTUM_CHECKS
            continue
        assert_abstains(decision, active[0][0])
        assert {code for code, _, _ in active} <= set(decision.codes)
    assert list(ms.CODE_PRIORITY) == ['REVIEW', 'RISK', 'NOT_SELECTED', 'CAPACITY', 'COOLDOWN', 'STALE_DATA',
                                      'WARMUP', 'SPREAD', 'INSUFFICIENT_DEPTH', 'BUDGET_MINIMUM', 'VOLATILITY',
                                      'WEAK_TREND', 'FEES', 'NO_ECONOMIC_EDGE', 'ERROR']


def test_warmup_abstains_even_when_everything_else_passes():
    f1, f5 = frames(uptrend())
    features, reason = ms.compute_features('ETH/USDT', f1.iloc[:45], f5)
    assert features is None and reason.startswith('WARMUP')
    decision = decide(None, feature_reason=reason)
    assert_abstains(decision, ms.WARMUP)
    assert decision.regime == 'stale_uncertain' and decision.candidate is None
    # Without the reason the record still says WARMUP; with stale data as well, staleness dominates.
    assert decide(None).code == ms.WARMUP
    both = decide(None, data_fresh=False, feature_reason=reason)
    assert both.code == ms.STALE_DATA and ms.WARMUP in both.codes


def test_the_decision_bar_must_have_closed_by_now_and_be_recent(momentum):
    closes = (momentum.bar_ts_ms + ms.BAR_MS_1M) / 1000
    for early in (45, 0.001):                    # an open candle is not a completed bar: no look-ahead
        decision = decide(momentum, now=closes - early)
        assert_abstains(decision, ms.ERROR)
        assert decision.reasons[0].startswith('the decision bar closes') and decision.candidate is None
    assert decide(momentum, now=closes).action == 'enter'
    assert decide(momentum, now=closes + ms.MAX_BAR_AGE_S).action == 'enter'
    for late in (ms.MAX_BAR_AGE_S + 1, 6 * 3600):
        decision = decide(momentum, now=closes + late)
        assert_abstains(decision, ms.STALE_DATA)
        assert [c['check'] for c in decision.checks if not c['passed']] == ['features']
    timing = next(c for c in decide(momentum).checks if c['check'] == 'features')
    assert timing['bar_close_ms'] == momentum.bar_ts_ms + 60_000 and timing['bar_age_s'] == pytest.approx(0.25)


# ---------------------------------------------------------------- replay
def test_decision_ids_replay_from_the_record_and_change_with_any_input(momentum):
    first = decide(momentum)
    assert decide(momentum) == first
    assert first.decision_id.startswith('mdec_') and len(first.decision_id) == 21
    record = json.loads(json.dumps(first.as_dict()))         # survives storage as JSON
    inputs = record['inputs']
    replayed = ms.evaluate_entry(symbol=record['symbol'], features=ms.Features(**inputs['features']),
                                 regime=inputs['regime'], regime_reasons=tuple(inputs['regime_reasons']),
                                 sizing=inputs['sizing'], book_summary=inputs['book'],
                                 params=ms.MicroParams(**inputs['params']), gates=inputs['gates'],
                                 now=record['decided_at'], feature_reason=inputs['feature_reason'])
    assert replayed == first
    # The sizing record's bulky parts (checks, cost model) are not part of the replay inputs.
    assert 'checks' not in inputs['sizing'] and 'cost' not in inputs['sizing']
    assert decide(momentum, now=fresh(momentum) + 0.001).decision_id != first.decision_id
    assert decide(momentum, now=fresh(momentum) + 0.0001).decision_id == first.decision_id   # rounded to the ms
    assert decide(momentum, gates={**GATES, 'capacity': 2}).decision_id != first.decision_id
    assert decide(momentum, book={**BOOK, 'generation': 8}).decision_id != first.decision_id
    assert decide(dataclasses.replace(momentum, bar_ts_ms=momentum.bar_ts_ms + 60_000)).decision_id != \
        first.decision_id
    abstained = decide(momentum, gates={**GATES, 'selected': False})
    assert abstained.decision_id != first.decision_id and abstained.as_dict()['inputs']['gates']['selected'] is False


def test_entry_inputs_are_json_safe_whatever_the_caller_passes(momentum):
    book = {**BOOK, 'best_bid': D('99.99'), 'best_ask': D('100.01'), 'spread_bps': D('2.0'), 'age_s': float('nan'),
            'raw': object()}
    decision = decide(momentum, book=book, sizing_record=sizing(budget_cap=D('25')))
    text = json.dumps(decision.as_dict(), allow_nan=False)
    assert 'raw' not in json.loads(text)['inputs']['book']
    assert decision.action == 'enter' and decision.inputs['book']['age_s'] is None
    with pytest.raises(ValueError):
        decide(momentum, now=float('nan'))
    with pytest.raises(TypeError):
        decide(momentum, regime='trending_up', params={'edge_ratio': 0.5})
    with pytest.raises(TypeError):
        ms.classify_regime(momentum, spread_bps=2, depth_ok=True, data_fresh=True, params={'edge_ratio': 0.5})


def test_huge_numbers_never_expand_into_the_record(momentum):
    for generation in (D('1E+10000000'), D('1E+999999999'), 10 ** 5000):
        decision = decide(momentum, book={**BOOK, 'generation': generation})
        assert decision.inputs['book']['generation'] is None
        assert len(json.dumps(decision.as_dict(), allow_nan=False)) < 20_000
    # An unreadable best bid is unknown, so the book check fails rather than storing a giant string.
    assert_abstains(decide(momentum, book={**BOOK, 'best_bid': D('1E+10000000')}), ms.STALE_DATA)
    assert decide(momentum, book={**BOOK, 'generation': 2 ** 40}).inputs['book']['generation'] == 2 ** 40


def test_the_spread_is_never_narrower_than_the_best_bid_and_ask_imply(momentum):
    wide = decide(momentum, book={**BOOK, 'best_bid': '90', 'best_ask': '100.01', 'spread_bps': '2'})
    assert_abstains(wide, ms.SPREAD)
    spread = next(c for c in wide.checks if c['check'] == 'spread')
    assert spread['reported'] == 2.0 and spread['from_quote'] == pytest.approx(1053.6, abs=0.1)
    assert spread['value'] == spread['from_quote']
    # The best bid and ask imply 2 bps here; a wider reported spread is kept (it is the safer figure).
    assert next(c for c in decide(momentum, spread_bps='6').checks if c['check'] == 'spread')['value'] == 6.0
    assert decide(momentum).action == 'enter'


# ---------------------------------------------------------- params, stops
@pytest.mark.parametrize('field,value', [('edge_ratio', float('nan')), ('max_spread_bps', float('inf')),
                                         ('max_vol_bps', True), ('max_hold_s', 0), ('target_net_bps', -1),
                                         ('max_efficiency_range', 1.5), ('uncertainty_floor_bps', -0.1),
                                         ('max_loss_bps', None), ('min_efficiency_trend', '0.3'),
                                         ('target_net_bps', 1e16), ('target_net_bps', 1e-31),
                                         ('target_net_bps', 4.9), ('target_net_bps', 200.1),
                                         ('max_spread_bps', 1e300), ('edge_ratio', 1e300), ('edge_ratio', 1.01),
                                         ('max_vol_bps', 1e300), ('max_loss_bps', 10.0), ('max_loss_bps', 401),
                                         ('max_hold_s', 60), ('max_hold_s', 1e9),
                                         ('uncertainty_floor_bps', 1e300)])
def test_params_refuse_values_that_would_silently_pass_a_gate(field, value):
    with pytest.raises(ValueError):
        dataclasses.replace(PARAMS, **{field: value})


def test_params_accept_the_settings_bounds_and_amount_errors_abstain(momentum):
    for field, values in (('target_net_bps', (5, 200)), ('max_loss_bps', (20, 400)), ('max_hold_s', (120, 14_400)),
                          ('edge_ratio', (0.01, 1.0)), ('uncertainty_floor_bps', (0, 100))):
        for value in values:
            assert getattr(dataclasses.replace(PARAMS, **{field: value}), field) == value
    # Should an amount still fall outside money's range, the decision is an ERROR abstain, not an exception.
    broken = dataclasses.replace(PARAMS)
    object.__setattr__(broken, 'target_net_bps', 1e16)
    decision = decide(momentum, params=broken)
    assert_abstains(decision, ms.ERROR)
    assert [c['check'] for c in decision.checks if not c['passed']] == ['amounts']


def test_stop_is_the_entry_cost_plus_twice_atr_floored_at_25_and_never_beyond_the_loss_cap(momentum):
    def stop(atr, cap=80.0, cost=0.0):
        return ms.stop_net_bps(dataclasses.replace(momentum, atr_bps=atr),
                               dataclasses.replace(PARAMS, max_loss_bps=cap), cost)
    assert stop(5.0) == 25.0 and stop(20.0) == 40.0 and stop(40.0) == 80.0
    assert stop(40.01) is None                     # a 2 ATR stop the cap cannot hold: too volatile
    assert stop(8.0, cap=20.0) is None             # a cap below the 25 bps floor holds no stop: never entered
    assert stop(5.0, cost=30.0) == 55.0 and stop(20.0, cost=40.0) == 80.0
    assert stop(20.0, cost=40.5) is None           # cost plus move beyond the cap
    assert stop(5.0, cost=-1.0) is None and stop(5.0, cost=float('nan')) is None
    # Features refuse a NaN ATR; anything else carrying one gets no stop.
    with pytest.raises(ValueError):
        dataclasses.replace(momentum, atr_bps=float('nan'))
    assert ms.stop_net_bps(types.SimpleNamespace(atr_bps=float('nan')), PARAMS) is None
    assert ms.stop_move_bps(types.SimpleNamespace(atr_bps=-1.0)) is None


# --------------------------------------------------------------------- exit
TARGET, STOP = D('0.05'), D('0.20')
OPENED = NOW - 600


def estimate(est_net, *, uncertainty='0.004', depth_model='book', basis='25.00', fee='0.025', exhausted=False):
    """The shape of micro_economics.ExitEstimate.as_dict(), internally consistent."""
    if est_net is None:
        return dict(sellable_base='0.2493', gross_proceeds=None, sell_fee=None, net_proceeds=None,
                    allocated_basis=basis, est_net_pnl=None, uncertainty=uncertainty, depth_model=depth_model,
                    depth_exhausted=exhausted, break_even_bid=None, target_bid=None, residual_base='0',
                    labels={'proceeds': 'unknown'})
    net = D(basis) + D(est_net)
    return dict(sellable_base='0.2493', gross_proceeds=str(net + D(fee)), sell_fee=fee, net_proceeds=str(net),
                allocated_basis=basis, est_net_pnl=est_net, uncertainty=uncertainty, depth_model=depth_model,
                depth_exhausted=exhausted, break_even_bid='100.24', target_bid='100.44', residual_base='0',
                labels={'proceeds': 'estimated'})


def exit_for(est, *, depth_model=None, peak=None, regime='ranging', now=NOW, opened=OPENED, max_hold_s=1800.0,
             target=TARGET, stop=STOP, realized=None):
    model = depth_model if depth_model is not None else (est or {}).get('depth_model', 'book')
    extra = {} if realized is None else {'realized_net': realized}
    return ms.evaluate_exit(estimate=est, depth_model=model, target_net=target, stop_net=stop, peak_est_net=peak,
                            opened_wall=opened, max_hold_s=max_hold_s, regime=regime, now=now, **extra)


@pytest.mark.parametrize('model', ['flat_fallback', 'book'])
def test_safety_exits_fire_without_depth_and_without_any_profit(model):
    """Brief test 20: stop, maximum hold and regime shift need no profit, no entry signal and no depth."""
    stop = exit_for(estimate('-0.20', depth_model=model))
    assert (stop.action, stop.trigger, stop.needs_depth) == ('exit', 'stop_loss', False)
    held = exit_for(estimate('-0.01', depth_model=model), now=OPENED + 1800)
    assert (held.action, held.trigger, held.needs_depth) == ('exit', 'max_hold', False)
    shifted = exit_for(estimate('-0.001', depth_model=model), regime='trending_down')
    assert (shifted.action, shifted.trigger, shifted.needs_depth) == ('exit', 'regime_shift', False)
    assert exit_for(estimate('-0.001', depth_model=model), regime='excessively_volatile').trigger == 'regime_shift'
    # A maximum hold fires even when the book cannot price the position.
    blind = exit_for(estimate(None, depth_model='unavailable'), now=OPENED + 1800)
    assert (blind.action, blind.trigger) == ('exit', 'max_hold')
    # A hostile regime does not dump a winner: that is left to the profit exits.
    assert exit_for(estimate('0.01', depth_model=model), regime='trending_down').action == 'hold'


def test_profit_exits_need_a_book_depth_estimate():
    rich = estimate('0.08')
    taken = exit_for(rich)
    assert (taken.action, taken.trigger, taken.needs_depth) == ('exit', 'take_profit', True)
    assert exit_for(estimate('0.08', depth_model='flat_fallback')).action == 'hold'
    assert exit_for(rich, depth_model='flat_fallback').action == 'hold'          # the two must agree
    assert exit_for(estimate('0.08', exhausted=True)).action == 'hold'
    # Missing depth evidence is not depth evidence: the estimate itself must say 'book' and not exhausted.
    unproven = [{k: v for k, v in rich.items() if k != 'depth_exhausted'},
                {k: v for k, v in rich.items() if k != 'depth_model'},
                dict(rich, depth_exhausted=None), dict(rich, depth_exhausted='true'), dict(rich, depth_exhausted=0),
                dict(rich, depth_model=None)]
    for est in unproven:
        decision = exit_for(est, depth_model='book')
        assert decision.action == 'hold' and not decision.needs_depth, est
    # Safety exits still fire on such an estimate.
    assert exit_for(dict(unproven[0], est_net_pnl='-0.20', net_proceeds='24.80'), depth_model='book').trigger == \
        'stop_loss'
    # Safety exits come first in the documented order even when a profit is available.
    assert exit_for(rich, now=OPENED + 1800).trigger == 'max_hold'


def test_a_gross_gain_below_costs_never_takes_profit():
    # A gross gain beyond the target plus uncertainty, which the sell fee turns into a loss.
    losing = estimate('-0.005', fee='0.07')
    gross_gain = D(losing['gross_proceeds']) - D(losing['allocated_basis'])
    assert gross_gain - D(losing['uncertainty']) >= TARGET
    assert exit_for(losing).action == 'hold'
    # Even a gross figure passed as est_net_pnl by mistake cannot take profit.
    mislabeled = dict(losing, est_net_pnl=str(gross_gain))
    decision = exit_for(mislabeled)
    assert decision.action == 'hold' and decision.new_peak == D('-0.005')
    # At the target before uncertainty, but not after it.
    assert exit_for(estimate('0.052', uncertainty='0.004')).action == 'hold'
    assert exit_for(estimate('0.054', uncertainty='0.004')).trigger == 'take_profit'
    # Unknown uncertainty is not zero uncertainty.
    assert exit_for(dict(estimate('0.2'), uncertainty=None)).action == 'hold'


def test_profit_protection_gives_back_at_most_three_quarters_of_a_reached_target():
    peak = D('0.04')                                  # 0.8 of the target was reached
    protect = exit_for(estimate('0.014', uncertainty='0.002'), peak=peak)
    assert (protect.trigger, protect.needs_depth) == ('profit_protection', True) and protect.new_peak == peak
    assert exit_for(estimate('0.016', uncertainty='0.002'), peak=peak).action == 'hold'   # above 0.25 target
    assert exit_for(estimate('0.002', uncertainty='0.002'), peak=peak).action == 'hold'   # nothing left to protect
    assert exit_for(estimate('0.014', uncertainty='0.002'), peak=D('0.037')).action == 'hold'  # peak below 0.75
    # The peak ratchets on book estimates only, and never down.
    assert exit_for(estimate('0.03'), peak=D('0.01')).new_peak == D('0.03')
    assert exit_for(estimate('0.005'), peak=D('0.03')).new_peak == D('0.03')
    assert exit_for(estimate('0.045', depth_model='flat_fallback'), peak=D('0.01')).new_peak == D('0.01')
    assert exit_for(estimate('0.03'), peak=None).new_peak == D('0.03')


def test_profit_protection_needs_a_give_back_even_when_the_uncertainty_exceeds_half_the_target():
    """A steady rise through the protection band is not a give-back: it goes on to take profit."""
    for target, uncertainty in ((D('0.05'), D('0.0375')), (D('0.02'), D('0.0125')), (D('0.05'), D('0.0125'))):
        peak, verdict, est = None, None, D('0')
        while verdict is None or verdict.action != 'exit':
            est += D('0.0025')
            verdict = exit_for(estimate(str(est), uncertainty=str(uncertainty)), peak=peak, target=target)
            peak = verdict.new_peak
        assert verdict.trigger == 'take_profit', (target, uncertainty, est, verdict.reason)
        assert est - uncertainty >= target
    # Once the net after uncertainty has been above the band, falling back into it is protected.
    fell = exit_for(estimate('0.045', uncertainty='0.0375'), peak=D('0.07'))
    assert (fell.trigger, fell.new_peak) == ('profit_protection', D('0.07'))
    # A peak whose net after uncertainty never left the band protects nothing yet.
    assert exit_for(estimate('0.045', uncertainty='0.0375'), peak=D('0.05')).action == 'hold'


def test_a_stop_counts_the_loss_already_realized_by_a_partial_exit():
    """The stop is an amount for the whole cycle, so the rest of a partial sale carries what was already lost."""
    assert exit_for(estimate('-0.09')).action == 'hold'
    rest = exit_for(estimate('-0.09'), realized=D('-0.12'))       # -0.21 for the cycle against a 0.20 stop
    assert (rest.action, rest.trigger) == ('exit', 'stop_loss')
    stop = next(c for c in rest.checks if c['check'] == 'stop_loss')
    assert stop['realized_net'] == '-0.12' and stop['cycle_net'] == '-0.21'
    assert exit_for(estimate('-0.07'), realized=D('-0.12')).action == 'hold'
    # A cycle that has already lost its whole stop sells the rest at once, even at a small estimated gain.
    assert exit_for(estimate('0.01'), realized=D('-0.25')).trigger == 'stop_loss'
    # A realized gain never loosens the stop on the rest.
    assert exit_for(estimate('-0.20'), realized=D('0.05')).trigger == 'stop_loss'
    assert exit_for(estimate('-0.19'), realized=D('0.05')).action == 'hold'


def test_a_stop_fired_by_a_realized_loss_names_the_cycle_net_in_its_reason():
    """The reason is stored as the trade's reason and shown beside the rest's estimate: it must say what fired."""
    rest = exit_for(estimate('-0.09'), realized=D('-0.12'))
    assert rest.reason == ('stop loss: cycle net -0.2100 (estimated -0.0900 on the rest plus -0.1200 already '
                           'realized) at or below the stop -0.2000')
    stop = next(c for c in rest.checks if c['check'] == 'stop_loss')
    assert stop['detail'] == rest.reason.removeprefix('stop loss: ')
    # A small estimated gain on the rest is not described as an estimated loss.
    gain = exit_for(estimate('0.01'), realized=D('-0.25'))
    assert 'estimated +0.0100 on the rest plus -0.2500 already realized' in gain.reason
    assert 'estimated net at or below' not in gain.reason
    # Without a realized loss the estimate alone fired it, and the reason says so.
    for realized in (None, D('0'), D('0.05')):
        assert exit_for(estimate('-0.20'), realized=realized).reason == 'stop loss: estimated net at or below the stop'


def test_without_an_estimate_the_exit_cannot_be_evaluated():
    missing = exit_for(None, peak=D('0.02'))
    assert (missing.action, missing.trigger, missing.needs_depth, missing.new_peak) == \
        ('cannot_evaluate', None, False, D('0.02'))
    late = exit_for(None, now=OPENED + 3600)
    assert late.action == 'cannot_evaluate'
    assert [c for c in late.checks if c['check'] == 'max_hold'][0]['fired'] is True   # visible, waits for data
    assert exit_for(estimate(None, depth_model='unavailable')).action == 'cannot_evaluate'
    # An unreadable stop or target is not a pass: the cycle cannot be evaluated unless a safety exit fires.
    assert exit_for(estimate('-0.30'), stop=None).action == 'cannot_evaluate'
    assert exit_for(estimate('0.01'), target='garbage').action == 'cannot_evaluate'
    assert exit_for(estimate('-0.30'), stop=None, now=OPENED + 1800).trigger == 'max_hold'
    # An open time that cannot be read is treated as a hold that has run out.
    assert exit_for(estimate('0.01'), opened=float('nan')).trigger == 'max_hold'
    with pytest.raises(ValueError):
        exit_for(estimate('0.01'), now=None)


def test_exit_records_are_json_safe_and_list_every_trigger():
    decision = exit_for(estimate('0.08'), peak=D('0.02'))
    record = json.loads(json.dumps(decision.as_dict(), allow_nan=False))
    assert record['new_peak'] == '0.08' and record['trigger'] == 'take_profit'
    assert [c['check'] for c in record['checks']] == list(ms.EXIT_TRIGGERS)
    assert [c['check'] for c in record['checks'] if c['fired']] == ['take_profit']
