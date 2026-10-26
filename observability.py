"""Structured event log, correlation IDs and in-process metrics.

Events are JSON lines under ``<data dir>/logs/events.jsonl`` (rotated at 5 MB,
three backups), always passed through ``storage.redact`` so a secret that
reaches a message is masked. The file is opened for each append and closed
again, so no handle outlives a write: Windows can delete or move the data
directory between events, and tests can clean up temporary directories.
Metrics live in memory only: counters, gauges and bounded latency reservoirs
that report p50/p95/p99. Nothing here contacts a network or blocks a hot
path for longer than one small file append.
"""
from __future__ import annotations

import bisect
import contextvars
import json
import threading
import time
import uuid
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path

import storage

_LOG_LOCK = threading.Lock()
_correlation: contextvars.ContextVar[dict] = contextvars.ContextVar('cryptopilot_correlation', default={})

MAX_EVENT_BYTES = 16_000          # a full decision record is ~2-3 KB
MAX_LOG_BYTES = 5_000_000
LOG_BACKUPS = 3


def new_id(prefix: str) -> str:
    """Short, URL-safe correlation identifier such as ``dec_3f9a...``."""
    return f'{prefix}_{uuid.uuid4().hex[:16]}'


def log_path() -> Path:
    """The event log of the current data directory."""
    return storage.DATA_DIR / 'logs' / 'events.jsonl'


def _rotate(path: Path) -> None:
    for index in range(LOG_BACKUPS - 1, 0, -1):
        older = path.with_name(f'{path.name}.{index}')
        if older.exists():
            older.replace(path.with_name(f'{path.name}.{index + 1}'))
    path.replace(path.with_name(path.name + '.1'))


def _append(text: str) -> None:
    """Append one line, rotating first when the file would pass ``MAX_LOG_BYTES``."""
    path = log_path()
    with _LOG_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.exists() and path.stat().st_size + len(text) + 1 > MAX_LOG_BYTES:
                _rotate(path)
        except OSError:
            pass
        with path.open('a', encoding='utf8') as handle:
            handle.write(text + '\n')


def correlation() -> dict:
    return dict(_correlation.get())


class bind:
    """Attach correlation fields (decision_id, intent_id, ...) to events in this context."""

    def __init__(self, **fields):
        self.fields = {key: value for key, value in fields.items() if value is not None}
        self.token = None

    def __enter__(self):
        merged = {**_correlation.get(), **self.fields}
        self.token = _correlation.set(merged)
        return merged

    def __exit__(self, *_):
        if self.token is not None:
            _correlation.reset(self.token)


def _redacted(value):
    """Redact every string inside a JSON-shaped value before it is serialized."""
    if isinstance(value, str):
        return storage.redact(value)
    if isinstance(value, dict):
        return {key: _redacted(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redacted(item) for item in value]
    return value


def event(kind: str, level: str = 'info', **fields) -> dict:
    """Write one structured, redacted event line and return the record."""
    record = {'ts': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
              'kind': str(kind)[:64], 'level': str(level)[:16], **_correlation.get()}
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, float):
            record[key] = round(value, 6)
        elif isinstance(value, (int, bool)):
            record[key] = value
        elif isinstance(value, str):
            record[key] = storage.redact(value)
        else:
            # Strings are redacted before serialization, so a masked value can
            # never run into a JSON delimiter and break the line.
            try:
                record[key] = _redacted(json.loads(json.dumps(value, default=str)))
            except (TypeError, ValueError):
                record[key] = storage.redact(str(value))
    try:
        text = json.dumps(record, separators=(',', ':'), default=str)
    except (TypeError, ValueError):
        text = json.dumps({'ts': record['ts'], 'kind': record['kind'], 'level': 'error', 'error': 'unserializable event'})
    text = storage.redact(text)
    if len(text) > MAX_EVENT_BYTES:
        # Keep the line valid JSON: scalars stay, structured fields are replaced by their size.
        slim = {key: value if isinstance(value, (int, float, bool, str)) or value is None
                else f'<omitted {len(json.dumps(value, default=str))} bytes>' for key, value in record.items()}
        slim['truncated'] = True
        text = storage.redact(json.dumps(slim, separators=(',', ':'), default=str))[:MAX_EVENT_BYTES]
    try:
        _append(text)
    except Exception:
        pass  # Logging must never take down the order path.
    return record


class Reservoir:
    """Bounded sample of recent durations in milliseconds with exact quantiles."""

    def __init__(self, size: int = 512):
        self.samples: deque[float] = deque(maxlen=size)
        self.count, self.total, self.maximum = 0, 0.0, 0.0

    def add(self, value: float) -> None:
        self.samples.append(float(value))
        self.count += 1
        self.total += float(value)
        self.maximum = max(self.maximum, float(value))

    def summary(self) -> dict:
        if not self.samples:
            return {'count': self.count, 'p50_ms': None, 'p95_ms': None, 'p99_ms': None, 'max_ms': None, 'mean_ms': None}
        ordered = sorted(self.samples)

        def quantile(q: float) -> float:
            index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
            return round(ordered[index], 3)
        return {'count': self.count, 'p50_ms': quantile(.5), 'p95_ms': quantile(.95), 'p99_ms': quantile(.99),
                'max_ms': round(max(ordered), 3), 'mean_ms': round(sum(ordered) / len(ordered), 3),
                'window': len(ordered)}


class Metrics:
    """Thread-safe counters, gauges and latency reservoirs."""

    def __init__(self):
        self.lock = threading.Lock()
        self.counters: Counter = Counter()
        self.gauges: dict[str, float] = {}
        self.timings: dict[str, Reservoir] = {}
        self.started = time.time()

    def incr(self, name: str, amount: int = 1) -> None:
        with self.lock:
            self.counters[name] += amount

    def gauge(self, name: str, value) -> None:
        with self.lock:
            self.gauges[name] = value

    def observe(self, name: str, milliseconds: float) -> None:
        with self.lock:
            reservoir = self.timings.get(name)
            if reservoir is None:
                reservoir = self.timings[name] = Reservoir()
            reservoir.add(milliseconds)

    class _Timer:
        def __init__(self, metrics, name):
            self.metrics, self.name, self.started = metrics, name, None
            self.elapsed_ms = None

        def __enter__(self):
            self.started = time.perf_counter()
            return self

        def __exit__(self, *_):
            self.elapsed_ms = (time.perf_counter() - self.started) * 1000
            self.metrics.observe(self.name, self.elapsed_ms)

    def timer(self, name: str) -> '_Timer':
        return Metrics._Timer(self, name)

    def snapshot(self) -> dict:
        with self.lock:
            return {'uptime_seconds': round(time.time() - self.started, 1),
                    'counters': dict(sorted(self.counters.items())),
                    'gauges': dict(sorted(self.gauges.items())),
                    'latency': {name: reservoir.summary() for name, reservoir in sorted(self.timings.items())}}


METRICS = Metrics()


def percentiles(values) -> dict:
    """p50/p95/p99 of an arbitrary list, for benchmarks and reports."""
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return {'count': 0, 'p50': None, 'p95': None, 'p99': None, 'max': None}

    def quantile(q):
        index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
        return ordered[index]
    return {'count': len(ordered), 'p50': quantile(.5), 'p95': quantile(.95), 'p99': quantile(.99),
            'max': ordered[-1], 'mean': sum(ordered) / len(ordered)}


def insort_bounded(values: list, value, limit: int) -> None:
    """Keep a sorted list bounded; used by tests of reservoir behaviour."""
    bisect.insort(values, value)
    del values[limit:]
