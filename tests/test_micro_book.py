"""Validated order-book snapshots and the per-symbol book tracker (brief test 23 and related)."""
import dataclasses
import json
import sys
import threading
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import micro_book
from micro_book import BookError, BookSnapshot, BookTracker

D = Decimal
WALL = 1_760_000_000.0          # a receipt time on this PC's wall clock
WALL_MS = int(WALL * 1000)
MONO = 5_000.0                  # the matching monotonic reading


def depth(bids=None, asks=None, *, nonce=None, timestamp=None, symbol='ETH/USDT'):
    """A CCXT order book as fetch_order_book returns it."""
    return {'symbol': symbol,
            'bids': [['2000.10', '1.5'], ['2000.00', '2']] if bids is None else bids,
            'asks': [['2000.20', '0.75'], ['2000.50', '3']] if asks is None else asks,
            'timestamp': timestamp, 'datetime': None, 'nonce': nonce}


def book(raw=None, *, wall=WALL, mono=MONO, offset=0.0, max_levels=50, symbol='ETH/USDT', exchange='mexc'):
    return micro_book.from_ccxt_order_book(depth(symbol=symbol) if raw is None else raw, exchange=exchange,
                                           symbol=symbol, received_wall=wall, received_mono=mono,
                                           clock_offset_s=offset, max_levels=max_levels)


def seq(n, *, wall=WALL, mono=MONO, symbol='ETH/USDT', bid='2000.10'):
    return book(depth([[bid, '1']], [['2000.20', '1']], nonce=n, symbol=symbol), wall=wall, mono=mono, symbol=symbol)


def stamped(ts_ms, *, wall=WALL, mono=MONO, symbol='ETH/USDT'):
    return book(depth(timestamp=ts_ms, symbol=symbol), wall=wall, mono=mono, symbol=symbol)


def ticker(**fields):
    base = {'symbol': 'ETH/USDT', 'bid': 2000.1, 'ask': 2000.2, 'bidVolume': None, 'askVolume': None,
            'timestamp': None, '_received_at': WALL}
    base.update(fields)
    return base


def from_ticker(raw, **kwargs):
    kwargs.setdefault('received_wall', WALL)
    kwargs.setdefault('received_mono', MONO)
    return micro_book.from_ticker(raw, exchange='mexc', symbol='ETH/USDT', **kwargs)


def refused(code, fn, *args, **kwargs):
    with pytest.raises(BookError) as raised:
        fn(*args, **kwargs)
    assert raised.value.code == code, (raised.value.code, str(raised.value))
    return raised.value


# ------------------------------------------------------------- snapshots
def test_depth_snapshot_is_exact_best_first_and_known_depth():
    snapshot = book(depth([[2000.1, 1.5], ['2000.00', '2']], [['2000.20', 0.75], [2000.5, '3']], nonce=744267132))
    assert snapshot.bids == ((D('2000.1'), D('1.5')), (D('2000.00'), D('2')))
    assert snapshot.asks == ((D('2000.20'), D('0.75')), (D('2000.5'), D('3')))
    assert all(isinstance(x, Decimal) for level in snapshot.bids + snapshot.asks for x in level)
    assert snapshot.best_bid == D('2000.1') and snapshot.best_ask == D('2000.20')
    assert snapshot.mid == D('2000.15')
    assert abs(snapshot.spread_bps - D('0.1') / D('2000.15') * 10000) < D('1e-20')     # (ask - bid) / mid
    assert snapshot.depth_known is True and snapshot.source == 'rest_depth' and snapshot.level_count == 2
    assert snapshot.venue_sequence == 744267132 and snapshot.venue_timestamp_ms is None
    assert snapshot.generation == 0        # assigned only by a tracker


