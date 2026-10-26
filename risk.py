"""Enforceable pre-trade risk controls, independent of the strategy.

Every path that can create an order (automated entries, software-monitored
exits, the manual ticket) must obtain an :class:`Authorization` from
:meth:`RiskGate.authorize` immediately before the order is created, and the
engine's submit path refuses to send anything without a fresh, matching
authorization. The gate reads durable state (kill switch, journal, trades,
latches) plus the marks the engine already holds, so a new order path cannot
accidentally skip a control.

Entry controls can only block; exit controls are deliberately narrow (kill
switch policy, stale quote, runaway order rate), because blocking a protective
exit is itself a risk.
"""
from __future__ import annotations

import json
import math
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import observability
import storage

AUTHORIZATION_TTL_SECONDS = 30.0
RELEASE_PHRASE = 'RESUME TRADING'
REJECT_SPIKE_COUNT, REJECT_SPIKE_WINDOW_S, BREAKER_HOLD_S = 3, 600, 1800
FAILURE_SPIKE_COUNT, FAILURE_SPIKE_WINDOW_S = 3, 600
SLIPPAGE_SAMPLE, SLIPPAGE_MULTIPLE = 3, 3.0
EXIT_RATE_MULTIPLE = 3
MIN_ORDER_USDT = 12.0

ENTRY_KINDS = ('entry', 'manual_entry')
EXIT_KINDS = ('exit', 'manual_exit')


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class OrderRequest:
    exchange: str
    mode: str
    symbol: str
    side: str                      # 'BUY' | 'SELL'
    kind: str                      # 'entry' | 'exit' | 'manual_entry' | 'manual_exit'
    notional: float                # USDT to spend (buys) or qty * price (sells)
    price: float                   # executable quote the request is priced from
    quantity: float | None = None
    signal_close: float | None = None
    atr: float | None = None
    stop_fraction: float | None = None
    quote_age_seconds: float | None = None
    spread_pct: float | None = None
    reason: str = ''
    decision_id: str | None = None

    def __post_init__(self):
        if self.side not in ('BUY', 'SELL'):
            raise ValueError('Order side must be BUY or SELL')
        if self.kind not in ENTRY_KINDS + EXIT_KINDS:
            raise ValueError('Unknown order kind')
        if (self.side == 'BUY') != (self.kind in ENTRY_KINDS):
            raise ValueError('Entries buy and exits sell')

    @property
    def is_entry(self) -> bool:
        return self.kind in ENTRY_KINDS


@dataclass(frozen=True)
class Authorization:
    allowed: bool
    request: OrderRequest
    failed: tuple[str, ...]
    checks: tuple[dict, ...]
    token: str
    issued_at: float = field(default_factory=time.monotonic)

    @property
    def reason(self) -> str:
        return '; '.join(self.failed)

    def valid_for(self, symbol: str, side: str, *, now: float | None = None) -> bool:
        """A submit may only use an authorization issued for it, moments ago."""
        age = (time.monotonic() if now is None else now) - self.issued_at
        return bool(self.allowed and self.request.symbol == symbol and self.request.side == side
                    and 0 <= age <= AUTHORIZATION_TTL_SECONDS)


