"""Structured event log, correlation IDs and in-process metrics.

Events are JSON lines under ``<data dir>/logs/events.jsonl`` (rotated), always
passed through ``storage.redact`` so a secret that reaches a message is masked.
Metrics live in memory only: counters, gauges and bounded latency reservoirs
that report p50/p95/p99. Nothing here contacts a network or blocks a hot
path for longer than one buffered file write.
"""
from __future__ import annotations

import bisect
import contextvars
import json
import logging
import logging.handlers
import threading
import time
import uuid
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path

import storage

_LOGGER_LOCK = threading.Lock()
_LOGGER: logging.Logger | None = None
_LOGGER_PATH: Path | None = None
_correlation: contextvars.ContextVar[dict] = contextvars.ContextVar('cryptopilot_correlation', default={})

MAX_EVENT_BYTES = 4000


def new_id(prefix: str) -> str:
    """Short, URL-safe correlation identifier such as ``dec_3f9a...``."""
    return f'{prefix}_{uuid.uuid4().hex[:16]}'


def _logger() -> logging.Logger:
    """Rotating JSON-lines logger bound to the current data directory."""
    global _LOGGER, _LOGGER_PATH
    path = storage.DATA_DIR / 'logs' / 'events.jsonl'
    with _LOGGER_LOCK:
        if _LOGGER is not None and _LOGGER_PATH == path:
            return _LOGGER
        logger = logging.getLogger('cryptopilot.events.' + uuid.uuid4().hex)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding='utf8')
        except OSError:
            handler = logging.NullHandler()
        handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(handler)
        _LOGGER, _LOGGER_PATH = logger, path
        return logger


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


def event(kind: str, level: str = 'info', **fields) -> dict:
    """Write one structured, redacted event line and return the record."""
    record = {'ts': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
              'kind': str(kind)[:64], 'level': str(level)[:16], **_correlation.get()}
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, float):
            record[key] = round(value, 6)
        elif isinstance(value, (int, bool, str)):
            record[key] = value
        else:
            try:
                record[key] = json.loads(json.dumps(value, default=str))
            except (TypeError, ValueError):
                record[key] = str(value)
    try:
        text = json.dumps(record, separators=(',', ':'), default=str)
    except (TypeError, ValueError):
        text = json.dumps({'ts': record['ts'], 'kind': record['kind'], 'level': 'error', 'error': 'unserializable event'})
    text = storage.redact(text)
    if len(text) > MAX_EVENT_BYTES:
        text = text[:MAX_EVENT_BYTES - 20] + '..."truncated":true}'
    try:
        _logger().info(text)
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