def test_mexc_depth_response_through_ccxts_own_parser_is_accepted_with_its_update_id():
    import ccxt
    client = ccxt.mexc()                   # no markets loaded, no request made: parsing only
    response = {'lastUpdateId': '744267132',
                'bids': [['40838.50', '0.387864'], ['40837.95', '0.008400']],
                'asks': [['40838.61', '6.544908'], ['40838.88', '0.498000']]}
    parsed = client.parse_order_book(response, 'BTC/USDT', client.safe_integer(response, 'timestamp'))
    parsed['nonce'] = client.safe_integer(response, 'lastUpdateId')
    snapshot = micro_book.from_ccxt_order_book(parsed, exchange='mexc', symbol='BTC/USDT',
                                               received_wall=WALL, received_mono=MONO)
    assert snapshot.venue_sequence == 744267132 and snapshot.venue_timestamp_ms is None
    assert snapshot.best_bid == D('40838.5') and snapshot.best_ask == D('40838.61')


@pytest.mark.parametrize('bids,asks', [
    ([['100', '1']], [['99', '1']]),        # crossed
    ([['100', '1']], [['100', '1']]),       # locked: no executable spread either
    ([['100.0', '1']], [['100.00', '1']]),  # locked, written differently
])
def test_crossed_and_locked_books_are_refused(bids, asks):
    refused(micro_book.BOOK_CROSSED, book, depth(bids, asks))


@pytest.mark.parametrize('bids,asks', [([], [['1', '1']]), ([['1', '1']], []), (None, [['1', '1']])])
def test_an_empty_or_missing_side_is_refused(bids, asks):
    raw = depth([['1', '1']], [['2', '1']])
    raw['bids'], raw['asks'] = bids, asks
    refused(micro_book.BOOK_EMPTY, book, raw)


BAD_NUMBERS = [float('nan'), float('inf'), float('-inf'), 'nan', 'Infinity', -1, '-0.5', 0, '0', 0.0, True, False,
               'garbage', '', '1,5', '1e400', '1e20', 10 ** 20, [], {}, object()]


@pytest.mark.parametrize('bad', BAD_NUMBERS)
@pytest.mark.parametrize('position', ['price', 'quantity'])
@pytest.mark.parametrize('side', ['bids', 'asks'])
def test_nonfinite_negative_zero_bool_and_garbage_levels_are_refused(bad, position, side):
    raw = depth([['100', '1'], ['99', '1']], [['101', '1'], ['102', '1']])
    level = raw[side][1]
    level[0 if position == 'price' else 1] = bad
    refused(micro_book.BOOK_INVALID_LEVEL, book, raw)


@pytest.mark.parametrize('level', [['100'], '100', 100, None, {'price': '100', 'amount': '1'}, ['100', None]])
def test_malformed_level_shapes_and_missing_depth_quantities_are_refused(level):
    refused(micro_book.BOOK_INVALID_LEVEL, book, depth([['101', '1'], level], [['102', '1']]))


def test_a_side_that_is_not_a_list_is_refused():
    refused(micro_book.BOOK_INVALID_LEVEL, book, depth('100,1', [['102', '1']]))


def test_error_messages_never_echo_the_raw_value():
    secret = 'sk_live_' + 'x' * 30
    error = refused(micro_book.BOOK_INVALID_LEVEL, book, depth([[secret, '1']], [['102', '1']]))
    assert secret not in str(error)


@pytest.mark.parametrize('bids,asks', [
    ([['99', '1'], ['100', '1']], [['101', '1']]),                  # bids ascending
    ([['100', '1']], [['102', '1'], ['101', '1']]),                 # asks descending
    ([['100', '1'], ['99', '1'], ['100', '1']], [['101', '1']]),    # a repeated price that is not adjacent
])
def test_unsorted_levels_are_refused_not_resorted(bids, asks):
    refused(micro_book.BOOK_UNSORTED, book, depth(bids, asks))


