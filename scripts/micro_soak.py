#!/usr/bin/env python3
"""Opt-in wall-clock PAPER soak of the micro mode, in a NEW, isolated profile.

Run it by hand; nothing schedules it and it installs nothing:

    python scripts/micro_soak.py --data-dir D:\\soak\\micro-1 --hours 24 --pairs BTC/USDT,ETH/USDT
    python scripts/micro_soak.py --data-dir D:\\soak\\micro-2 --hours 24 --pairs BTC/USDT,ETH/USDT --streaming
    python scripts/micro_soak.py --data-dir /tmp/soak-fixture --minutes 20 --fixture      # offline, synthetic

Isolation comes first. Before any application module is imported the script
refuses a data folder that already holds files or overlaps the normal
CryptoPilot profile (or a profile the calling shell names in
``CRYPTOPILOT_DATA_DIR``), sets ``CRYPTOPILOT_DATA_DIR`` to the new folder, selects
the null keyring backend, turns update checks off and replaces the credential
loader with one that returns nothing. So no saved key, setting, portfolio or
log of a normal installation can be read, and no account call can be made:
the engine runs in PAPER with public market data only, and the micro mode
refuses LIVE at every boundary. No web server is started (no port is taken).

Every ``--sample-seconds`` one JSON line goes to ``<data-dir>/soak/samples.jsonl``:
wall and monotonic time, process memory and threads, engine state, cycle
counts by state, each pair's waiting code and book age, and the micro latency
percentiles. A wall-clock gap much larger than the sample interval (sleep,
hibernation, a stalled process) is recorded as a pause. At the end
``<data-dir>/soak/summary.json`` records the requested and actual duration,
platform, interpreter, source commit and file hashes, the faults seen, the
final results and what the run did not test. Ctrl+C ends a run early and is
recorded as such; the engine is stopped gracefully either way.

``--fixture`` runs the same engine against the synthetic markets of
``research/micro_fixtures.py`` on the real clock (a mechanics market that
completes cycles, plus flat markets). It exercises the real loop, threads and
timing, never a real market; its results say nothing about profitability.
It needs no network, so it also refuses every remote connection and proxy
(``scripts/offline_net.py``).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_MODULES = ('storage', 'trader', 'credentials', 'micro', 'app')


def normal_profiles() -> list[tuple[Path, str]]:
    """The default profile, and the one an inherited CRYPTOPILOT_DATA_DIR points the app at."""
    base = os.environ.get('LOCALAPPDATA') or str(Path.home() / '.cryptopilot')
    profiles = [((Path(base) / 'CryptoPilot').expanduser().resolve(), 'the normal CryptoPilot profile')]
    inherited = os.environ.get('CRYPTOPILOT_DATA_DIR')
    if inherited:
        profiles.append((Path(inherited).expanduser().resolve(),
                         'the CryptoPilot profile that CRYPTOPILOT_DATA_DIR names'))
    return profiles


def isolate(data_dir: Path) -> Path:
    """Refuse an unsafe folder, then point every application path at the new one."""
    loaded = [name for name in APP_MODULES if name in sys.modules]
    if loaded:
        raise SystemExit(f'refusing: application modules were imported before isolation: {loaded}')
    data_dir = data_dir.expanduser().resolve()
    for profile, what in normal_profiles():
        if data_dir == profile or profile.is_relative_to(data_dir) or data_dir.is_relative_to(profile):
            raise SystemExit(f'refusing {data_dir}: it overlaps {what} {profile}')
    if data_dir.exists() and (not data_dir.is_dir() or any(data_dir.iterdir())):
        raise SystemExit(f'refusing {data_dir}: a soak needs a NEW, empty folder')
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ['CRYPTOPILOT_DATA_DIR'] = str(data_dir)
    os.environ['PYTHON_KEYRING_BACKEND'] = 'keyring.backends.null.Keyring'
    os.environ['CRYPTOPILOT_NO_UPDATE_CHECK'] = '1'
    os.environ['CRYPTOPILOT_NO_BROWSER'] = '1'
    return data_dir


def source_identity() -> dict:
    """The application, research and harness files, and the commit when git can say (else None)."""
    files = sorted([*ROOT.glob('*.py'), *(ROOT / 'static').glob('*'), *(ROOT / 'research').glob('*.py'),
                    *(ROOT / 'scripts').glob('*.py')])
    digest = hashlib.sha256()
    for path in files:
        if path.is_file():
            digest.update(path.relative_to(ROOT).as_posix().encode() + b'\0' + path.read_bytes() + b'\0')
    identity = {'files': len(files), 'files_sha256': digest.hexdigest(), 'commit': None, 'dirty': None}
    try:
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True, timeout=10)
        status = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=ROOT,
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return identity                          # no git: the commit and its state are unknown
    if head.returncode == 0 and status.returncode == 0 and head.stdout.strip():
        identity['commit'] = head.stdout.strip()
        identity['dirty'] = bool(status.stdout.strip())
    return identity


def process_resources() -> dict:
    out = {'threads': threading.active_count()}
    try:
        import psutil          # optional; not a dependency of the app
        info = psutil.Process().memory_info()
        out['rss_mb'] = round(info.rss / 2 ** 20, 1)
        return out
    except Exception:
        pass
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        scale = 1 if sys.platform == 'darwin' else 1024    # ru_maxrss: bytes on macOS, KiB on Linux
        out['max_rss_mb'] = round(usage.ru_maxrss * scale / 2 ** 20, 1)
    except Exception:
        out['rss_mb'] = None
    return out


def fixture_exchange(minutes: int):
    from research import micro_fixtures as fx
    clock = fx.FixtureClock(start=time.time())            # never advanced: the real clock
    now = clock.minute()
    length = 300 + minutes + 120
    fine = {'amount_step': 0.000001, 'price_tick': 0.01}
    markets = [fx.SeriesMarket('ETH/USDT', now - 300, fx.swings(length, base=2500.0, range_bps=80, rise_bars=18,
                                                                drop_bars=2, zigzag_bps=4),
                               market=fx.mexc_market('ETH/USDT', **fine)),
               fx.SeriesMarket('BTC/USDT', now - 300, fx.flat(length, base=60000.0),
                               market=fx.mexc_market('BTC/USDT', **fine)),
               fx.SeriesMarket('SOL/USDT', now - 300, fx.random_walk(length, base=150.0, vol_bps=8, seed=7),
                               market=fx.mexc_market('SOL/USDT', **fine))]
    return fx.FixtureExchange(clock, markets)


def sample(engine, observability, micro, started_mono: float) -> dict:
    status = engine.micro.status()
    cycles = micro.open_cycles(engine.exchange_name, 'paper')
    states: dict = {}
    for cycle in cycles:
        states[cycle['state']] = states.get(cycle['state'], 0) + 1
    books = status.get('books') or {}
    pairs = {}
    for row in status.get('pairs', []):
        waiting = row.get('waiting') or {}
        decision = row.get('decision') or {}
        pairs[row['symbol']] = {'code': waiting.get('code') or decision.get('code'),
                                'waiting_s': waiting.get('waiting_s'), 'error': row.get('error')}
    latency = observability.METRICS.snapshot()['latency']
    results = status.get('results') or {}
    return {
        'wall_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'elapsed_s': round(time.monotonic() - started_mono, 1),
        'process': process_resources(),
        'engine': {'running': engine.running, 'stopping': engine.stopping, 'mode': engine.mode,
                   'risk_block': engine.risk_block, 'worker_alive': bool(engine.thread and engine.thread.is_alive()),
                   'feed_healthy': bool(engine.feed and getattr(engine.feed, 'healthy', lambda: False)())},
        'micro': {'open_by_state': states, 'closed_cycles': results.get('closed_cycles'),
                  'realized_net_total': results.get('realized_net_total'), 'latch': status.get('latch'),
                  'review_block': len(status.get('review_block') or []), 'last_error': status.get('last_error'),
                  'last_tick': status.get('last_tick')},
        'pairs': pairs,
        'books': books,
        'latency_ms': {name: {k: summary.get(k) for k in ('count', 'p50_ms', 'p95_ms', 'p99_ms', 'max_ms')}
                       for name, summary in latency.items() if name.startswith(('micro.', 'submit', 'order'))},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--data-dir', required=True, help='a NEW, empty folder for this soak profile')
    length = parser.add_mutually_exclusive_group(required=True)
    length.add_argument('--hours', type=float)
    length.add_argument('--minutes', type=float)
    parser.add_argument('--pairs', default='BTC/USDT,ETH/USDT', help='comma-separated USDT pairs')
    parser.add_argument('--exchange', default='mexc', choices=('mexc', 'kraken'))
    parser.add_argument('--streaming', action='store_true', help='turn streaming quotes on (default off)')
    parser.add_argument('--sample-seconds', type=float, default=60.0, help='1 to 3600 (default 60)')
    parser.add_argument('--fixture', action='store_true', help='synthetic markets on the real clock (offline)')
    args = parser.parse_args(argv)
    requested_s = (args.hours * 3600.0) if args.hours is not None else (args.minutes * 60.0)
    if not 0 < requested_s <= 7 * 24 * 3600:
        raise SystemExit('the duration must be positive and at most seven days')
    if not 1.0 <= args.sample_seconds <= 3600.0:            # also refuses NaN
        raise SystemExit('--sample-seconds must be between 1 and 3600')
    data_dir = isolate(Path(args.data_dir))
    if args.fixture:
        import offline_net                                   # beside this script
        offline_net.clear_proxies(os.environ)
        offline_net.install()
    # The source that runs is the source read now; a checkout that changes during the run is recorded as such.
    source_at_start = source_identity()
    sys.path.insert(0, str(ROOT))

    import credentials
    credentials.load = lambda *_: None          # no saved credential can be read, whatever the backend
    credentials.save = credentials.delete = lambda *_: None
    import micro
    import observability
    import storage
    import trader
    assert storage.DATA_DIR == data_dir, (storage.DATA_DIR, data_dir)

    pairs = [p.strip().upper() for p in args.pairs.split(',') if p.strip()]
    if args.fixture:
        exchange = fixture_exchange(int(requested_s // 60) + 1)
        pairs = list(exchange.series)
        trader.TradingEngine._new_exchange = staticmethod(lambda *a, **k: exchange)
    soak_dir = data_dir / 'soak'
    soak_dir.mkdir()
    samples_path = soak_dir / 'samples.jsonl'
    faults: list[dict] = []
    started_wall, started_mono = time.time(), time.monotonic()
    engine = trader.TradingEngine()
    if engine.mode != 'paper':
        raise SystemExit('refusing: the engine did not start in PAPER mode')
    cfg = dict(storage.settings())
    cfg.pop('paper_start_usdt', None)
    cfg.update(time_horizon='micro', exchange='mexc' if args.fixture else args.exchange, watchlist=pairs,
               auto_discovery=False, streaming_quotes=bool(args.streaming and not args.fixture))
    engine.update_config(cfg)
    engine.start()
    outcome = 'completed'
    count, last_wall = 0, time.time()
    try:
        deadline = started_mono + requested_s
        while time.monotonic() < deadline:
            time.sleep(max(0.0, min(args.sample_seconds, deadline - time.monotonic())))
            now_wall = time.time()
            gap = now_wall - last_wall
            if gap > max(3 * args.sample_seconds, args.sample_seconds + 30):
                faults.append({'kind': 'pause', 'wall_gap_s': round(gap, 1),
                               'note': 'sleep, hibernation or a stalled process between samples',
                               'at': datetime.now(timezone.utc).isoformat(timespec='seconds')})
            last_wall = now_wall
            try:
                row = sample(engine, observability, micro, started_mono)
            except Exception as exc:                 # recorded, never hidden; the soak continues
                faults.append({'kind': 'sample_error', 'error': f'{type(exc).__name__}: {exc}'[:300]})
                continue
            with samples_path.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(row, default=str) + '\n')
            count += 1
            if not row['engine']['worker_alive']:
                faults.append({'kind': 'worker_stopped', 'risk_block': row['engine']['risk_block'],
                               'at': row['wall_utc']})
                outcome = 'worker stopped'
                break
    except KeyboardInterrupt:
        outcome = 'interrupted by the operator'
    finally:
        engine.stop()
        settled = engine.wait_idle(60)
        if not settled:
            faults.append({'kind': 'stop_timeout', 'note': 'an order was still being confirmed after 60 s'})
    final = engine.micro.status()
    summary = {
        'harness': 'scripts/micro_soak.py',
        'outcome': outcome,
        'requested_s': requested_s,
        'actual_s': round(time.monotonic() - started_mono, 1),
        'started_utc': datetime.fromtimestamp(started_wall, timezone.utc).isoformat(timespec='seconds'),
        'ended_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'platform': platform.platform(), 'python': sys.version.split()[0], 'machine': platform.machine(),
        'source': source_at_start,
        'source_changed_during_run': source_identity() != source_at_start,
        'profile': str(data_dir), 'mode': engine.mode, 'exchange': engine.exchange_name, 'pairs': pairs,
        'market_data': 'synthetic fixture on the real clock' if args.fixture else 'public endpoints, no account',
        'remote_network_refused': bool(args.fixture),
        'streaming_quotes': bool(engine.cfg.get('streaming_quotes')),
        'samples': count, 'faults': faults,
        'results': final.get('results'), 'open_cycles': final.get('cycles'), 'latch': final.get('latch'),
        'review_block': final.get('review_block'),
        'metrics': observability.METRICS.snapshot(),
        'not_tested': [
            'real orders: every fill is the PAPER venue walking the public order book',
            'the account fee rate (needs a signed endpoint; the modeled fee is used)',
            'venue-side outages longer than this run, delistings, maintenance windows',
            'sleep/hibernation unless a pause fault above shows one happened',
            'synthetic markets only: nothing here is evidence about a real market' if args.fixture
            else 'economic edge: one run is one sample of one period; see docs/ATB_MICRO_RESEARCH.md',
        ],
    }
    (soak_dir / 'summary.json').write_text(json.dumps(summary, indent=1, default=str) + '\n', encoding='utf-8')
    print(json.dumps({k: summary[k] for k in ('outcome', 'requested_s', 'actual_s', 'samples')} |
                     {'faults': len(faults), 'closed_cycles': (summary['results'] or {}).get('closed_cycles'),
                      'summary': str(soak_dir / 'summary.json')}))
    return 0 if outcome == 'completed' and not any(f['kind'] != 'pause' for f in faults) else 1


if __name__ == '__main__':
    raise SystemExit(main())