class RiskGate:
    """Hard controls evaluated against the engine's fresh marks and durable state."""

    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.Lock()
        self.metrics = observability.METRICS
        self._failures: list[float] = []      # monotonic times of exchange/ledger failures
        self._breaker_until: dict[str, float] = {}
        self.last_block: dict | None = None
        self.blocked = 0

    # ----------------------------------------------------------------- state
    def _key(self, name: str, mode: str | None = None) -> str:
        scope = name if mode is None else f'{name}.{mode}'
        return storage.scope_key(scope, self.engine.exchange_name)

    def kill_switch(self) -> dict:
        raw = storage.get_meta(self._key('kill_switch'))
        if not raw:
            return {'engaged': False, 'allow_exits': True, 'engaged_at': None, 'reason': None}
        try:
            saved = json.loads(raw)
            if not isinstance(saved, dict):
                raise ValueError
        except (ValueError, TypeError):
            # An unreadable record fails closed: treat the switch as engaged.
            return {'engaged': True, 'allow_exits': True, 'engaged_at': None, 'reason': 'unreadable kill switch record'}
        return {'engaged': bool(saved.get('engaged')), 'allow_exits': saved.get('allow_exits') is not False,
                'engaged_at': saved.get('engaged_at'), 'reason': saved.get('reason')}

    def switch_blocks(self, request: OrderRequest) -> str | None:
        """The kill switch as it stands now, for the last check before an order leaves."""
        switch = self.kill_switch()
        if not switch['engaged']:
            return None
        if request.is_entry:
            return 'Kill switch engaged: new orders blocked'
        return None if switch['allow_exits'] else 'Kill switch engaged with exits blocked'

    def engage_kill_switch(self, reason: str = 'Operator kill switch', *, allow_exits: bool = True) -> dict:
        """Immediate and durable. Needs no confirmation and no idle engine."""
        record = {'engaged': True, 'allow_exits': bool(allow_exits), 'engaged_at': _now_iso(),
                  'reason': str(reason)[:200]}
        storage.set_meta(self._key('kill_switch'), json.dumps(record))
        with self.engine.lock:
            self.engine.risk_block = 'Kill switch engaged: new orders blocked' + ('' if allow_exits else '; exits blocked too')
        storage.audit('warning', f'KILL SWITCH ENGAGED ({self.engine.exchange_name.upper()}): {record["reason"]}. '
                                 + ('Protective exits remain allowed.' if allow_exits else 'All orders, including exits, are blocked.'))
        observability.event('risk.kill_switch', level='warning', engaged=True, allow_exits=allow_exits, reason=record['reason'])
        self.metrics.incr('risk.kill_switch_engaged')
        return self.kill_switch()

    def release_kill_switch(self, confirmation: str) -> dict:
        if confirmation != RELEASE_PHRASE:
            raise storage.ConfigValidationError(f'Type {RELEASE_PHRASE} to release the kill switch.')
        storage.set_meta(self._key('kill_switch'), json.dumps({'engaged': False, 'released_at': _now_iso()}))
        with self.engine.lock:
            if self.engine.risk_block and self.engine.risk_block.startswith('Kill switch'):
                self.engine.risk_block = None
        storage.audit('warning', f'Kill switch released for {self.engine.exchange_name.upper()} after explicit confirmation.')
        observability.event('risk.kill_switch', level='warning', engaged=False)
        return self.kill_switch()

    def drawdown_stop(self, mode: str) -> str | None:
        return storage.get_meta(self._key('drawdown_stop', mode)) or None

    def clear_drawdown_stop(self, mode: str, confirmation: str) -> None:
        if confirmation != RELEASE_PHRASE:
            raise storage.ConfigValidationError(f'Type {RELEASE_PHRASE} to clear the drawdown stop.')
        with storage.connection() as db:
            db.execute('DELETE FROM meta WHERE key=?', (self._key('drawdown_stop', mode),))
            db.execute('DELETE FROM meta WHERE key=?', (self._key('peak_equity', mode),))
        storage.audit('warning', f'{mode.upper()} drawdown stop cleared after explicit confirmation; the equity peak restarts from the next fresh valuation.')
        observability.event('risk.drawdown_stop_cleared', level='warning', mode=mode)

    def realized_stop(self, mode: str) -> str | None:
        """ISO time at which today's realised-loss stop latched, or None."""
        return storage.get_meta(self._key('realized_stop', mode)) or None

    def _latch_realized_stop(self, mode: str, realized: float, limit: float) -> None:
        storage.set_meta(self._key('realized_stop', mode), _now_iso())
        storage.audit('error', f'{mode.upper()} daily realised-loss stop latched: {realized:.2f} USDT against a limit of '
                               f'{limit:.2f} USDT. New entries are blocked until cleared with {RELEASE_PHRASE}.')
        observability.event('risk.realized_stop', level='error', mode=mode, realized=realized, limit=limit)
        self.metrics.incr('risk.realized_stop_latched')

    def clear_realized_stop(self, mode: str, confirmation: str) -> None:
        """Lift the latch. For the rest of this UTC day it latches again only on a further loss.

        The day's realised total at the moment of clearing is recorded, so the
        next entry does not re-latch on the loss the operator just reviewed.
        """
        if confirmation != RELEASE_PHRASE:
            raise storage.ConfigValidationError(f'Type {RELEASE_PHRASE} to clear the realised-loss stop.')
        realized = self._realized_today(mode)
        cleared = json.dumps({'date': _today(), 'realized': round(realized, 8)})
        with storage.connection() as db:
            db.execute('DELETE FROM meta WHERE key=?', (self._key('realized_stop', mode),))
            db.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',
                       (self._key('realized_stop_cleared', mode), cleared))
        storage.audit('warning', f'{mode.upper()} daily realised-loss stop cleared after explicit confirmation at '
                                 f'{realized:.2f} USDT realised today; a further loss today latches it again.')
        observability.event('risk.realized_stop_cleared', level='warning', mode=mode, realized=realized)

    def _cleared_floor(self, mode: str) -> float | None:
        """Today's realised total when the operator last cleared the latch, or None."""
        try:
            record = json.loads(storage.get_meta(self._key('realized_stop_cleared', mode)) or 'null')
        except ValueError:
            return None
        if not isinstance(record, dict) or record.get('date') != _today():
            return None
        return _finite(record.get('realized'))

    def observe_realized(self, mode: str, equity: float) -> str | None:
        """Latch the realised-loss stop as soon as today's realised loss crosses the limit.

        Called with every fresh valuation (each monitor poll and scan) as well
        as from :meth:`authorize`, so a loss booked by an exit latches at once
        instead of waiting for the next entry attempt, after which a profit or
        the UTC rollover could have hidden it. Returns the latch time.
        """
        value = _finite(equity)
        latched = self.realized_stop(mode)
        if latched or value is None or value <= 0:
            return latched
        realized = self._realized_today(mode)
        limit = value * float(self.engine.cfg.get('daily_realized_loss_limit_pct',
                                                  storage.DEFAULTS['daily_realized_loss_limit_pct'])) / 100
        floor = self._cleared_floor(mode)
        crossed = realized <= -limit and (floor is None or realized < floor - 0.005)
        if crossed:
            # A durable latch: a later profit, a new UTC day or a settings
            # save does not lift it; only the release phrase does.
            self._latch_realized_stop(mode, realized, limit)
            return self.realized_stop(mode)
        return None

    def observe_equity(self, mode: str, equity: float) -> dict:
        """Track the durable equity peak and latch the drawdown stop.

        Called whenever the engine values the portfolio from fresh marks.
        """
        value = _finite(equity)
        if value is None or value <= 0:
            return {'peak': None, 'drawdown_pct': None, 'latched': bool(self.drawdown_stop(mode))}
        key = self._key('peak_equity', mode)
        peak = _finite(storage.get_meta(key)) or 0.0
        if value > peak:
            peak = value
            storage.set_meta(key, repr(value))
        drawdown = 100 * (peak - value) / peak if peak else 0.0
        limit = float(self.engine.cfg.get('max_drawdown_pct', storage.DEFAULTS['max_drawdown_pct']))
        latched = bool(self.drawdown_stop(mode))
        if drawdown >= limit and not latched:
            storage.set_meta(self._key('drawdown_stop', mode), _now_iso())
            latched = True
            storage.audit('error', f'{mode.upper()} drawdown stop latched: equity {value:.2f} is {drawdown:.2f}% below the peak {peak:.2f}. New entries stay blocked until the stop is cleared.')
            observability.event('risk.drawdown_stop', level='error', mode=mode, equity=value, peak=peak, drawdown_pct=drawdown)
            self.metrics.incr('risk.drawdown_stop_latched')
        self.metrics.gauge(f'risk.drawdown_pct.{mode}', round(drawdown, 3))
        return {'peak': peak, 'drawdown_pct': round(drawdown, 3), 'latched': latched}

    def record_failure(self, kind: str) -> None:
        """Exchange rejections, uncertain outcomes and ledger failures feed the breaker."""
        now = time.monotonic()
        with self.lock:
            self._failures = [t for t in self._failures if now - t <= FAILURE_SPIKE_WINDOW_S] + [now]
            if len(self._failures) >= FAILURE_SPIKE_COUNT:
                self._breaker_until['failures'] = now + BREAKER_HOLD_S
                self._failures.clear()
                tripped = True
            else:
                tripped = False
        self.metrics.incr(f'risk.failure.{kind}')
        if tripped:
            storage.audit('error', f'Circuit breaker: {FAILURE_SPIKE_COUNT} execution failures within {FAILURE_SPIKE_WINDOW_S // 60} minutes; new entries pause for {BREAKER_HOLD_S // 60} minutes.')
            observability.event('risk.breaker', level='error', breaker='failures', hold_seconds=BREAKER_HOLD_S)

    # ------------------------------------------------------------ evaluation
    def _recent_orders(self, mode: str, seconds: float) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec='milliseconds')
        return storage.rows('SELECT status,created_at,updated_at,intended_price,filled_price,side FROM order_journal '
                            'WHERE exchange=? AND mode=? AND created_at>=? ORDER BY created_at',
                            (self.engine.exchange_name, mode, since))

    def _realized_today(self, mode: str) -> float:
        today = datetime.now(timezone.utc).date().isoformat()
        row = storage.rows("SELECT COALESCE(SUM(realized),0) AS value FROM trades WHERE exchange=? AND mode=? AND side='SELL' AND created_at LIKE ?",
                           (self.engine.exchange_name, mode, today + '%'))
        return float(row[0]['value'] or 0.0)

    def _reject_spike_remaining(self, mode: str) -> float:
        """Seconds until the rejection-spike breaker closes on its own; 0 when it is closed.

        Only requests that reached the counterparty count (``submitted_at`` is
        set): an order the engine itself refused before sending (a Stop, the
        kill switch) is not an exchange rejection.
        """
        since = (datetime.now(timezone.utc) - timedelta(seconds=REJECT_SPIKE_WINDOW_S)).isoformat(timespec='milliseconds')
        rows = storage.rows("SELECT updated_at FROM order_journal WHERE exchange=? AND mode=? AND status='rejected' "
                            'AND submitted_at IS NOT NULL AND updated_at>=? ORDER BY updated_at',
                            (self.engine.exchange_name, mode, since))
        if len(rows) < REJECT_SPIKE_COUNT:
            return 0.0
        # It closes when enough of the counted rejections have left the window.
        oldest_needed = rows[len(rows) - REJECT_SPIKE_COUNT]['updated_at']
        try:
            closes = datetime.fromisoformat(oldest_needed).timestamp() + REJECT_SPIKE_WINDOW_S
        except (TypeError, ValueError):
            return float(REJECT_SPIKE_WINDOW_S)
        return max(1.0, closes - time.time())

    def _reject_spike(self, mode: str) -> bool:
        return self._reject_spike_remaining(mode) > 0

    def _slippage_record(self, mode: str) -> dict:
        raw = storage.get_meta(self._key('slippage_breaker', mode))
        try:
            record = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            record = {}
        return record if isinstance(record, dict) else {}

    def _slippage_remaining(self, mode: str) -> float:
        record = self._slippage_record(mode)
        until, now = _finite(record.get('until')), time.time()
        remaining = (until or 0.0) - now
        if remaining > BREAKER_HOLD_S:
            # The PC clock stepped back after the trip. The hold never runs
            # longer than its nominal length from now, so it still closes.
            record['until'] = now + BREAKER_HOLD_S
            storage.set_meta(self._key('slippage_breaker', mode), json.dumps(record))
            remaining = float(BREAKER_HOLD_S)
        return max(0.0, remaining)

    def _slippage_breaker(self, mode: str) -> tuple[bool, float | None]:
        """Pause entries for ``BREAKER_HOLD_S`` after the last fills ran far worse than assumed.

        The fills that tripped it are then spent: once the hold ends, only
        orders accounted after them can trip it again, so it closes on its own
        even when no new order can move the average.
        """
        record = self._slippage_record(mode)
        if self._slippage_remaining(mode) > 0:
            return True, _finite(record.get('average'))
        after = record.get('after') if isinstance(record.get('after'), str) else ''
        rows = storage.rows('SELECT intended_price,filled_price,side,updated_at FROM order_journal WHERE exchange=? AND mode=? '
                            "AND status='accounted' AND intended_price IS NOT NULL AND filled_price IS NOT NULL "
                            'AND updated_at>? ORDER BY updated_at DESC LIMIT ?',
                            (self.engine.exchange_name, mode, after, SLIPPAGE_SAMPLE))
        if len(rows) < SLIPPAGE_SAMPLE:
            return False, None
        adverse = []
        for row in rows:
            intended, filled = _finite(row['intended_price']), _finite(row['filled_price'])
            if not intended or not filled or intended <= 0:
                return False, None
            move = (filled - intended) / intended * 100
            adverse.append(move if row['side'] == 'BUY' else -move)
        average = round(sum(adverse) / len(adverse), 4)
        assumed = float(self.engine.cfg.get('slippage_pct', 0.2))
        if average <= max(0.05, assumed) * SLIPPAGE_MULTIPLE:
            return False, average
        storage.set_meta(self._key('slippage_breaker', mode), json.dumps(
            {'until': time.time() + BREAKER_HOLD_S, 'after': max(row['updated_at'] for row in rows), 'average': average}))
        self.metrics.incr('risk.breaker_open')
        storage.audit('error', f'Circuit breaker: average adverse slippage {average:.3f}% over the last {SLIPPAGE_SAMPLE} fills; '
                               f'new entries pause for {BREAKER_HOLD_S // 60} minutes.')
        observability.event('risk.breaker', level='error', breaker='slippage', average_adverse_slippage_pct=average,
                            hold_seconds=BREAKER_HOLD_S)
        return True, average

    def authorize(self, request: OrderRequest, *, positions: list[dict], cash: float | None,
                  equity: float | None, marks_fresh: bool, symbol_fresh: bool) -> Authorization:
        """Evaluate every control for this request and return a signed verdict.

        ``positions`` are the tracked positions in this exchange/mode, ``cash``
        and ``equity`` the engine's current valuation (None when unavailable),
        ``marks_fresh`` whether every held asset has a fresh quote and
        ``symbol_fresh`` whether the request's own symbol does.
        """
        cfg = self.engine.cfg
        checks: list[dict] = []
        failed: list[str] = []
        mode, now_mono = request.mode, time.monotonic()

        def check(name: str, passed: bool, detail: str = '', **values):
            checks.append({'check': name, 'passed': bool(passed), 'detail': detail, **values})
            if not passed:
                failed.append(detail or name)

        switch = self.kill_switch()
        halted = bool(mode == 'live' and (self.engine.live_halted or self.engine._durable_live_block()))
        check('mode_scope', request.exchange == self.engine.exchange_name and mode == self.engine.mode,
              'Request belongs to another exchange or trading mode')
        check('orders_allowed', self.engine._may_execute(), 'Engine is stopped or stopping')
        check('live_halt', not halted, 'Live execution is halted pending manual reconciliation')
        check('symbol_quote_fresh', symbol_fresh, f'No fresh quote for {request.symbol}',
              age_seconds=request.quote_age_seconds, limit_seconds=cfg['data_max_age_seconds'])
        check('price_finite', bool(_finite(request.price)) and request.price > 0, 'Executable price is invalid')
        check('notional_finite', bool(_finite(request.notional)) and request.notional > 0, 'Order notional is invalid')
        if request.is_entry:
            check('kill_switch', not switch['engaged'], 'Kill switch engaged: new orders blocked')
            check('settings_review', not (mode == 'live' and storage.settings_health()['review_required']),
                  'Recovered settings require an explicit review and save before new live exposure')
            check('all_marks_fresh', marks_fresh, 'A held asset has a stale or missing price; new entries blocked')
            check('valuation_available', cash is not None and equity is not None and _finite(equity) and equity > 0,
                  'Portfolio valuation unavailable; new entries blocked')
            eq = float(equity) if equity is not None and _finite(equity) else 0.0
            check('min_order', request.notional >= MIN_ORDER_USDT, f'Order below the {MIN_ORDER_USDT:.0f} USDT minimum')
            max_order = float(cfg['max_order_notional_usdt'])
            check('max_order_notional', request.notional <= max_order + 1e-6,
                  f'Order notional {request.notional:.2f} exceeds the {max_order:.0f} USDT per-order cap', limit=max_order)
            held_value = 0.0
            for position in positions:
                if position['symbol'] == request.symbol:
                    mark = self.engine.last_prices.get(request.symbol, position['entry_price'])
                    held_value += float(position['qty']) * float(mark)
            max_asset = float(cfg['max_asset_notional_usdt'])
            check('max_asset_notional', held_value + request.notional <= max_asset + 1e-6,
                  f'Position in {request.symbol} would exceed the {max_asset:.0f} USDT per-asset cap', limit=max_asset)
            if eq > 0:
                check('max_position_pct', request.notional <= eq * cfg['max_position_pct'] / 100 * 1.0001,
                      'Order exceeds the maximum position size percentage')
                with self.engine.lock:
                    used = sum(float(p['qty']) * float(self.engine.last_prices.get(p['symbol'], p['entry_price'])) for p in positions)
                check('max_exposure_pct', used + request.notional <= eq * cfg['max_exposure_pct'] / 100 * 1.0001,
                      'Order would exceed the gross exposure cap', used=round(used, 2))
                if request.stop_fraction is not None:
                    fraction = float(request.stop_fraction)
                    fee, slip = cfg['fee_pct'] / 100, cfg['slippage_pct'] / 100
                    loss_fraction = 1 - (1 - fraction) * (1 - slip) * (1 - fee) / (1 + fee)
                    check('per_trade_loss_budget', request.notional * loss_fraction <= eq * cfg['risk_pct'] / 100 * 1.001,
                          'Loss at the protective stop exceeds the per-trade risk budget',
                          loss_at_stop=round(request.notional * loss_fraction, 2))
                    available_risk = max(0.0, eq * cfg['max_portfolio_risk_pct'] / 100 - self.engine._portfolio_risk(positions))
                    check('portfolio_stop_risk', request.notional * loss_fraction <= available_risk + 1e-6,
                          'Aggregate protective-stop risk cap reached')
            tradable = [p for p in positions if not self.engine._is_dust(
                p, self.engine._catalogue().get(p['symbol']), self.engine.last_prices.get(p['symbol'], p['entry_price']))]
            check('max_positions', len(tradable) < cfg['max_positions'] or any(p['symbol'] == request.symbol for p in positions),
                  'The maximum number of open positions has been reached')
            daily_allowed, loss_today = self.engine._daily_allowed(mode, eq) if eq > 0 else (False, None)
            check('daily_equity_loss', daily_allowed, 'Daily loss limit reached; entries blocked until the next UTC day',
                  loss_pct=loss_today)
            realized = self._realized_today(mode)
            latched_at = self.observe_realized(mode, eq)
            check('daily_realized_loss', not latched_at,
                  f'Realised-loss stop latched at {latched_at}; entries blocked until cleared with {RELEASE_PHRASE}'
                  if latched_at else '', realized=round(realized, 2))
            check('drawdown_stop', not self.drawdown_stop(mode), 'Drawdown stop latched; entries blocked until cleared')
            if request.spread_pct is not None:
                check('spread', request.spread_pct <= cfg['max_spread_pct'], 'Bid-ask spread exceeds the configured limit',
                      spread_pct=request.spread_pct)
            if request.signal_close is not None and request.atr is not None:
                from strategy import entry_price_ok
                check('price_move', entry_price_ok(request.price, request.signal_close, request.atr),
                      'Market price moved too far from the completed signal candle')
            recent = self._recent_orders(mode, 60)
            check('order_rate', len(recent) < cfg['max_orders_per_minute'],
                  f'Order rate limit of {cfg["max_orders_per_minute"]} per minute reached', recent=len(recent))
            check('reject_spike', not self._reject_spike(mode),
                  'Circuit breaker: repeated exchange rejections; new entries paused')
            with self.lock:
                breaker_open = any(until > now_mono for until in self._breaker_until.values())
            check('failure_breaker', not breaker_open, 'Circuit breaker: repeated execution failures; new entries paused')
            slipped, average_slip = self._slippage_breaker(mode)
            check('slippage_breaker', not slipped, 'Circuit breaker: realized slippage far above the assumed cost',
                  average_adverse_slippage_pct=average_slip)
        else:
            check('kill_switch_exits', not switch['engaged'] or switch['allow_exits'],
                  'Kill switch engaged with exits blocked')
            recent = self._recent_orders(mode, 60)
            check('exit_order_rate', len(recent) < cfg['max_orders_per_minute'] * EXIT_RATE_MULTIPLE,
                  'Runaway order rate; exits paused for review', recent=len(recent))
            check('position_tracked', any(p['symbol'] == request.symbol for p in positions),
                  'No tracked position exists for this exit')
        allowed = not failed
        authorization = Authorization(allowed, request, tuple(failed), tuple(checks), secrets.token_hex(8))
        if allowed:
            self.metrics.incr('risk.authorized')
        else:
            self.blocked += 1
            self.metrics.incr('risk.blocked')
            self.last_block = {'at': _now_iso(), 'symbol': request.symbol, 'side': request.side,
                               'kind': request.kind, 'reasons': list(failed)}
            with self.engine.lock:
                self.engine.risk_block = failed[0]
            observability.event('risk.blocked', level='warning', symbol=request.symbol, side=request.side,
                                order_kind=request.kind, reasons=list(failed), notional=request.notional,
                                decision_id=request.decision_id)
        observability.event('risk.authorize', allowed=allowed, symbol=request.symbol, side=request.side,
                            order_kind=request.kind, notional=request.notional, failed=list(failed),
                            token=authorization.token, decision_id=request.decision_id)
        return authorization

    def status(self) -> dict:
        with self.lock:
            now = time.monotonic()
            breakers = {name: round(until - now, 1) for name, until in self._breaker_until.items() if until > now}
        mode = self.engine.mode
        for name, remaining in (('reject_spike', self._reject_spike_remaining(mode)),
                                ('slippage', self._slippage_remaining(mode))):
            if remaining > 0:
                breakers[name] = round(remaining, 1)
        return {'kill_switch': self.kill_switch(), 'drawdown_stop': self.drawdown_stop(mode),
                'realized_stop': self.realized_stop(mode),
                'peak_equity': _finite(storage.get_meta(self._key('peak_equity', mode))),
                'blocked_total': self.blocked, 'last_block': self.last_block,
                'breakers_open': breakers, 'release_phrase': RELEASE_PHRASE,
                'realized_today': round(self._realized_today(mode), 2)}