def test_equal_adjacent_prices_are_merged_by_summing_never_dropped():
    snapshot = book(depth([['100', '0.1'], ['100.0', '0.2'], ['99', '1']],
                          [['101', '1'], ['101', '2'], ['101.00', '3'], ['102', '1']]))
    assert snapshot.bids == ((D('100'), D('0.3')), (D('99'), D('1')))
    assert snapshot.asks == ((D('101'), D('6')), (D('102'), D('1')))
    total_in = D('0.1') + D('0.2') + D('1') + D('1') + D('2') + D('3') + D('1')
    assert sum(q for _, q in snapshot.bids + snapshot.asks) == total_in


def test_duplicates_of_the_last_kept_level_are_merged_even_at_the_level_limit():
    snapshot = book(depth([['100', '1'], ['99', '1'], ['99', '4'], ['98', '1']], [['101', '1']]), max_levels=2)
    assert snapshot.bids == ((D('100'), D('1')), (D('99'), D('5')))


def test_merged_quantity_beyond_the_money_bound_is_refused():
    huge = str(10 ** 15)
    refused(micro_book.BOOK_INVALID_LEVEL, book, depth([['100', huge], ['100', huge]], [['101', '1']]))


def test_max_levels_is_honoured_and_keeps_the_best_levels():
    bids = [[str(1000 - i), '1'] for i in range(100)]
    asks = [[str(1001 + i), '1'] for i in range(100)]
    snapshot = book(depth(bids, asks), max_levels=7)
    assert len(snapshot.bids) == len(snapshot.asks) == 7 == snapshot.level_count
    assert snapshot.bids[0][0] == D('1000') and snapshot.bids[-1][0] == D('994')
    assert snapshot.asks[0][0] == D('1001') and snapshot.asks[-1][0] == D('1007')
    for bad in (0, -1, True, 1.5, micro_book.MAX_LEVELS + 1):
        with pytest.raises(ValueError):
            book(depth(bids, asks), max_levels=bad)


class Poison:
    """A level that fails the test if anything looks at it."""
    def __getitem__(self, index):
        raise AssertionError('a level beyond the kept depth was read')
    def __len__(self):
        raise AssertionError('a level beyond the kept depth was read')


def test_levels_beyond_the_kept_depth_are_never_parsed():
    bids = [[str(1000 - i), '1'] for i in range(11)] + [Poison()] * (micro_book.MAX_RAW_LEVELS - 11)
    asks = [[str(1001 + i), '1'] for i in range(11)] + [Poison()] * (micro_book.MAX_RAW_LEVELS - 11)
    snapshot = book(depth(bids, asks), max_levels=10)
    assert len(snapshot.bids) == len(snapshot.asks) == 10


def test_a_malicious_number_of_levels_is_refused_without_parsing_them():
    level = ['100', '1']
    flood = [level] * 1_000_000
    refused(micro_book.BOOK_INVALID_LEVEL, book, depth(flood, [['101', '1']]))
    refused(micro_book.BOOK_INVALID_LEVEL, book, depth([level], [['101', '1']] * (micro_book.MAX_RAW_LEVELS + 1)))
    # the same bound holds for a snapshot built directly
    with pytest.raises(BookError) as raised:
        BookSnapshot(exchange='mexc', symbol='ETH/USDT', bids=((D(1), D(1)),) * (micro_book.MAX_LEVELS + 1),
                     asks=((D(2), D(1)),), source='fixture', depth_known=True, venue_timestamp_ms=None,
                     venue_sequence=None, received_wall=WALL, received_mono=MONO)
    assert raised.value.code == micro_book.BOOK_INVALID_LEVEL


def test_a_full_venue_depth_is_accepted_and_parsing_stops_at_the_limit():
    bids = [[str(10000 - i), '1'] for i in range(micro_book.MAX_RAW_LEVELS)]
    asks = [[str(10001 + i), '1'] for i in range(micro_book.MAX_RAW_LEVELS)]
    assert len(book(depth(bids, asks), max_levels=micro_book.MAX_LEVELS).bids) == micro_book.MAX_RAW_LEVELS
    assert book(depth(bids, asks), max_levels=20).level_count == 20


