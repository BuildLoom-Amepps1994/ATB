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
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import micro_strategy as ms
import strategy

D = Decimal
START_MS = 1_761_436_800_000          # a 5-minute boundary
NOW = START_MS / 1000 + 86_400.25
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
    """The shape of micro_economics.SizingResult.as_dict() for an eligible 25 USDT cycle."""
    record = dict(symbol='ETH/USDT', eligible=True, code=None, detail='eligible', checks=[{'check': 'budget'}],
                  budget_cap='25', target_budget='25', gross_quote='24.97', fee_reserve='0.03', max_debit='25.00',
                  expected_base='0.2496', expected_fee_base='0.0002496', sellable_base='0.2493',
                  predicted_residual_base='0.0000504', entry_vwap='100.03', break_even_bid='100.24',
                  target_bid='100.16', required_move_bps=required, minimum_feasible_budget='5.03',
                  minimum_feasible_detail='', cost={'taker_fee': '0.001'})
    record.update(changes)
    return record


def features_of(closes, symbol='ETH/USDT'):
    f1, f5 = frames(closes)
    features, reason = ms.compute_features(symbol, f1, f5)
    assert features is not None, reason
    return features


def decide(features, *, symbol='ETH/USDT', regime=None, spread_bps=None, depth_ok=True, data_fresh=True,
           params=PARAMS, gates=None, book=None, sizing_record=None, now=NOW, feature_reason=None):
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
        live[n] = decide(features, now=NOW + n, feature_reason=reason)
    for n, made in live.items():
        # Later: the full history exists. Truncating 1m to n rows and passing the
        # whole 5m frame (future 5m bars included) gives the same decision.
        features, reason = ms.compute_features('ETH/USDT', full_1m.iloc[:n], full_5m)
        again = decide(features, now=NOW + n, feature_reason=reason)
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
    assert proposal['stop_net_bps'] == ms.stop_net_bps(momentum, PARAMS)
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
        if decide(features, now=NOW + n).action == 'enter':
            entries.append(n - 400)
        if decide(features, regime='ranging', now=NOW + n).action == 'enter':
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
            decisions.append(decide(features, sizing_record=sizing(required=required), now=NOW + n,
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
    ('stop_beyond_cap', {'params': {'max_loss_bps': 10.0}}, ms.VOLATILITY),
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


def test_all_failed_checks_are_listed_in_order_and_the_dominant_code_follows_the_priority(momentum):
    layers = [  # (code, change), highest priority first
        (ms.REVIEW, {'gates': {'review_block': True}}),
        (ms.RISK, {'gates': {'risk_block': 'drawdown'}}),
        (ms.NOT_SELECTED, {'gates': {'selected': False}}),
        (ms.CAPACITY, {'gates': {'capacity': False}}),
        (ms.COOLDOWN, {'gates': {'cooldown_remaining_s': 12.5}}),
        (ms.STALE_DATA, {'data_fresh': False}),
        (ms.SPREAD, {'spread_bps': '12'}),
        (ms.BUDGET_MINIMUM, {'sizing': {'eligible': False, 'code': 'BUDGET_BELOW_VENUE_MINIMUM'}}),
        (ms.VOLATILITY, {'params': {'max_loss_bps': 10.0}}),
        (ms.NO_ECONOMIC_EDGE, {'sizing': {'required_move_bps': '200'}}),
    ]
    for start in range(len(layers) + 1):
        active = layers[start:]
        change: dict = {}
        for _, layer in reversed(active):       # higher-priority layers win where they touch the same input
            for key, value in layer.items():
                change[key] = {**change.get(key, {}), **value} if isinstance(value, dict) else value
        decision = scenario(momentum, change)
        if not active:
            assert decision.action == 'enter'
            continue
        assert_abstains(decision, active[0][0])
        assert {code for code, _ in active} <= set(decision.codes)
        order = [check['check'] for check in decision.checks]
        failed = [check['check'] for check in decision.checks if not check['passed']]
        assert failed == [name for name in order if name in failed]   # evaluation order, nothing dropped
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
    assert decide(momentum, now=NOW + 0.001).decision_id != first.decision_id
    assert decide(momentum, now=NOW + 0.0001).decision_id == first.decision_id   # rounded to the millisecond
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


# ---------------------------------------------------------- params, stops
@pytest.mark.parametrize('field,value', [('edge_ratio', float('nan')), ('max_spread_bps', float('inf')),
                                         ('max_vol_bps', True), ('max_hold_s', 0), ('target_net_bps', -1),
                                         ('max_efficiency_range', 1.5), ('uncertainty_floor_bps', -0.1),
                                         ('max_loss_bps', None), ('min_efficiency_trend', '0.3')])
def test_params_refuse_values_that_would_silently_pass_a_gate(field, value):
    with pytest.raises(ValueError):
        dataclasses.replace(PARAMS, **{field: value})


def test_stop_is_twice_atr_floored_at_25_and_never_beyond_the_loss_cap(momentum):
    def stop(atr, cap=80.0):
        return ms.stop_net_bps(dataclasses.replace(momentum, atr_bps=atr),
                               dataclasses.replace(PARAMS, max_loss_bps=cap))
    assert stop(5.0) == 25.0 and stop(20.0) == 40.0 and stop(40.0) == 80.0
    assert stop(40.01) is None                     # a 2 ATR stop the cap cannot hold: too volatile
    assert stop(8.0, cap=20.0) == 20.0             # a cap below the floor wins
    assert stop(float('nan')) is None


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
             target=TARGET, stop=STOP):
    model = depth_model if depth_model is not None else (est or {}).get('depth_model', 'book')
    return ms.evaluate_exit(estimate=est, depth_model=model, target_net=target, stop_net=stop, peak_est_net=peak,
                            opened_wall=opened, max_hold_s=max_hold_s, regime=regime, now=now)


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
