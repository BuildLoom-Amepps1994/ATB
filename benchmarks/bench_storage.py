"""Cost of one storage call, and storage calls per legacy path. No exchange is contacted.

Run from the repository root:
    python benchmarks/bench_storage.py [--calls N]

Like ``bench_latency.py``, the file runs unchanged in a checkout of the
previous release, so the same command gives "before" and "after" figures.

* ``schema``       the rows of ``sqlite_master`` (tables, indexes and SQLite's
                   own rows): every new connection parses all of them, in its
                   ``PRAGMA synchronous``.
* ``call_ms``      the median of ``--calls`` trivial calls (default 500), each a
                   new ``storage.connection()`` and one ``SELECT 1``, after 50
                   discarded calls.
* ``connections``  ``storage.connection`` calls per protective tick (1 and 5
                   positions, 0 ms round trip) and per warm 8-symbol scan,
                   counted by wrapping it, averaged over 20 calls after 3
                   discarded ones, with the fixtures of ``bench_latency.py``.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_latency  # noqa: E402  (sets the isolated data folder and keyring before storage is imported)
import storage  # noqa: E402


def schema() -> dict:
    with storage.connection() as db:
        kinds = db.execute("SELECT type, count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
                           "GROUP BY type").fetchall()
        rows = db.execute('SELECT count(*) FROM sqlite_master').fetchone()[0]
    return {'sqlite_master_rows': rows, 'named': {kind: count for kind, count in kinds}}


def call_ms(calls: int) -> float:
    def call():
        with storage.connection() as db:
            db.execute('SELECT 1').fetchone()
    for _ in range(50):
        call()
    samples = []
    for _ in range(calls):
        start = time.perf_counter()
        call()
        samples.append((time.perf_counter() - start) * 1000)
    return round(statistics.median(samples), 3)


def connections() -> dict:
    counted, real = [0], storage.connection

    @contextlib.contextmanager
    def counting(*args, **kwargs):
        counted[0] += 1
        with real(*args, **kwargs) as db:
            yield db
    found, label, timed = {}, [''], bench_latency.timed

    def counting_timed(fn, repeat, **kwargs):
        for _ in range(3):
            fn()
        counted[0] = 0
        for _ in range(20):
            fn()
        found[label[0]] = counted[0] / 20
        return timed(fn, 1, **kwargs)
    storage.connection, bench_latency.timed = counting, counting_timed
    try:
        for positions in (1, 5):
            label[0] = f'protective_tick_{positions}_positions'
            bench_latency.monitor_tick(positions, 0.0, 1, stream=False)
        label[0] = 'warm_scan'
        bench_latency.scan_warm(1, rtt=0.0)
    finally:
        storage.connection, bench_latency.timed = real, timed
    return found


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--calls', type=int, default=500)
    args = parser.parse_args()
    storage.init_db()
    print(json.dumps({'schema': schema(), 'call_ms': call_ms(args.calls), 'connections': connections()}))


if __name__ == '__main__':
    main()