@pytest.mark.parametrize('lead_s,offset,ok', [
    (0, 0.0, True), (5.0, 0.0, True), (5.5, 0.0, False), (60, 0.0, False),
    (10, -10.0, True),      # the venue clock runs 10 s ahead of this PC (CCXT timeDifference -10000 ms)
    (4, 2.0, False),        # this PC runs 2 s ahead: the venue's 4 s lead becomes 6 s
    (-30, 0.0, True),       # an old venue timestamp is not a future one
])
def test_future_venue_timestamps_are_refused_after_the_clock_offset(lead_s, offset, ok):
    raw = depth(timestamp=WALL_MS + int(lead_s * 1000))
    if ok:
        assert book(raw, offset=offset).venue_timestamp_ms == raw['timestamp']
    else:
        refused(micro_book.BOOK_FUTURE, book, raw, offset=offset)
    tick = ticker(timestamp=WALL_MS + int(lead_s * 1000))
    if ok:
        assert from_ticker(tick, clock_offset_s=offset).venue_timestamp_ms == tick['timestamp']
    else:
        refused(micro_book.BOOK_FUTURE, from_ticker, tick, clock_offset_s=offset)


@pytest.mark.parametrize('field,value', [('timestamp', 'yesterday'), ('timestamp', -5), ('timestamp', 0),
                                         ('timestamp', True), ('timestamp', float('nan')), ('timestamp', 1.5),
                                         ('nonce', -1), ('nonce', '12'), ('nonce', 2 ** 70), ('nonce', False)])
def test_malformed_venue_timestamp_or_sequence_makes_the_response_untrusted(field, value):
    raw = depth()
    raw[field] = value
    refused(micro_book.BOOK_INVALID_LEVEL, book, raw)


def test_a_response_for_another_symbol_or_no_response_is_missing():
    refused(micro_book.BOOK_MISSING, book, depth(symbol='BTC/USDT'), symbol='ETH/USDT')
    refused(micro_book.BOOK_MISSING, micro_book.from_ccxt_order_book, None, exchange='mexc', symbol='ETH/USDT',
            received_wall=WALL, received_mono=MONO)
    refused(micro_book.BOOK_MISSING, from_ticker, ticker(symbol='BTC/USDT'))


def test_as_dict_uses_decimal_strings_and_caps_the_levels():
    snapshot = book(depth([[str(100 - i), '0.1'] for i in range(10)], [[str(101 + i), 1.25] for i in range(10)],
                          nonce=7))
    shown = snapshot.as_dict(levels=3)
    assert json.loads(json.dumps(shown)) == shown
    for key in ('best_bid', 'best_ask', 'mid', 'spread_bps'):
        assert isinstance(shown[key], str) and D(shown[key]) > 0
    assert shown['bids'] == [['100', '0.1'], ['99', '0.1'], ['98', '0.1']]
    assert shown['asks'][0] == ['101', '1.25'] and len(shown['asks']) == 3
    assert shown['bid_levels'] == 10 and shown['venue_sequence'] == 7 and shown['depth_known'] is True
    assert D(shown['spread_bps']) >= snapshot.spread_bps            # rounded for display, never narrowed
    assert snapshot.as_dict(levels=0)['bids'] == []
    with pytest.raises(ValueError):
        snapshot.as_dict(levels=-1)


