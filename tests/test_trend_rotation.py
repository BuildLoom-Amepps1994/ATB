"""The rotation study's simulator: a stopped coin is not bought back the same day.

Research-lens finding of the 2026-11-04 audit: step 1 sold a stopped coin,
step 2 listed it as a candidate (its previous close was still above the
band) and step 4 bought it back at the same day's open, so every stop was
charged without removing the exposure. Synthetic candles, no data files.
"""

from research import trend_rotation

DAY = trend_rotation.DAY


def _rows(closes, crash_day=None, crash_low=None, crash_close=None):
    rows = []
    for day, close in enumerate(closes):
        open_, high, low = (closes[day - 1] if day else close), close, close
        if day == crash_day:
            low, close = crash_low, crash_close
        rows.append([day * DAY, open_, max(open_, high, close), min(low, open_, close), close, 1_000_000.0])
    return rows


def _data(rows):
    return {'AAA/USDT': trend_rotation.prepare(rows)}


def test_a_stopped_coin_is_not_bought_back_at_the_same_days_open():
    closes = [100 + 200 * day / 299 for day in range(300)] + [290.0]
    data = _data(_rows(closes, crash_day=300, crash_low=120.0, crash_close=290.0))
    result = trend_rotation.simulate(data, ['AAA/USDT'], rotation=None, stop=0.25, size='fixed', start=None, end=301 * DAY)
    assert result['trades'] == 1 and result['wins'] == 0           # the stop fill, and nothing re-bought that day
    assert result['mean_trade_pct'] < -20                           # -25 % plus costs
    assert trend_rotation.PROTOCOL['version'] == 'trend-rotation-v2'


def test_a_coin_that_holds_its_trend_is_kept_and_sold_at_the_end():
    closes = [100 + 200 * day / 299 for day in range(300)] + [305.0]
    data = _data(_rows(closes))
    result = trend_rotation.simulate(data, ['AAA/USDT'], rotation=None, stop=0.25, size='fixed', start=None, end=301 * DAY)
    assert result['trades'] == 1 and result['wins'] == 1 and result['return_pct'] > 0
