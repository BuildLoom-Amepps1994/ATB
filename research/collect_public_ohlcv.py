"""Collect public 1m candles (and optionally order-book snapshots) for micro-mode research.

Run on a machine that can reach the exchange's PUBLIC market-data endpoints:

    python -m research.collect_public_ohlcv --exchange mexc --symbols ETH/USDT,BTC/USDT --days 14 --out research/data

Public endpoints only. This script never reads or accepts an API key, never
creates an authenticated client and never sends an order; it uses the same
ccxt version as the application, with its rate limiter enabled and no extra
retries. It does not route around regional restrictions: if the exchange
refuses this location, it stops and says so.

Each pair is written as ``<BASE>_<QUOTE>_1m.json``:
``{"provenance": {...}, "market": {...}, "ohlcv": [[ms, o, h, l, c, v], ...]}``.
The provenance records the venue, endpoint method, retrieval UTC, ccxt
version, requested and returned ranges, gaps found, the open-bar policy (the
still-forming bar is dropped) and the SHA-256 of the candle rows. Today's
listing is not a point-in-time universe: a pair delisted earlier is simply
absent, which is survivor bias the report must state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def collect(exchange_name: str, symbols: list[str], days: int, out: Path, *, book_snapshots: int = 0) -> list[dict]:
    import ccxt
    if exchange_name not in ('mexc', 'kraken'):
        raise SystemExit('Supported exchanges: mexc, kraken')
    client = getattr(ccxt, exchange_name)({'enableRateLimit': True, 'timeout': 20000,
                                           'options': {'defaultType': 'spot'}})
    client.load_markets()
    out.mkdir(parents=True, exist_ok=True)
    summaries = []
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 86_400_000
    for symbol in symbols:
        if symbol not in client.markets:
            print(f'{symbol}: not listed on {exchange_name}; skipped', file=sys.stderr)
            continue
        rows, since = [], start_ms
        while since < now_ms:
            batch = client.fetch_ohlcv(symbol, '1m', since=since, limit=1000)
            if not batch:
                break
            rows.extend(batch)
            since = int(batch[-1][0]) + 60_000
            if len(batch) < 2:
                break
        cutoff = int(time.time() * 1000)
        clean = sorted({int(r[0]): r for r in rows if int(r[0]) + 60_000 <= cutoff}.values(), key=lambda r: r[0])
        gaps = [(a[0], b[0]) for a, b in zip(clean, clean[1:]) if b[0] - a[0] != 60_000]
        books = []
        for _ in range(book_snapshots):
            book = client.fetch_order_book(symbol, 20)
            books.append({'received_ms': int(time.time() * 1000), 'timestamp': book.get('timestamp'),
                          'nonce': book.get('nonce'), 'bids': book.get('bids'), 'asks': book.get('asks')})
        digest = hashlib.sha256(json.dumps(clean).encode()).hexdigest()
        provenance = {'venue': exchange_name, 'method': 'fetch_ohlcv (public)', 'timeframe': '1m',
                      'retrieved_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                      'ccxt_version': ccxt.__version__, 'requested_from_ms': start_ms, 'requested_to_ms': now_ms,
                      'returned_from_ms': clean[0][0] if clean else None, 'returned_to_ms': clean[-1][0] if clean else None,
                      'bars': len(clean), 'gaps': gaps[:200], 'gap_count': len(gaps),
                      'open_bar_policy': 'the still-forming bar is dropped', 'rows_sha256': digest,
                      'book_snapshots': len(books), 'survivorship': 'current listing only'}
        market = client.markets[symbol]
        payload = {'provenance': provenance, 'market': {k: market.get(k) for k in (
            'id', 'symbol', 'base', 'quote', 'type', 'spot', 'active', 'taker', 'maker', 'precision', 'limits', 'info')},
            'ohlcv': clean, 'books': books}
        name = symbol.replace('/', '_') + '_1m.json'
        (out / name).write_text(json.dumps(payload) + '\n', encoding='utf-8')
        summaries.append({'symbol': symbol, 'file': name, 'bars': len(clean), 'gaps': len(gaps), 'sha256': digest})
        print(f'{symbol}: {len(clean)} bars, {len(gaps)} gaps -> {name}')
    return summaries


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--exchange', default='mexc')
    parser.add_argument('--symbols', required=True, help='comma-separated, e.g. ETH/USDT,BTC/USDT')
    parser.add_argument('--days', type=int, default=14)
    parser.add_argument('--books', type=int, default=0, help='order-book snapshots per pair (public depth endpoint)')
    parser.add_argument('--out', default='research/data')
    args = parser.parse_args(argv)
    symbols = [s.strip().upper() for s in args.symbols.split(',') if s.strip()]
    collect(args.exchange, symbols, max(1, min(args.days, 60)), Path(args.out), book_snapshots=max(0, min(args.books, 50)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