def test_snapshot_is_immutable_and_its_invariants_hold_for_direct_construction():
    snapshot = book()
    with pytest.raises(Exception):
        snapshot.bids = ()
    common = dict(exchange='mexc', symbol='ETH/USDT', venue_timestamp_ms=None, venue_sequence=None,
                  received_wall=WALL, received_mono=MONO)
    # a depth source must know every quantity, and depth_known cannot be claimed with an unknown one
    with pytest.raises(BookError) as raised:
        BookSnapshot(bids=((D(1), None),), asks=((D(2), D(1)),), source='rest_depth', depth_known=False, **common)
    assert raised.value.code == micro_book.BOOK_INVALID_LEVEL
    with pytest.raises(BookError) as raised:
        BookSnapshot(bids=((D(1), None),), asks=((D(2), D(1)),), source='fixture', depth_known=True, **common)
    assert raised.value.code == micro_book.BOOK_INVALID_LEVEL
    with pytest.raises(BookError) as raised:   # direct construction does not merge: equal neighbours are a defect
        BookSnapshot(bids=((D(1), D(1)), (D(1), D(1))), asks=((D(2), D(1)),), source='fixture', depth_known=True,
                     **common)
    assert raised.value.code == micro_book.BOOK_UNSORTED
    with pytest.raises(ValueError):
        BookSnapshot(bids=((D(1), D(1)),), asks=((D(2), D(1)),), source='websocket', depth_known=True, **common)
    unknown = BookSnapshot(bids=[(D(1), None)], asks=[(D(2), None)], source='fixture', depth_known=False, **common)
    assert unknown.bids == ((D(1), None),) and isinstance(unknown.bids, tuple)


# --------------------------------------------------------------- tickers
def test_ticker_without_volumes_is_top_of_book_with_unknown_depth():
    snapshot = from_ticker(ticker(_stream_sequence=99, _source='stream'), source='stream_ticker')
    assert snapshot.depth_known is False
    assert snapshot.bids == ((D('2000.1'), None),) and snapshot.asks == ((D('2000.2'), None),)
    assert snapshot.venue_sequence is None      # the feed's local counter is not venue evidence
    assert snapshot.source == 'stream_ticker'


def test_ticker_with_volumes_is_a_known_one_level_depth():
    snapshot = from_ticker(ticker(bidVolume='0.387864', askVolume=6.544908, timestamp=WALL_MS - 200))
    assert snapshot.depth_known is True and snapshot.level_count == 1
    assert snapshot.bids == ((D('2000.1'), D('0.387864')),) and snapshot.asks == ((D('2000.2'), D('6.544908')),)
    assert snapshot.venue_timestamp_ms == WALL_MS - 200


@pytest.mark.parametrize('volumes', [{'bidVolume': '1'}, {'askVolume': 2.5}, {'bidVolume': 0, 'askVolume': '1'},
                                     {'bidVolume': '', 'askVolume': '1'}])
def test_ticker_with_one_unknown_volume_does_not_claim_depth(volumes):
    snapshot = from_ticker(ticker(**volumes))
    assert snapshot.depth_known is False
    known = [q for _, q in snapshot.bids + snapshot.asks if q is not None]
    assert all(q > 0 for q in known)            # an unknown volume is None, never a zero-quantity level


@pytest.mark.parametrize('field,value,code', [
    ('bid', None, micro_book.BOOK_EMPTY), ('ask', None, micro_book.BOOK_EMPTY),
    ('bid', 'garbage', micro_book.BOOK_INVALID_LEVEL), ('ask', float('nan'), micro_book.BOOK_INVALID_LEVEL),
    ('bid', 0, micro_book.BOOK_INVALID_LEVEL), ('ask', True, micro_book.BOOK_INVALID_LEVEL),
    ('bidVolume', -1, micro_book.BOOK_INVALID_LEVEL), ('askVolume', 'lots', micro_book.BOOK_INVALID_LEVEL),
    ('bid', 2000.2, micro_book.BOOK_CROSSED), ('bid', 2000.3, micro_book.BOOK_CROSSED),
])
def test_bad_tickers_are_refused_with_the_rule(field, value, code):
    refused(code, from_ticker, ticker(**{field: value}))


def test_source_must_match_the_builder():
    with pytest.raises(ValueError):
        from_ticker(ticker(), source='rest_depth')
    with pytest.raises(ValueError):
        micro_book.from_ccxt_order_book(depth(), exchange='mexc', symbol='ETH/USDT', received_wall=WALL,
                                        received_mono=MONO, source='ticker')


# --------------------------------------------------------------- tracker
def test_stale_snapshot_by_monotonic_age_is_not_returned():
    tracker = BookTracker(max_age_s=5)
    stored = tracker.accept(book(mono=100.0))
    assert tracker.latest('ETH/USDT', now_mono=105.0) is stored
    assert tracker.latest('ETH/USDT', now_mono=105.01) is None
    assert tracker.latest('BTC/USDT', now_mono=100.0) is None
    assert tracker.status(106.0)['ETH/USDT']['code'] == micro_book.BOOK_STALE


def test_age_is_monotonic_whatever_the_wall_clock_says():
    tracker = BookTracker(max_age_s=5)
    # received with a wall time an hour old but just now on the monotonic clock: fresh
    tracker.accept(book(wall=WALL - 3600, mono=100.0))
    assert tracker.latest('ETH/USDT', now_mono=101.0) is not None
    # and a now_mono from another clock (here a wall time) can never revive it
    assert tracker.latest('ETH/USDT', now_mono=WALL) is None
    assert book(mono=100.0).age_seconds(10.0) == float('inf')     # now far before receipt: clocks mixed up
    assert book(mono=100.0).age_seconds(99.5) == 0.0               # a reader racing the writer


def test_a_caller_can_tighten_but_not_loosen_the_age_limit():
    tracker = BookTracker(max_age_s=5)
    tracker.accept(book(mono=100.0))
    assert tracker.latest('ETH/USDT', now_mono=103.0, max_age_s=2) is None
    assert tracker.latest('ETH/USDT', now_mono=103.0, max_age_s=60) is not None
    assert tracker.latest('ETH/USDT', now_mono=110.0, max_age_s=60) is None


def test_lower_update_id_is_out_of_order_and_the_stored_book_is_kept():
    tracker = BookTracker(max_age_s=5)
    first = tracker.accept(seq(1000, mono=100.0))
    error = refused(micro_book.BOOK_OUT_OF_ORDER, tracker.accept, seq(999, mono=100.5, bid='1999'))
    assert 'ETH/USDT' in str(error)
    assert tracker.latest('ETH/USDT', now_mono=101.0) is first
    status = tracker.status(101.0)['ETH/USDT']
    assert status['generation'] == 1 and status['sequence'] == 1000 and status['refused'] == 1
    assert status['last_error'].startswith(micro_book.BOOK_OUT_OF_ORDER)
    same = tracker.accept(seq(1000, mono=101.0))      # same update id: same venue state, a newer receipt
    newer = tracker.accept(seq(1001, mono=99.0))     # received earlier, but the venue says it is newer
    assert (same.generation, newer.generation) == (2, 3)
    assert tracker.latest('ETH/USDT', now_mono=101.5) is newer


def test_older_venue_timestamp_without_sequences_is_out_of_order_beyond_one_second():
    tracker = BookTracker(max_age_s=5)
    first = tracker.accept(stamped(WALL_MS, mono=100.0))
    refused(micro_book.BOOK_OUT_OF_ORDER, tracker.accept, stamped(WALL_MS - 1001, mono=100.2))
    assert tracker.latest('ETH/USDT', now_mono=100.5) is first
    jitter = tracker.accept(stamped(WALL_MS - 1000, mono=100.3))     # within the tolerance
    assert jitter.generation == 2


def test_an_interleaved_ticker_cannot_let_an_older_depth_snapshot_back_in():
    tracker = BookTracker(max_age_s=5)
    tracker.accept(seq(1000, mono=100.0))
    top = tracker.accept(from_ticker(ticker(), received_mono=100.1))
    refused(micro_book.BOOK_OUT_OF_ORDER, tracker.accept, seq(990, mono=100.2))
    assert tracker.latest('ETH/USDT', now_mono=100.3) is top


def test_without_shared_venue_evidence_the_earlier_receipt_loses():
    tracker = BookTracker(max_age_s=5)
    tracker.accept(seq(1000, mono=100.0))
    late = from_ticker(ticker(), received_mono=99.9)          # a slow thread delivering an older response
    refused(micro_book.BOOK_OUT_OF_ORDER, tracker.accept, late)
    tracker.accept(from_ticker(ticker(), received_mono=100.1))


def test_wall_clock_rollback_while_monotonic_advances_invalidates_every_symbol():
    tracker = BookTracker(max_age_s=5)
    tracker.accept(seq(10, wall=WALL, mono=100.0, symbol='ETH/USDT'))
    tracker.accept(seq(20, wall=WALL + 1, mono=101.0, symbol='BTC/USDT'))
    tracker.accept(seq(11, wall=WALL + 1.5, mono=101.4, symbol='ETH/USDT'))   # wall jitter under 1 s is fine
    refused(micro_book.CLOCK_ROLLBACK, tracker.accept, seq(21, wall=WALL - 30, mono=102.0, symbol='BTC/USDT'))
    assert tracker.latest('ETH/USDT', now_mono=102.1) is None
    assert tracker.latest('BTC/USDT', now_mono=102.1) is None
    status = tracker.status(102.1)
    assert all(row['state'] == 'invalidated' and 'stepped back' in row['invalidated'] for row in status.values())
    assert tracker.rollbacks == 1
    # a response received before the rollback was detected cannot slip in afterwards
    refused(micro_book.CLOCK_ROLLBACK, tracker.accept, seq(12, wall=WALL + 1.6, mono=101.9, symbol='ETH/USDT'))
    # new snapshots on the new clock rebuild the state, and generations keep counting
    rebuilt = tracker.accept(seq(22, wall=WALL - 29, mono=103.0, symbol='BTC/USDT'))
    assert rebuilt.generation == 2 and tracker.latest('BTC/USDT', now_mono=103.5) is rebuilt
    assert tracker.accept(seq(13, wall=WALL - 28.5, mono=103.2, symbol='ETH/USDT')).generation == 3


def test_wall_clock_behind_an_earlier_receipt_without_monotonic_progress_is_not_a_rollback():
    tracker = BookTracker(max_age_s=5)
    tracker.accept(seq(10, wall=WALL, mono=100.0))
    # an older response handed over late: wall and monotonic both earlier, venue says newer
    tracker.accept(seq(11, wall=WALL - 5, mono=95.0))
    assert tracker.rollbacks == 0


def test_invalidate_on_reconnect_stops_serving_and_starts_the_evidence_over():
    tracker = BookTracker(max_age_s=5)
    tracker.accept(seq(1000, mono=100.0))
    tracker.accept(seq(5, mono=100.0, symbol='BTC/USDT'))
    tracker.invalidate('ETH/USDT', reason='stream reconnect')
    assert tracker.latest('ETH/USDT', now_mono=100.1) is None
    assert tracker.latest('BTC/USDT', now_mono=100.1) is not None
    assert tracker.status(100.1)['ETH/USDT']['invalidated'] == 'stream reconnect'
    # a venue that restarted its counters cannot lock the symbol out after a reconnect
    again = tracker.accept(seq(3, mono=100.5))
    assert again.generation == 2 and tracker.latest('ETH/USDT', now_mono=100.6) is again
    tracker.invalidate()
    assert tracker.latest('ETH/USDT', now_mono=100.6) is None and tracker.latest('BTC/USDT', now_mono=100.6) is None
    tracker.invalidate('NEVER/SEEN')        # nothing to invalidate is not an error


def test_generation_is_a_local_count_of_accepted_snapshots_never_venue_evidence():
    tracker = BookTracker(max_age_s=5)
    forged = dataclasses.replace(seq(500_000, mono=100.0), generation=999)
    first = tracker.accept(forged)
    assert first.generation == 1 and first.venue_sequence == 500_000     # the incoming generation is ignored
    refused(micro_book.BOOK_OUT_OF_ORDER, tracker.accept, seq(499_999, mono=100.1))
    second = tracker.accept(seq(500_001, mono=100.2))
    other = tracker.accept(seq(7, mono=100.2, symbol='BTC/USDT'))
    assert second.generation == 2 and other.generation == 1              # per symbol, refusals do not count
    assert book().generation == 0 and from_ticker(ticker()).generation == 0
    assert 'NOT venue evidence' in micro_book.__doc__ and 'local' in micro_book.__doc__


def test_tracker_future_check_uses_the_snapshots_clock_offset_and_its_own_tolerance():
    strict = BookTracker(max_age_s=5, future_tolerance_s=1.0)
    ahead = book(depth(timestamp=WALL_MS + 3000), mono=100.0)      # 3 s ahead: within the builder's 5 s
    refused(micro_book.BOOK_FUTURE, strict.accept, ahead)
    assert strict.latest('ETH/USDT', now_mono=100.0) is None
    corrected = book(depth(timestamp=WALL_MS + 3000), mono=100.1, offset=-3.0)
    assert strict.accept(corrected).clock_offset_s == -3.0


def test_status_reports_each_symbol_and_is_json_safe():
    tracker = BookTracker(max_age_s=5)
    tracker.accept(seq(42, mono=100.0))
    tracker.accept(seq(7, mono=90.0, symbol='BTC/USDT'))
    with pytest.raises(BookError):
        tracker.accept(book(depth([['1', '1']], [['1', '1']], symbol='SOL/USDT'), symbol='SOL/USDT'))
    status = tracker.status(101.0)
    assert json.loads(json.dumps(status)) == status
    eth = status['ETH/USDT']
    assert {'generation', 'age_s', 'source', 'sequence', 'last_error'} <= set(eth)
    assert eth == {**eth, 'generation': 1, 'age_s': 1.0, 'source': 'rest_depth', 'sequence': 42, 'state': 'fresh',
                   'code': None}
    assert status['BTC/USDT']['state'] == 'stale' and status['BTC/USDT']['code'] == micro_book.BOOK_STALE
    # a book that never validated never reaches the tracker, so it has no row
    assert 'SOL/USDT' not in status


def test_a_tracker_holds_one_exchange():
    tracker = BookTracker(max_age_s=5)
    tracker.accept(book(mono=100.0))
    with pytest.raises(ValueError):
        tracker.accept(book(mono=100.1, exchange='kraken'))
    with pytest.raises(BookError) as raised:
        tracker.accept(None)
    assert raised.value.code == micro_book.BOOK_MISSING
    for bad in (0, -1, float('nan'), True):
        with pytest.raises(ValueError):
            BookTracker(max_age_s=bad)


def test_concurrent_accepts_assign_unique_generations_and_keep_the_newest_sequence():
    tracker = BookTracker(max_age_s=60)
    snapshots = [seq(n, mono=100.0 + n / 1000) for n in range(1, 201)]
    accepted, errors = [], []

    def worker(chunk):
        for snapshot in chunk:
            try:
                accepted.append(tracker.accept(snapshot))
            except BookError as exc:
                errors.append(exc.code)

    threads = [threading.Thread(target=worker, args=(snapshots[i::4],)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert set(errors) <= {micro_book.BOOK_OUT_OF_ORDER}
    assert len(accepted) + len(errors) == 200
    assert sorted(s.generation for s in accepted) == list(range(1, len(accepted) + 1))
    assert tracker.latest('ETH/USDT', now_mono=100.3).venue_sequence == 200
