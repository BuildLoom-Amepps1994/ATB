"""Autonomous PAPER micro-trading cycles: buy about 25 USDT, take a small net gain or a bounded loss, repeat.

The manager runs on the engine's worker thread when the selected strategy
horizon is ``micro``. Each tick it

1. refuses to do anything unless the engine is in PAPER mode (``micro_policy``),
2. manages every open cycle first: a fresh validated order book (or, failing
   that, a fresh top-of-book quote for safety exits only), an after-cost exit
   estimate, the exit policy, and a market sell through the engine's single
   risk gate and submit path,
3. refreshes slower completed-bar context (1m features, 5m regime) after each
   bar closes, re-checking open cycles between downloads,
4. evaluates new entries only for the pairs the operator selected, through one
   sizing component (``micro_economics``) and the strategy rules
   (``micro_strategy``), reserving cash atomically before an order intent
   exists.

A cycle is a durable row in ``micro_cycles`` with an explicit state machine
(``TRANSITIONS``). Order intents carry ``decision_id = cycle_id``, so the
order journal and the cycle always correlate. A fill, the journal's
``accounted`` transition, the ledger rows, the cycle state and the reservation
release commit in one SQLite transaction; a crash can leave an order without
its booking (resolved later from the paper venue's durable record, with a
read-only explanation and an operator confirmation) but never a booking
without its cycle state.

Nothing here can submit to a real venue: the only client is the in-process
``micro_venue.MicroPaperVenue`` and every boundary asks ``micro_policy``.
"""
from __future__ import annotations

import json
import math
import threading
import time
import uuid
from collections import Counter, deque
from datetime import datetime, timezone
from decimal import Decimal

import ccxt

import micro_book
import micro_economics as econ
import micro_policy
import micro_strategy as strat
import micro_venue
import money
import observability
import storage
import venue_rules

STATES = ('ENTRY_RESERVED', 'ENTRY_SUBMITTED', 'ENTRY_UNKNOWN', 'HOLDING', 'EXIT_SUBMITTED', 'EXIT_UNKNOWN',
          'REVIEW', 'CLOSED', 'ABANDONED')
TERMINAL = ('CLOSED', 'ABANDONED')
ENTRY_STATES = ('ENTRY_RESERVED', 'ENTRY_SUBMITTED', 'ENTRY_UNKNOWN')
OWNING_STATES = ('HOLDING', 'EXIT_SUBMITTED', 'EXIT_UNKNOWN')
BLOCKING_STATES = ('ENTRY_UNKNOWN', 'EXIT_UNKNOWN', 'REVIEW')
# States that exist only while one operation runs; one seen when none runs was interrupted.
TRANSIENT_STATES = ('ENTRY_RESERVED', 'ENTRY_SUBMITTED', 'EXIT_SUBMITTED')
# Intent statuses that settled an earlier attempt: never the outcome of the current one.
SETTLED_INTENT_STATUSES = ('accounted', 'reconciled')
TRANSITIONS = {
    'ENTRY_RESERVED': {'ENTRY_SUBMITTED', 'ABANDONED', 'REVIEW'},
    'ENTRY_SUBMITTED': {'HOLDING', 'ABANDONED', 'ENTRY_UNKNOWN', 'REVIEW'},
    'ENTRY_UNKNOWN': {'HOLDING', 'ABANDONED', 'REVIEW'},
    'HOLDING': {'EXIT_SUBMITTED', 'CLOSED', 'REVIEW'},
    'EXIT_SUBMITTED': {'HOLDING', 'CLOSED', 'EXIT_UNKNOWN', 'REVIEW'},
    'EXIT_UNKNOWN': {'HOLDING', 'CLOSED', 'REVIEW'},
    'REVIEW': {'HOLDING', 'CLOSED', 'ABANDONED'},
    'CLOSED': set(), 'ABANDONED': set(),
}
REVIEW_PHRASE = 'I HAVE REVIEWED THIS CYCLE'
RESUME_PHRASE = 'RESUME TRADING'

# Fixed research defaults of the strategy (documented in docs/ATB_MICRO_ECONOMICS.md).
UNCERTAINTY_FLOOR_BPS = 5.0
MAX_VOL_BPS = 60.0
MIN_EFFICIENCY_TREND = 0.35
MAX_EFFICIENCY_RANGE = 0.25
EDGE_RATIO = 0.6
MAX_SPREAD_BPS = 20.0
BOOK_LEVELS = 20
ENTRY_BOOK_SECONDS = 3.0          # a flat pair's book is refreshed this often while a setup is plausible
IDLE_BOOK_SECONDS = 30.0          # otherwise only this often (eligibility display)
EXIT_RETRY_SECONDS = (5.0, 10.0, 20.0, 40.0, 60.0)
CATALOGUE_MAX_AGE_S = 2 * 3600.0
# While a cycle is held the hourly catalogue refresh waits until the catalogue is this old, then makes one attempt:
# a slow or failing reload must not hold the worker while an exit may be due.
CATALOGUE_HOLD_REFRESH_S = CATALOGUE_MAX_AGE_S - 1800.0
ENTRIES_PER_TICK = 1
RECONCILE_WINDOW_S = 6 * 3600.0  # venue records this recent are compared with the journal before an entry
LATE_FILL_CHECK_S = 30.0         # how often a partially filled holding re-reads its venue orders
CONTEXT_RETRY_S = 5.0            # a candle frame that lacks the last closed bar is read again after this
FLATTEN_WINDOW_S = 120.0         # an Exit-all request keeps trying this long within the run it was made in
FLATTEN_STOP_WAIT_S = 10.0       # Exit all pressed while stopping waits this long for the worker to finish

SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS micro_cycles (
        cycle_id TEXT PRIMARY KEY, exchange TEXT NOT NULL, mode TEXT NOT NULL, symbol TEXT NOT NULL,
        state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
        strategy TEXT, decision_id TEXT, opportunity_key TEXT NOT NULL, budget_cap TEXT NOT NULL,
        reserved_quote TEXT NOT NULL DEFAULT '0', sizing TEXT,
        entry_intent_id TEXT, entry_order_id TEXT, entry_filled_base TEXT, entry_gross TEXT, entry_fee_quote TEXT,
        entry_fee_base TEXT, entry_fee_other TEXT, entry_fee_status TEXT, entry_debit TEXT, entry_partial INTEGER,
        entry_price TEXT, opened_at TEXT, opened_wall REAL,
        carried_base TEXT NOT NULL DEFAULT '0', carried_basis TEXT NOT NULL DEFAULT '0',
        owned_base TEXT NOT NULL DEFAULT '0', basis_quote TEXT NOT NULL DEFAULT '0',
        target_net TEXT, stop_net TEXT, max_hold_s REAL, peak_est_net TEXT,
        exit_count INTEGER NOT NULL DEFAULT 0, closed_base TEXT NOT NULL DEFAULT '0',
        exit_gross TEXT NOT NULL DEFAULT '0', exit_fees TEXT NOT NULL DEFAULT '0',
        exit_proceeds_net TEXT NOT NULL DEFAULT '0', realized_net TEXT NOT NULL DEFAULT '0',
        realized_status TEXT, last_exit_trigger TEXT, closed_at TEXT, closed_wall REAL,
        residual_base TEXT NOT NULL DEFAULT '0', residual_basis TEXT NOT NULL DEFAULT '0',
        last_reason TEXT, review_reason TEXT, late_fill_reason TEXT)''',
    # One open cycle per pair, and one cycle per opportunity, even across restarts.
    '''CREATE UNIQUE INDEX IF NOT EXISTS micro_one_open_cycle ON micro_cycles(exchange, mode, symbol)
        WHERE state NOT IN ('CLOSED', 'ABANDONED')''',
    'CREATE UNIQUE INDEX IF NOT EXISTS micro_one_cycle_per_opportunity ON micro_cycles(exchange, mode, opportunity_key)',
    'CREATE INDEX IF NOT EXISTS micro_cycles_recent ON micro_cycles(exchange, mode, closed_wall)',
    '''CREATE TABLE IF NOT EXISTS micro_cycle_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT NOT NULL, at TEXT NOT NULL,
        from_state TEXT, to_state TEXT NOT NULL, reason TEXT NOT NULL, data TEXT)''',
    'CREATE INDEX IF NOT EXISTS micro_cycle_events_cycle ON micro_cycle_events(cycle_id, id)',
)


def ensure_schema(db) -> None:
    for statement in SCHEMA:
        db.execute(statement)
    columns = {row[1] for row in db.execute('PRAGMA table_info(micro_cycles)').fetchall()}
    if 'late_fill_reason' not in columns:      # a table created by an earlier build of this mode
        db.execute('ALTER TABLE micro_cycles ADD COLUMN late_fill_reason TEXT')


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def _seconds_since(stamp: str | None) -> float | None:
    """Seconds from an ISO time this module wrote until now, or None when it cannot be read."""
    try:
        return round(max(0.0, time.time() - datetime.fromisoformat(str(stamp)).timestamp()), 1)
    except (TypeError, ValueError, OverflowError):
        return None


def _dec(value, default: Decimal | None = None) -> Decimal | None:
    if value is None or value == '':
        return default
    try:
        return money.parse(value, field='stored amount', allow_negative=True)
    except money.MoneyError:
        return default


class CycleError(RuntimeError):
    """An illegal cycle transition or a cycle that changed underneath an update."""


class VenueRecordUnreadable(RuntimeError):
    """The paper venue's record of an order could not be read (not the same as the venue having none)."""


def _refusal(message: str, code: str) -> CycleError:
    """A reservation refusal that names the waiting code the pair shows."""
    error = CycleError(message)
    error.code = code
    return error


# ---------------------------------------------------------------------- store
def transition(db, cycle: dict, to_state: str, reason: str, *, data: dict | None = None, **fields) -> dict:
    """Move ``cycle`` to ``to_state`` inside the caller's transaction (optimistic version check)."""
    current = db.execute('SELECT state, version FROM micro_cycles WHERE cycle_id=?', (cycle['cycle_id'],)).fetchone()
    if current is None:
        raise CycleError('cycle does not exist')
    if current['version'] != cycle['version'] or current['state'] != cycle['state']:
        raise CycleError(f'cycle changed underneath this update ({current["state"]} v{current["version"]})')
    if to_state != current['state'] and to_state not in TRANSITIONS[current['state']]:
        raise CycleError(f'illegal cycle transition {current["state"]} -> {to_state}')
    stamp = _now_iso()
    values = {**fields, 'state': to_state, 'updated_at': stamp, 'version': cycle['version'] + 1,
              'last_reason': str(reason)[:300]}
    assignments = ','.join(f'{name}=?' for name in values)
    db.execute(f'UPDATE micro_cycles SET {assignments} WHERE cycle_id=?', (*values.values(), cycle['cycle_id']))
    db.execute('INSERT INTO micro_cycle_events(cycle_id, at, from_state, to_state, reason, data) VALUES (?,?,?,?,?,?)',
               (cycle['cycle_id'], stamp, current['state'], to_state, str(reason)[:300],
                json.dumps(data, default=str, allow_nan=False) if data else None))
    updated = dict(cycle)
    updated.update(values)
    return updated


def load_cycle(cycle_id: str) -> dict | None:
    found = storage.rows('SELECT * FROM micro_cycles WHERE cycle_id=?', (cycle_id,))
    return found[0] if found else None


def open_cycles(exchange: str, mode: str) -> list[dict]:
    return storage.rows("SELECT * FROM micro_cycles WHERE exchange=? AND mode=? AND state NOT IN ('CLOSED','ABANDONED') "
                        'ORDER BY created_at', (exchange, mode))


def cycle_events(cycle_id: str) -> list[dict]:
    return storage.rows('SELECT * FROM micro_cycle_events WHERE cycle_id=? ORDER BY id', (cycle_id,))


# -------------------------------------------------------------------- manager
class MicroManager:
    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.RLock()
        self.metrics = observability.METRICS
        self._venue = None
        self._venue_exchange = None
        self.books = micro_book.BookTracker(max_age_s=30.0)
        self.context: dict[str, dict] = {}          # symbol -> features, regime and bar time
        self.rules_cache: dict[str, venue_rules.VenueRules] = {}
        self.rules_loaded_at: float | None = None
        self._rules_stamp: float | None = None       # the engine's markets_loaded_at the rules were built from
        self.pairs: dict[str, dict] = {}             # dashboard rows per pair
        self.counters: Counter = Counter()           # dominant waiting reason codes
        self.waiting_since: dict[str, float] = {}
        self.last_codes: dict[str, str] = {}
        self.exit_backoff: dict[str, tuple[int, float]] = {}
        self.decisions: deque = deque(maxlen=300)
        self.recovery: list[dict] = []
        self.last_tick = self.last_error = None
        self.tick_count = 0
        self._next_book: dict[str, float] = {}
        self._next_mark: dict[str, float] = {}      # residual marks keep their own schedule
        self._flatten_until = 0.0                    # monotonic deadline of a pending Exit-all request
        self._flatten_tried: set[str] = set()        # cycles already given their first Exit-all attempt
        self._late_checked: dict[str, float] = {}
        # The bar and gate state each pair's last entry decision was made on: an idle pair keeps that decision
        # between book reads only while both are unchanged.
        self._decided_bar: dict[str, tuple] = {}
        self._conflicts: dict[str, tuple[float, list]] = {}

    # ------------------------------------------------------------ plumbing
    @property
    def venue(self) -> micro_venue.MicroPaperVenue:
        """The durable paper venue of the engine's current exchange (rebuilt when the exchange changes)."""
        engine = self.engine
        if self._venue is None or self._venue_exchange != engine.exchange_name:
            self._venue = micro_venue.MicroPaperVenue(engine.exchange_name, rules=self.rules,
                                                      balances=engine._paper_balances)
            self._venue_exchange = engine.exchange_name
            self.books = micro_book.BookTracker(max_age_s=30.0)
            self.context.clear()
            self.rules_cache.clear()
            self._rules_stamp = None
            self.pairs.clear()
            self._decided_bar.clear()
            self._next_book.clear()
            self._next_mark.clear()
        return self._venue

    def active(self) -> bool:
        return micro_policy.is_micro(horizon=self.engine.cfg.get('time_horizon'))

    def _key(self, name: str) -> str:
        return storage.scope_key(f'micro.paper.{name}', self.engine.exchange_name)

    def params(self) -> strat.MicroParams:
        cfg = self.engine.cfg
        return strat.MicroParams(
            target_net_bps=float(cfg['micro_target_net_bps']), max_loss_bps=float(cfg['micro_max_loss_bps']),
            max_hold_s=float(cfg['micro_max_hold_minutes']) * 60.0, uncertainty_floor_bps=UNCERTAINTY_FLOOR_BPS,
            max_spread_bps=min(float(cfg['max_spread_pct']) * 100.0, MAX_SPREAD_BPS), max_vol_bps=MAX_VOL_BPS,
            min_efficiency_trend=MIN_EFFICIENCY_TREND, max_efficiency_range=MAX_EFFICIENCY_RANGE, edge_ratio=EDGE_RATIO)

    def budget_cap(self) -> Decimal:
        configured = money.parse(self.engine.cfg['micro_budget_usdt'], field='micro budget')
        return min(configured, econ.MICRO_MAX_ALL_IN_USDT)

    def allocation(self) -> Decimal:
        """The finite capital the micro strategy may commit at once: cycles x cap (never the paper balance)."""
        return money.mul(self.budget_cap(), Decimal(int(self.engine.cfg['micro_max_cycles'])))

    def target_net(self) -> Decimal:
        return money.mul(self.budget_cap(), money.bps(money.parse(self.engine.cfg['micro_target_net_bps'])))

    def selected(self) -> list[str]:
        """Only the operator's watchlist is tradable; discovery never adds pairs here."""
        return list(dict.fromkeys(self.engine.cfg['watchlist']))

    def open_symbols(self) -> set[str]:
        """Pairs with an unfinished cycle (holding, in flight or waiting for review)."""
        rows = storage.rows("SELECT symbol FROM micro_cycles WHERE exchange=? AND mode='paper' "
                            "AND state NOT IN ('CLOSED','ABANDONED')", (self.engine.exchange_name,))
        return {row['symbol'] for row in rows}

    def owned_symbols(self) -> set[str]:
        """Pairs whose ledger position belongs to the micro cycles: open cycles, and a closed
        cycle's retained remainder that is still in the ledger (the next cycle carries it).
        Only the newest closed remainder of a pair counts, and only while the ledger holds
        exactly that quantity, so a later position bought outside the cycles is never claimed.
        The legacy monitor and the manual ticket leave these alone. Fails closed: when the
        cycle table cannot be read, every paper position counts as micro-owned."""
        exchange = self.engine.exchange_name
        try:
            # One statement on one connection: the legacy monitor calls this on every protective tick.
            rows = storage.rows("SELECT c.symbol, NULL AS residual_base, NULL AS qty FROM micro_cycles c "
                                "WHERE c.exchange=? AND c.mode='paper' AND c.state NOT IN ('CLOSED','ABANDONED') "
                                "UNION ALL SELECT c.symbol, c.residual_base, p.qty FROM micro_cycles c "
                                "JOIN positions p ON p.exchange=c.exchange AND p.mode=c.mode AND p.symbol=c.symbol "
                                "WHERE c.exchange=? AND c.mode='paper' AND c.cycle_id=(SELECT r.cycle_id FROM "
                                "micro_cycles r WHERE r.exchange=c.exchange AND r.mode=c.mode AND r.symbol=c.symbol "
                                "AND r.state='CLOSED' AND r.residual_base IS NOT NULL AND r.residual_base NOT IN ('0', '') "
                                "ORDER BY r.closed_wall DESC, r.rowid DESC LIMIT 1)", (exchange, exchange))
            return {row['symbol'] for row in rows if row['residual_base'] is None
                    or math.isclose(float(row['qty']), float(_dec(row['residual_base'], money.ZERO)),
                                    rel_tol=1e-9, abs_tol=1e-12)}
        except Exception:
            return {row['symbol'] for row in storage.rows("SELECT symbol FROM positions WHERE exchange=? AND mode='paper'",
                                                          (exchange,))}

    def rules(self, symbol: str) -> venue_rules.VenueRules:
        cached = self.rules_cache.get(symbol)
        if cached is not None:
            return cached
        engine = self.engine
        market = engine._catalogue().get(symbol)
        precision_mode = getattr(engine.public, 'precisionMode', venue_rules.TICK_SIZE)
        rules = venue_rules.normalize(market, exchange=engine.exchange_name, symbol=symbol,
                                      loaded_at=self.rules_loaded_at, precision_mode=precision_mode)
        self.rules_cache[symbol] = rules
        return rules

    def cost(self, rules: venue_rules.VenueRules) -> econ.CostModel:
        cfg = self.engine.cfg
        return econ.cost_model(configured_fee_pct=cfg['fee_pct'], configured_slippage_pct=cfg['slippage_pct'],
                               latency_bps=cfg['micro_latency_bps'], rules=rules)

    def _cash(self) -> Decimal:
        return money.parse(float(self.engine._cash('paper')), field='paper cash')

    # ------------------------------------------------------------ latches
    def latch(self) -> dict | None:
        return self._latch_record(storage.get_meta(self._key('stop')))

    @staticmethod
    def _latch_record(raw: str | None) -> dict | None:
        if not raw:
            return None
        try:
            record = json.loads(raw)
        except ValueError:
            return {'reason': 'unreadable micro stop record', 'at': None}
        return record if isinstance(record, dict) and record.get('engaged') else None

    def _engage_latch(self, reason: str, **values) -> None:
        if self.latch():
            return
        record = {'engaged': True, 'reason': reason, 'at': _now_iso(), **values}
        storage.set_meta(self._key('stop'), json.dumps(record, default=str))
        storage.audit('error', f'MICRO STOP latched: {reason}. New micro entries are blocked until cleared with {RESUME_PHRASE}; '
                               'open cycles keep their exits.')
        observability.event('micro.stop_latched', level='error', reason=reason, **values)
        self.metrics.incr('micro.stop_latched')

    def clear_latch(self, confirmation: str) -> None:
        if confirmation != RESUME_PHRASE:
            raise storage.ConfigValidationError(f'Type {RESUME_PHRASE} to clear the micro stop.')
        storage.set_meta(self._key('stop'), json.dumps({'engaged': False, 'cleared_at': _now_iso(),
                                                         'cleared_wall': time.time()}))
        storage.audit('warning', 'Micro stop cleared after explicit confirmation. Losses before this moment no longer '
                                 'count toward the consecutive-loss stop; the daily loss stop counts from this level.')
        observability.event('micro.stop_cleared', level='warning')

    def _cleared_record(self) -> dict:
        try:
            record = json.loads(storage.get_meta(self._key('stop')) or '{}')
        except ValueError:
            return {}
        return record if isinstance(record, dict) and not record.get('engaged') else {}

    def _closed_since(self, since_wall: float) -> list[dict]:
        return storage.rows("SELECT * FROM micro_cycles WHERE exchange=? AND mode='paper' AND state='CLOSED' "
                            'AND closed_wall>=? ORDER BY closed_wall', (self.engine.exchange_name, since_wall))

    def _check_loss_limits(self) -> None:
        """Latch on consecutive losing cycles or the day's realized micro loss. Never self-clears."""
        cfg = self.engine.cfg
        cleared = self._cleared_record()
        cleared_wall = float(cleared.get('cleared_wall') or 0.0)
        recent = storage.rows("SELECT realized_net, closed_wall FROM micro_cycles WHERE exchange=? AND mode='paper' "
                              "AND state='CLOSED' AND closed_wall>? ORDER BY closed_wall DESC LIMIT 50",
                              (self.engine.exchange_name, cleared_wall))
        streak = 0
        for row in recent:
            if (_dec(row['realized_net'], money.ZERO) or money.ZERO) < 0:
                streak += 1
            else:
                break
        if streak >= int(cfg['micro_max_consecutive_losses']):
            self._engage_latch(f'{streak} consecutive losing micro cycles', streak=streak)
            return
        today_utc = datetime.fromtimestamp(time.time(), timezone.utc)   # the same clock as closed_wall
        day_start = today_utc.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        since = max(day_start, cleared_wall)
        today = money.add(*[(_dec(row['realized_net'], money.ZERO) or money.ZERO) for row in self._closed_since(since)])
        limit = money.parse(cfg['micro_daily_loss_limit_usdt'])
        if today <= -limit:
            self._engage_latch(f'realized micro loss today {money.display(today)} USDT reached the '
                               f'{money.display(limit)} USDT limit', realized_today=money.text(today))

    # ----------------------------------------------------------- recovery
    def recover_on_startup(self) -> list[dict]:
        """Read-only explanation of every unfinished cycle; only provably empty reservations are released.

        Runs while the engine is STOPPED in PAPER mode. Nothing is submitted.
        A reservation with no order intent, or an intent that never left
        (no ``submitted_at``), sent nothing and is released. Everything else
        keeps blocking new micro entries until the operator applies a
        recovery that books what the paper venue's own record shows.
        """
        explanations = []
        try:
            cycles = open_cycles(self.engine.exchange_name, 'paper')
        except Exception as exc:
            self.recovery = [{'error': f'micro cycle table unreadable: {type(exc).__name__}'}]
            return self.recovery
        for cycle in cycles:
            try:
                explanations.append(self._recover_cycle(cycle))
            except Exception as exc:
                explanations.append({'cycle_id': cycle['cycle_id'], 'symbol': cycle['symbol'], 'state': cycle['state'],
                                     'explanation': f'recovery check failed: {type(exc).__name__}; review required'})
        orphans = self._orphans()
        self._note_closed_late_fills(orphans)
        explanations.extend(orphans)
        try:
            # A close committed just before a crash never ran its loss-limit check.
            self._check_loss_limits()
        except Exception as exc:
            self._engage_latch(f'micro loss limits could not be checked at startup ({type(exc).__name__})')
        self.recovery = [item for item in explanations if item]
        if self.recovery:
            observability.event('micro.recovery', level='warning', items=self.recovery)
        return self.recovery

    def _intent(self, cycle_id: str, side: str, *, attempt: bool = False) -> dict | None:
        """The newest ``side`` intent of the cycle; with ``attempt``, only one that can belong to the current attempt.

        Booking marks an intent accounted in the same transaction that moves
        the cycle on, and a recovery settles one as reconciled the same way,
        so an accounted or reconciled intent seen while a cycle is mid-order
        belongs to an earlier, finished attempt (a partial exit before this
        one). Treating it as the current outcome would book that sale twice.
        """
        settled = ' AND status NOT IN (%s)' % ','.join('?' * len(SETTLED_INTENT_STATUSES)) if attempt else ''
        found = storage.rows(f'SELECT * FROM order_journal WHERE decision_id=? AND side=?{settled} '
                             'ORDER BY created_at DESC, rowid DESC LIMIT 1',
                             (cycle_id, side, *(SETTLED_INTENT_STATUSES if attempt else ())))
        return found[0] if found else None

    def _venue_record(self, intent: dict | None, *, strict: bool = False) -> dict | None:
        """The paper venue's record of ``intent``'s order, or None when the venue has none.

        With ``strict`` a failed read raises ``VenueRecordUnreadable``: a recovery must never take an order it
        could not look up for one that never filled (releasing a filled buy, or reopening a sold holding)."""
        if not intent or not intent.get('client_order_id'):
            return None
        try:
            return self.venue.fetch_order(None, intent['symbol'], {'clientOrderId': intent['client_order_id']})
        except ccxt.OrderNotFound:
            return None
        except Exception as exc:
            if strict:
                raise VenueRecordUnreadable(type(exc).__name__) from exc
            return None

    def _explain_record(self, leg: str, intent: dict) -> str:
        """What the paper venue's own record of ``intent`` shows, read now: the text the operator confirms."""
        try:
            record = self._venue_record(intent, strict=True)
        except VenueRecordUnreadable as exc:
            return (f'The paper venue record of the {leg} intent {intent["intent_id"][:8]} could not be read ({exc}), '
                    'so its outcome is still unknown. Applying recovery is refused until the record can be read; '
                    'nothing is booked or released meanwhile.')
        return self._describe_record(leg, intent, record)

    def _recovery_record(self, intent: dict | None) -> dict | None:
        try:
            return self._venue_record(intent, strict=True)
        except VenueRecordUnreadable as exc:
            raise storage.ConfigValidationError(f'The paper venue record could not be read ({exc}). Nothing was '
                                                'booked or released; apply the recovery again once it can be '
                                                'read.') from exc

    def _recover_cycle(self, cycle: dict) -> dict | None:
        state, symbol = cycle['state'], cycle['symbol']
        base = {'cycle_id': cycle['cycle_id'], 'symbol': symbol, 'state': state}
        if state in ('ENTRY_RESERVED', 'ENTRY_SUBMITTED', 'ENTRY_UNKNOWN'):
            intent = self._intent(cycle['cycle_id'], 'BUY', attempt=True)
            if intent is None or (intent['status'] == 'pending' and not intent.get('submitted_at')) \
                    or intent['status'] == 'rejected':
                with storage.connection() as db:
                    if intent is not None and intent['status'] == 'pending':
                        storage.update_order_intent(intent['intent_id'], 'rejected', note='never sent; released by the recovery check', db=db)
                    transition(db, cycle, 'ABANDONED', 'released: no order left this computer',
                               reserved_quote='0')
                return {**base, 'state': 'ABANDONED', 'explanation': 'The reservation was released: no order intent '
                        'had been sent, so nothing can have filled.'}
            explanation = self._explain_record('entry', intent)
            if state != 'ENTRY_UNKNOWN':
                with storage.connection() as db:
                    transition(db, cycle, 'ENTRY_UNKNOWN', 'interrupted during the entry order', review_reason=explanation)
            elif cycle.get('review_reason') != explanation:
                with storage.connection() as db:
                    transition(db, cycle, 'ENTRY_UNKNOWN', 'outcome still unknown; explained from the venue record',
                               review_reason=explanation)
            return {**base, 'state': 'ENTRY_UNKNOWN', 'explanation': explanation, 'action': 'apply_recovery'}
        if state in ('EXIT_SUBMITTED', 'EXIT_UNKNOWN'):
            intent = self._intent(cycle['cycle_id'], 'SELL', attempt=True)
            if intent is None or (intent['status'] == 'pending' and not intent.get('submitted_at')) \
                    or intent['status'] == 'rejected':
                with storage.connection() as db:
                    if intent is not None and intent['status'] == 'pending':
                        storage.update_order_intent(intent['intent_id'], 'rejected', note='never sent; released by the recovery check', db=db)
                    transition(db, cycle, 'HOLDING', 'exit order never left; the holding is unchanged')
                return {**base, 'state': 'HOLDING', 'explanation': 'The exit order was never sent; the holding is '
                        'unchanged and will be managed when the bot starts.'}
            explanation = self._explain_record('exit', intent)
            if state != 'EXIT_UNKNOWN':
                with storage.connection() as db:
                    transition(db, cycle, 'EXIT_UNKNOWN', 'interrupted during the exit order', review_reason=explanation)
            elif cycle.get('review_reason') != explanation:
                with storage.connection() as db:
                    transition(db, cycle, 'EXIT_UNKNOWN', 'outcome still unknown; explained from the venue record',
                               review_reason=explanation)
            return {**base, 'state': 'EXIT_UNKNOWN', 'explanation': explanation, 'action': 'apply_recovery'}
        if state == 'HOLDING':
            problem = self._ownership_problem(cycle)
            if problem:
                with storage.connection() as db:
                    transition(db, cycle, 'REVIEW', problem, review_reason=problem)
                return {**base, 'state': 'REVIEW', 'explanation': problem, 'action': 'apply_recovery'}
            return None
        if state == 'REVIEW':
            return {**base, 'explanation': cycle.get('review_reason') or 'review required', 'action': 'apply_recovery'}
        return None

    def settle_stranded(self) -> list[dict]:
        """Explain and convert cycles left mid-operation, exactly as a restart would. Submits nothing.

        ENTRY_RESERVED, ENTRY_SUBMITTED and EXIT_SUBMITTED exist only while
        one entry or exit runs. Call this only when none runs (the start of a
        tick, the worker's exit, a recovery while stopped): a cycle still in
        one of them was interrupted by a failure between its durable writes,
        and is released, returned to HOLDING, or moved to ENTRY_UNKNOWN or
        EXIT_UNKNOWN, where it blocks new entries until it is recovered.
        """
        found = []
        for cycle in open_cycles(self.engine.exchange_name, 'paper'):
            if cycle['state'] not in TRANSIENT_STATES:
                continue
            try:
                item = self._recover_cycle(cycle)
            except Exception as exc:
                item = {'cycle_id': cycle['cycle_id'], 'symbol': cycle['symbol'], 'state': cycle['state'],
                        'explanation': f'recovery check failed: {type(exc).__name__}; review required'}
            if item:
                found.append(item)
        if found:
            known = {item.get('cycle_id') for item in found}
            self.recovery = [item for item in self.recovery if item.get('cycle_id') not in known] + found
            storage.audit('error', 'Micro: an entry or exit was interrupted by a failure; ' + '; '.join(
                f'{item["symbol"]} now {item["state"]}' for item in found) + '.')
            observability.event('micro.stranded_settled', level='error', items=found)
        return found

    @staticmethod
    def _describe_record(leg: str, intent: dict, record: dict | None) -> str:
        if record is None:
            return (f'The {leg} intent {intent["intent_id"][:8]} was sent but the paper venue has no record of it, so '
                    'no fill happened. Applying recovery releases it.')
        exact = (record.get('info') or {}).get('exact') or {}
        return (f'The paper venue reports the {leg} order {record.get("id")} as {record.get("status")} with '
                f'{exact.get("filled", record.get("filled"))} filled for {exact.get("cost", record.get("cost"))} '
                f'(fee {exact.get("fee_cost")} {exact.get("fee_currency")}). Applying recovery books exactly that.')

    def _ownership_problem(self, cycle: dict) -> str | None:
        position = storage.rows('SELECT qty, cost FROM positions WHERE exchange=? AND mode=? AND symbol=?',
                                (cycle['exchange'], cycle['mode'], cycle['symbol']))
        owned = _dec(cycle['owned_base'], money.ZERO)
        if not position:
            return f'The ledger has no {cycle["symbol"]} position although the cycle owns {money.text(owned)}.'
        held = float(position[0]['qty'])
        if not math.isclose(held, float(owned), rel_tol=1e-9, abs_tol=1e-12):
            return (f'The ledger holds {held!r} {cycle["symbol"].split("/")[0]} but the cycle owns {money.text(owned)}; '
                    'something outside the cycle changed this position.')
        return None

    def _orphans(self, symbol: str | None = None, *, since_ms: int | None = None) -> list[dict]:
        """Venue orders that no journal intent explains, or that filled more than the ledger booked."""
        found = []
        try:
            records = self.venue.records(symbol, since_ms=since_ms)
        except Exception as exc:
            return [{'symbol': symbol, 'explanation': f'paper venue records unreadable: {type(exc).__name__}',
                     'action': 'review'}]
        for record in records:
            client_id = record.get('clientOrderId') or record.get('client_order_id')
            journal = storage.rows('SELECT intent_id, status, filled_quantity, decision_id FROM order_journal '
                                   'WHERE exchange=? AND mode=? AND client_order_id=?',
                                   (self.engine.exchange_name, 'paper', client_id))
            if not journal:
                found.append({'symbol': record.get('symbol'), 'order_id': record.get('order_id') or record.get('id'),
                              'explanation': 'The paper venue holds an order no journal intent explains (orphan).',
                              'action': 'review'})
                continue
            row = journal[0]
            exact = (record.get('info') or {}).get('exact') or {}
            venue_filled = _dec(exact.get('filled'), None)
            if venue_filled is None:
                venue_filled = _dec(record.get('filled'), money.ZERO)
            if row['status'] == 'accounted' and row['filled_quantity'] is not None and venue_filled is not None:
                booked = money.parse(float(row['filled_quantity']), field='booked fill')
                if venue_filled > booked * (1 + Decimal('1e-12')):
                    found.append({'symbol': record.get('symbol'), 'order_id': record.get('order_id') or record.get('id'),
                                  'cycle_id': row['decision_id'],
                                  'explanation': f'The paper venue now reports {money.text(venue_filled)} filled for an '
                                                 f'order booked at {money.text(booked)} (a late fill after the cancel).',
                                  'action': 'review'})
        return found

    def _note_closed_late_fills(self, found: list[dict]) -> None:
        """Make a late fill reported for a closed cycle durable: it blocks entries until recovered.

        The reconciliation that reports it only looks back RECONCILE_WINDOW_S;
        without this record the block would lift by itself once the venue
        record aged out of that window, and the fill would never be booked.
        """
        for item in found:
            cycle_id = item.get('cycle_id')
            if not cycle_id:
                continue
            with storage.connection() as db:
                row = db.execute('SELECT state, late_fill_reason FROM micro_cycles WHERE cycle_id=?',
                                 (cycle_id,)).fetchone()
                if row is None or row['state'] not in TERMINAL or row['late_fill_reason']:
                    continue
                reason = (item['explanation'] + ' The cycle had already closed. Stop the bot and apply the recovery '
                          'to book it; new micro entries stay blocked until then.')
                stamp = _now_iso()
                db.execute('UPDATE micro_cycles SET late_fill_reason=?, updated_at=?, version=version+1 '
                           'WHERE cycle_id=?', (reason[:600], stamp, cycle_id))
                db.execute('INSERT INTO micro_cycle_events(cycle_id, at, from_state, to_state, reason, data) '
                           'VALUES (?,?,?,?,?,?)', (cycle_id, stamp, row['state'], row['state'],
                                                     'late fill reported after close',
                                                     json.dumps({'order_id': item.get('order_id')})))
            storage.audit('error', f'Micro {item.get("symbol")}: {reason}')
            observability.event('micro.late_fill_after_close', level='error', cycle_id=cycle_id,
                                order_id=item.get('order_id'))

    def _closed_late_fills(self) -> list[dict]:
        return storage.rows("SELECT * FROM micro_cycles WHERE exchange=? AND mode='paper' AND state IN "
                            "('CLOSED','ABANDONED') AND late_fill_reason IS NOT NULL ORDER BY created_at",
                            (self.engine.exchange_name,))

    def _late_fills(self, cycle: dict) -> list[tuple[dict, dict, Decimal]]:
        """(intent, venue view, extra base) for each accounted order of ``cycle`` the venue later filled further."""
        found = []
        for intent in storage.rows("SELECT * FROM order_journal WHERE decision_id=? AND status='accounted' "
                                   'ORDER BY created_at', (cycle['cycle_id'],)):
            view = self._venue_record(intent)
            if view is None or intent.get('filled_quantity') is None:
                continue
            try:
                venue_filled = self._fill(view)['filled']
            except Exception:
                continue
            booked = money.parse(float(intent['filled_quantity']), field='booked fill')
            if venue_filled > booked:
                found.append((intent, view, money.sub(venue_filled, booked)))
        return found

    def _check_late_fills(self, cycle: dict) -> dict:
        """A partially filled holding re-reads its venue orders now and then; a later fill sends it to review."""
        if not (int(cycle.get('entry_partial') or 0) or int(cycle.get('exit_count') or 0)):
            return cycle
        now = time.monotonic()
        if now - self._late_checked.get(cycle['cycle_id'], -1e9) < LATE_FILL_CHECK_S:
            return cycle
        self._late_checked[cycle['cycle_id']] = now
        late = self._late_fills(cycle)
        if not late:
            return cycle
        detail = '; '.join(f'{intent["side"]} order {view.get("id")} now shows {money.text(extra)} more filled'
                           for intent, view, extra in late)
        reason = (f'The paper venue reports a late fill ({detail}) after the order was booked. Stop the bot and '
                  'apply the recovery to book it exactly.')
        with storage.connection() as db:
            cycle = transition(db, cycle, 'REVIEW', 'late fill after booking', review_reason=reason)
        storage.audit('error', f'Micro {cycle["symbol"]}: {reason}')
        observability.event('micro.late_fill_detected', level='error', cycle_id=cycle['cycle_id'], detail=detail)
        return cycle

    def _book_late_fills(self, cycle: dict, late: list) -> str:
        """Book only the extra quantity each order filled after it was booked, in one transaction.

        An open cycle in review owns the result and returns to HOLDING (or
        closes). A closed cycle is never reopened: its totals and realized
        result are corrected, the ledger position is updated, and a sellable
        holding that results is adopted by a new HOLDING cycle so its exits
        manage it (an unsellable one stays as a residual the next cycle on the
        pair carries). Returns the outcome sentence.
        """
        engine = self.engine
        symbol = cycle['symbol']
        adopted = None
        with storage.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            fresh = dict(db.execute('SELECT * FROM micro_cycles WHERE cycle_id=?', (cycle['cycle_id'],)).fetchone())
            if fresh['version'] != cycle['version']:
                raise CycleError('the cycle changed before the late fill could be booked')
            closed = fresh['state'] in TERMINAL
            position = db.execute('SELECT qty, cost FROM positions WHERE exchange=? AND mode=? AND symbol=?',
                                  (engine.exchange_name, 'paper', symbol)).fetchone()
            if closed:
                # The ledger position is what is left of a closed cycle (its residual, if no later cycle carried it),
                # never a position bought outside the cycles: the late fill would be booked into it and adopted.
                outside = self._outside_position(db, symbol, position) if position else None
                if outside:
                    raise CycleError(outside)
                owned = money.parse(float(position['qty']), field='ledger quantity') if position else money.ZERO
                basis = money.parse(float(position['cost']), field='ledger cost') if position else money.ZERO
            else:
                owned = _dec(fresh['owned_base'], money.ZERO)
                basis = _dec(fresh['basis_quote'], money.ZERO)
            key = engine._key('paper_cash')
            cash = money.parse(float(db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()['value']),
                               field='paper cash')
            realized_total = _dec(fresh['realized_net'], money.ZERO)
            totals = {name: _dec(fresh[name], money.ZERO) for name in (
                'entry_filled_base', 'entry_gross', 'entry_fee_quote', 'entry_debit', 'closed_base', 'exit_gross',
                'exit_fees', 'exit_proceeds_net')}
            for intent, view, _extra in late:
                total = self._fill(view)
                booked = db.execute('SELECT COALESCE(SUM(quantity),0) AS q, COALESCE(SUM(gross),0) AS g, '
                                    'COALESCE(SUM(fee),0) AS f FROM trades WHERE exchange=? AND mode=? AND order_id=?',
                                    (engine.exchange_name, 'paper', view.get('id'))).fetchone()
                quote_fee, base_fee, other = self._split_fee(symbol, total)
                extra_gross = money.sub(total['gross'], money.parse(float(booked['g']), field='booked gross'))
                extra_fee = money.sub(quote_fee or money.ZERO, money.parse(float(booked['f']), field='booked fee'))
                extra_filled = money.sub(total['filled'], money.parse(float(intent['filled_quantity']),
                                                                      field='booked fill'))
                if intent['side'] == 'BUY':
                    extra_base = money.sub(money.sub(total['filled'], base_fee or money.ZERO),
                                           money.parse(float(booked['q']), field='booked quantity'))
                    if extra_base <= 0 or extra_gross < 0 or extra_fee < 0:
                        raise CycleError('the late entry fill does not add a positive quantity')
                    debit = money.add(extra_gross, extra_fee)
                    if debit > cash:
                        raise CycleError('paper cash below the late fill debit')
                    cash, owned, basis = money.sub(cash, debit), money.add(owned, extra_base), money.add(basis, debit)
                    for name, delta in (('entry_filled_base', extra_filled), ('entry_gross', extra_gross),
                                        ('entry_fee_quote', extra_fee), ('entry_debit', debit)):
                        totals[name] = money.add(totals[name], delta)
                    db.execute('INSERT INTO trades(created_at,exchange,mode,symbol,side,price,quantity,gross,fee,reason,'
                               'order_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                               (_now_iso(), engine.exchange_name, 'paper', symbol, 'BUY', float(total['average']),
                                float(extra_base), float(extra_gross), float(extra_fee),
                                'Micro cycle entry (late fill)', view.get('id')))
                else:
                    extra_sold = money.sub(total['filled'], money.parse(float(booked['q']), field='booked quantity'))
                    if extra_sold <= 0 or extra_gross < 0 or extra_fee < 0:
                        raise CycleError('the late exit fill does not add a positive quantity')
                    if extra_sold > owned:
                        raise CycleError(f'the venue reports selling {money.text(extra_sold)} more than the ledger holds '
                                         f'({money.text(owned)}); this cannot be booked automatically')
                    realized = econ.realize_exit(owned_base=owned, basis_quote=basis, sold_base=extra_sold,
                                                 gross_quote=extra_gross, quote_fee=extra_fee, base_fee=None,
                                                 other_fees={}, other_fee_value=None, fee_status=total['fee_status'])
                    cash = money.add(cash, realized.net_proceeds)
                    owned, basis = realized.residual_base, realized.residual_basis
                    realized_total = money.add(realized_total, realized.realized_net)
                    for name, delta in (('closed_base', realized.closed_base), ('exit_gross', extra_gross),
                                        ('exit_fees', extra_fee), ('exit_proceeds_net', realized.net_proceeds)):
                        totals[name] = money.add(totals[name], delta)
                    db.execute('INSERT INTO trades(created_at,exchange,mode,symbol,side,price,quantity,gross,fee,realized,'
                               'reason,order_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                               (_now_iso(), engine.exchange_name, 'paper', symbol, 'SELL', float(total['average']),
                                float(extra_sold), float(extra_gross), float(extra_fee), float(realized.realized_net),
                                'Micro exit (late fill)', view.get('id')))
                storage.update_order_intent(intent['intent_id'], 'accounted', db=db,
                                            filled_quantity=float(total['filled']), filled_price=float(total['average']),
                                            note='late fill booked by operator recovery')
            db.execute('UPDATE meta SET value=? WHERE key=?', (repr(float(cash)), key))
            if owned > 0:
                updated = db.execute('UPDATE positions SET qty=?, cost=? WHERE exchange=? AND mode=? AND symbol=?',
                                     (float(owned), float(basis), engine.exchange_name, 'paper', symbol)).rowcount
                if not updated:
                    db.execute('INSERT INTO positions(exchange,mode,symbol,qty,entry_price,cost,highest,stop,target,'
                               'opened_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                               (engine.exchange_name, 'paper', symbol, float(owned), float(money.div(basis, owned)),
                                float(basis), float(money.div(basis, owned)), 0.0, 0.0, _now_iso()))
            else:
                db.execute('DELETE FROM positions WHERE exchange=? AND mode=? AND symbol=?',
                           (engine.exchange_name, 'paper', symbol))
            aggregates = {name: money.text(value) for name, value in totals.items()}
            if not closed:
                transition(db, fresh, 'HOLDING' if owned > 0 else 'CLOSED', 'operator recovery: late fill booked',
                           owned_base=money.text(owned), basis_quote=money.text(basis),
                           realized_net=money.text(realized_total), review_reason=None, **aggregates,
                           **({} if owned > 0 else {'closed_at': _now_iso(), 'closed_wall': time.time()}))
                outcome = 'late fill booked from the venue record'
            else:
                stamp = _now_iso()
                assignments = ','.join(f'{name}=?' for name in aggregates)
                db.execute(f'UPDATE micro_cycles SET {assignments}, realized_net=?, late_fill_reason=NULL, updated_at=?, '
                           'version=version+1 WHERE cycle_id=?',
                           (*aggregates.values(), money.text(realized_total), stamp, fresh['cycle_id']))
                db.execute('INSERT INTO micro_cycle_events(cycle_id, at, from_state, to_state, reason, data) '
                           'VALUES (?,?,?,?,?,?)', (fresh['cycle_id'], stamp, fresh['state'], fresh['state'],
                                                     'operator recovery: late fill booked after close',
                                                     json.dumps({'owned_after': money.text(owned),
                                                                 'realized_net': money.text(realized_total)})))
                outcome = 'late fill booked from the venue record; the closed cycle\'s totals were corrected'
                # The ledger position is the one residual record of the pair: earlier records are superseded.
                db.execute("UPDATE micro_cycles SET residual_base='0', residual_basis='0' WHERE exchange=? AND mode='paper' "
                           "AND symbol=? AND state='CLOSED'", (engine.exchange_name, symbol))
                if owned > 0 and self._sellable(symbol, owned, money.div(basis, owned)):
                    adopted = self._adopt_holding(db, fresh, owned, basis)
                    outcome += f'; a new cycle {adopted[:12]} now manages the {money.text(owned)} held'
                elif owned > 0:
                    db.execute('UPDATE micro_cycles SET residual_base=?, residual_basis=? WHERE cycle_id=?',
                               (money.text(owned), money.text(basis), fresh['cycle_id']))
                    outcome += f'; {money.text(owned)} is below the venue minimum and stays as a residual'
        if not closed and owned <= 0:
            self._after_close(cycle)
        if closed:
            self._after_close(cycle)     # a corrected realized result counts toward the loss limits
        return outcome

    def _outside_position(self, db, symbol: str, position) -> str | None:
        """Why the pair's ledger row is not the remainder the closed cycles retain, or None when it is (or is dust).

        The newest closed cycle with a residual holds the pair's current remainder: each close records all
        that is left, a later cycle carries it, and a late-fill booking keeps one residual record per pair.
        """
        held = float(position['qty'])
        row = db.execute("SELECT residual_base FROM micro_cycles WHERE exchange=? AND mode='paper' AND symbol=? "
                         "AND state='CLOSED' AND residual_base IS NOT NULL AND residual_base NOT IN ('0', '') "
                         'ORDER BY closed_wall DESC LIMIT 1', (self.engine.exchange_name, symbol)).fetchone()
        retained = float(_dec(row['residual_base'], money.ZERO)) if row is not None else 0.0
        if math.isclose(held, retained, rel_tol=1e-9, abs_tol=1e-12):
            return None
        price = float(position['cost']) / held if held > 0 else 0.0
        if self.engine._is_dust(dict(position), self.engine._catalogue().get(symbol), price):
            return None          # a remainder below the venue minimum is carried, as a new entry carries it
        return (f'the ledger holds {held!r} {symbol.split("/")[0]}, outside the micro cycles (the closed cycles retain '
                f'{retained!r}); close that manual or legacy position first, then apply the recovery')

    def late_fill_symbols(self) -> set[str]:
        """Pairs with a closed cycle whose late fill is not booked yet (its recovery books into the pair's position)."""
        return {cycle['symbol'] for cycle in self._closed_late_fills()}

    def _sellable(self, symbol: str, quantity: Decimal, price: Decimal) -> bool:
        rules = self.rules(symbol)
        step = rules.amount_step
        amount = money.q_down(quantity, step) if step is not None else quantity
        minimum_cost = rules.market_min_cost()
        return bool(amount > 0 and (rules.min_amount is None or amount >= rules.min_amount)
                    and (minimum_cost is None or money.mul(amount, price) >= minimum_cost))

    def _adopt_holding(self, db, closed: dict, owned: Decimal, basis: Decimal) -> str:
        """A new HOLDING cycle for a holding a late entry fill created after its cycle closed (inside ``db``)."""
        params = self.params()
        cycle_id = 'cyc_' + uuid.uuid4().hex[:24]
        stamp = _now_iso()
        cap = self.budget_cap()
        db.execute('INSERT INTO micro_cycles(cycle_id, exchange, mode, symbol, state, created_at, updated_at, strategy, '
                   'decision_id, opportunity_key, budget_cap, reserved_quote, owned_base, basis_quote, entry_price, '
                   'opened_at, opened_wall, target_net, stop_net, max_hold_s, realized_status, last_reason) '
                   'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (cycle_id, closed['exchange'], 'paper', closed['symbol'], 'HOLDING', stamp, stamp,
                    'late_fill_recovery', closed['cycle_id'], f'{closed["symbol"]}|late_fill|{closed["cycle_id"]}|{stamp}',
                    money.text(cap), '0', money.text(owned), money.text(basis), money.text(money.div(basis, owned)),
                    stamp, time.time(), money.text(self.target_net()),
                    money.text(money.mul(cap, money.bps(money.parse(params.max_loss_bps)))), float(params.max_hold_s),
                    'confirmed', 'adopted a holding created by a late entry fill after its cycle closed'))
        db.execute('INSERT INTO micro_cycle_events(cycle_id, at, from_state, to_state, reason, data) VALUES (?,?,?,?,?,?)',
                   (cycle_id, stamp, None, 'HOLDING', 'adopted a late-fill holding',
                    json.dumps({'from_cycle': closed['cycle_id'], 'owned': money.text(owned),
                                'basis': money.text(basis)})))
        return cycle_id

    def _apply_closed_late_fill(self, cycle: dict) -> dict:
        others = [c for c in open_cycles(self.engine.exchange_name, 'paper') if c['symbol'] == cycle['symbol']]
        if others:
            raise storage.ConfigValidationError(f'Exit or recover the open {cycle["symbol"]} cycle first; the late fill '
                                                'changes the same ledger position.')
        late = self._late_fills(cycle)
        if not late:
            with storage.connection() as db:
                stamp = _now_iso()
                db.execute('UPDATE micro_cycles SET late_fill_reason=NULL, updated_at=?, version=version+1 '
                           'WHERE cycle_id=?', (stamp, cycle['cycle_id']))
                db.execute('INSERT INTO micro_cycle_events(cycle_id, at, from_state, to_state, reason, data) '
                           'VALUES (?,?,?,?,?,?)', (cycle['cycle_id'], stamp, cycle['state'], cycle['state'],
                                                     'operator recovery: the venue no longer reports a late fill', None))
            return {'cycle_id': cycle['cycle_id'], 'outcome': 'the venue no longer reports a late fill; nothing booked'}
        try:
            outcome = self._book_late_fills(cycle, late)
        except CycleError as exc:
            raise storage.ConfigValidationError(f'The late fill was not booked: {exc}.') from exc
        return {'cycle_id': cycle['cycle_id'], 'outcome': outcome}

    def _symbol_conflicts(self, symbol: str, now: float, *, force: bool = False) -> list[dict]:
        """Recent venue orders on ``symbol`` the journal does not explain (re-read at most every 10 s)."""
        checked = self._conflicts.get(symbol)
        mono = time.monotonic()
        if not force and checked is not None and mono - checked[0] < 10.0:
            return checked[1]
        found = self._orphans(symbol, since_ms=int((now - RECONCILE_WINDOW_S) * 1000))
        self._note_closed_late_fills(found)
        self._conflicts[symbol] = (mono, found)
        if found:
            known = {(item.get('order_id'), item.get('explanation')) for item in self.recovery}
            self.recovery.extend(item for item in found if (item.get('order_id'), item.get('explanation')) not in known)
        return found

    def _final_answer(self, symbol: str, intent: dict | None, order: dict) -> dict:
        """A canceled or partial answer is read once more: a late fill can race the cancel acknowledgement."""
        if order.get('status') == 'closed' or not intent or not intent.get('client_order_id'):
            return order
        try:
            fresh = self.venue.fetch_order(None, symbol, {'clientOrderId': intent['client_order_id']})
            before, after = self._fill(order)['filled'], self._fill(fresh)['filled']
        except Exception:
            return order
        if after <= before:
            return order
        observability.event('micro.late_fill_reconciled', level='warning', symbol=symbol, intent_id=intent['intent_id'],
                            first_answer=money.text(before), final=money.text(after))
        self.metrics.incr('micro.late_fill_reconciled')
        return fresh

    def review_block(self) -> list[dict]:
        """Unfinished outcomes that block every new micro entry.

        A mid-operation state is listed too when no operation can be running (stopped and idle): it was
        interrupted and its check could not explain it, so it blocks entries like an unknown outcome."""
        blockers = []
        idle = not self.engine._busy()
        for cycle in open_cycles(self.engine.exchange_name, 'paper'):
            if cycle['state'] in BLOCKING_STATES:
                blockers.append({'cycle_id': cycle['cycle_id'], 'symbol': cycle['symbol'], 'state': cycle['state'],
                                 'explanation': self._blocking_explanation(cycle)})
            elif idle and cycle['state'] in TRANSIENT_STATES:
                blockers.append({'cycle_id': cycle['cycle_id'], 'symbol': cycle['symbol'], 'state': cycle['state'],
                                 'explanation': f'Interrupted ({cycle.get("last_reason") or "mid-operation"}); its '
                                                'check has not explained it yet. Applying recovery reads the paper '
                                                "venue's record; nothing is resent.", 'interrupted': True})
        for cycle in self._closed_late_fills():
            blockers.append({'cycle_id': cycle['cycle_id'], 'symbol': cycle['symbol'], 'state': cycle['state'],
                             'explanation': cycle['late_fill_reason'], 'late_fill': True})
        return blockers

    def _blocking_explanation(self, cycle: dict) -> str:
        """An unknown outcome is explained by the venue record its recovery would book, read now."""
        if cycle['state'] in ('ENTRY_UNKNOWN', 'EXIT_UNKNOWN'):
            side, leg = ('BUY', 'entry') if cycle['state'] == 'ENTRY_UNKNOWN' else ('SELL', 'exit')
            try:
                intent = self._intent(cycle['cycle_id'], side, attempt=True)
                if intent is not None:
                    return self._explain_record(leg, intent)
            except Exception:
                pass
        return cycle.get('review_reason') or cycle.get('last_reason') or 'review required'

    def apply_recovery(self, cycle_id: str, confirmation: str) -> dict:
        """Operator-confirmed: book exactly what the paper venue's record shows. Bot must be stopped."""
        engine = self.engine
        if confirmation != REVIEW_PHRASE:
            raise storage.ConfigValidationError(f'Type {REVIEW_PHRASE} to apply this recovery.')
        with engine.lock:
            if engine._busy():
                raise storage.ConfigValidationError('Stop the bot and wait for active operations before applying a recovery.')
            if engine.mode != 'paper':
                raise storage.ConfigValidationError(micro_policy.PAPER_ONLY)
            engine.control_busy = True
        try:
            cycle = load_cycle(cycle_id)
            if cycle is None or cycle['exchange'] != engine.exchange_name or cycle['mode'] != 'paper':
                raise storage.ConfigValidationError('Unknown micro cycle for this exchange.')
            if cycle['state'] in TRANSIENT_STATES:
                # Interrupted mid-operation while the bot ran: explain it as a restart would, then recover.
                self.settle_stranded()
                cycle = load_cycle(cycle_id)
                explanation = next((item.get('explanation') for item in self.recovery
                                    if item.get('cycle_id') == cycle_id), cycle.get('last_reason'))
                if cycle['state'] in TRANSIENT_STATES:
                    raise storage.ConfigValidationError(f'The interrupted {cycle["symbol"]} cycle could not be checked '
                                                        f'({explanation}). Nothing was booked; apply the recovery '
                                                        'again, or restart the app.')
                if cycle['state'] not in BLOCKING_STATES:
                    return {'cycle_id': cycle_id, 'outcome': f'{cycle["state"].lower()}: {explanation}'}
            if cycle['state'] in TERMINAL and cycle.get('late_fill_reason'):
                result = self._apply_closed_late_fill(cycle)
            elif cycle['state'] not in BLOCKING_STATES:
                raise storage.ConfigValidationError('This cycle does not need a recovery.')
            elif cycle['state'] == 'ENTRY_UNKNOWN':
                result = self._apply_entry_recovery(cycle)
            elif cycle['state'] == 'EXIT_UNKNOWN':
                result = self._apply_exit_recovery(cycle)
            else:
                result = self._apply_review(cycle)
            storage.audit('warning', f'Micro cycle {cycle_id[:12]} recovered by the operator: {result["outcome"]}.')
            observability.event('micro.recovery_applied', level='warning', cycle_id=cycle_id, outcome=result['outcome'])
            self.recovery = [item for item in self.recovery if item.get('cycle_id') != cycle_id]
            return result
        finally:
            with engine.lock:
                engine.control_busy = False

    def _settle_intent_for_recovery(self, intent: dict, db, *, accounted: bool, order_id=None, record=None) -> None:
        if intent['status'] in ('accounted', 'rejected', 'reconciled'):
            return
        if intent['status'] != 'uncertain':
            storage.update_order_intent(intent['intent_id'], 'uncertain', note='restart before booking', db=db)
        if accounted:
            summary = record or {}
            storage.update_order_intent(intent['intent_id'], 'accounted', order_id=order_id, db=db,
                                        filled_price=summary.get('average'), filled_quantity=summary.get('filled'),
                                        note='booked from the paper venue record by operator recovery')
        else:
            storage.update_order_intent(intent['intent_id'], 'reconciled', db=db,
                                        note='paper venue holds no such order; released by operator recovery')

    def _apply_entry_recovery(self, cycle: dict) -> dict:
        intent = self._intent(cycle['cycle_id'], 'BUY', attempt=True)
        record = self._recovery_record(intent)
        filled = None
        if record is not None:
            exact = (record.get('info') or {}).get('exact') or {}
            filled = _dec(exact.get('filled'), None) if exact else _dec(record.get('filled'), None)
        if record is None or not filled:
            with storage.connection() as db:
                if intent is not None:
                    self._settle_intent_for_recovery(intent, db, accounted=False)
                transition(db, cycle, 'ABANDONED', 'operator recovery: the venue shows no fill', reserved_quote='0')
            return {'cycle_id': cycle['cycle_id'], 'outcome': 'released (no fill)'}
        booked = self._book_entry(cycle, intent, record, recovery=True)
        if not booked:
            raise storage.ConfigValidationError('The venue record could not be booked; see local activity.')
        return {'cycle_id': cycle['cycle_id'], 'outcome': 'entry fill booked from the venue record'}

    def _apply_exit_recovery(self, cycle: dict) -> dict:
        intent = self._intent(cycle['cycle_id'], 'SELL', attempt=True)
        record = self._recovery_record(intent)
        filled = None
        if record is not None:
            exact = (record.get('info') or {}).get('exact') or {}
            filled = _dec(exact.get('filled'), None) if exact else _dec(record.get('filled'), None)
        if record is None or not filled:
            with storage.connection() as db:
                if intent is not None:
                    self._settle_intent_for_recovery(intent, db, accounted=False)
                transition(db, cycle, 'HOLDING', 'operator recovery: the exit did not fill')
            return {'cycle_id': cycle['cycle_id'], 'outcome': 'holding unchanged (exit did not fill)'}
        booked = self._book_exit(cycle, intent, record, trigger=cycle.get('last_exit_trigger') or 'recovery',
                                 reason='exit booked by operator recovery', recovery=True)
        if not booked:
            raise storage.ConfigValidationError('The venue record could not be booked; see local activity.')
        return {'cycle_id': cycle['cycle_id'], 'outcome': 'exit fill booked from the venue record'}

    def _apply_review(self, cycle: dict) -> dict:
        late = self._late_fills(cycle)
        if late:
            try:
                outcome = self._book_late_fills(cycle, late)
            except CycleError as exc:
                raise storage.ConfigValidationError(f'The late fill was not booked: {exc}.') from exc
            return {'cycle_id': cycle['cycle_id'], 'outcome': outcome}
        position = storage.rows('SELECT qty, cost FROM positions WHERE exchange=? AND mode=? AND symbol=?',
                                (cycle['exchange'], cycle['mode'], cycle['symbol']))
        with storage.connection() as db:
            if position:
                qty = money.parse(float(position[0]['qty']), field='ledger quantity')
                cost = money.parse(float(position[0]['cost']), field='ledger cost')
                transition(db, cycle, 'HOLDING', 'operator review: the cycle adopts the ledger position',
                           owned_base=money.text(qty), basis_quote=money.text(cost), review_reason=None)
                return {'cycle_id': cycle['cycle_id'], 'outcome': 'cycle adopted the ledger position'}
            transition(db, cycle, 'CLOSED', 'operator review: the ledger holds nothing for this cycle',
                       closed_at=_now_iso(), closed_wall=time.time(), realized_status='unconfirmed', review_reason=None)
        return {'cycle_id': cycle['cycle_id'], 'outcome': 'cycle closed (no ledger position); realized result unconfirmed'}

    # -------------------------------------------------------------- data
    def _refresh_catalogue(self, *, holding: bool = False) -> None:
        engine = self.engine
        self._adopt_catalogue()          # a reload made outside this pass (a chart, a backtest, a manual order)
        if holding and engine.markets_loaded_at and self.rules_loaded_at is not None:
            if time.time() - self.rules_loaded_at < CATALOGUE_HOLD_REFRESH_S:
                return
            engine._fetch_markets(attempts=1)
        else:
            engine._fetch_markets()
        self._adopt_catalogue()
        if self.rules_loaded_at is None:
            self.rules_loaded_at = time.time()
            self.rules_cache.clear()

    def _adopt_catalogue(self) -> None:
        """Rebuild the rules whenever the engine's catalogue is not the load they were built from, whoever
        reloaded it, and age them from that load."""
        engine = self.engine
        loaded = engine.markets_loaded_at
        if loaded and loaded != self._rules_stamp:
            self._rules_stamp = loaded
            self.rules_loaded_at = time.time() - max(0.0, time.monotonic() - loaded)
            self.rules_cache.clear()

    def _poll_interval(self, count: int) -> float:
        rate = getattr(self.engine.public, 'rateLimit', None)
        spacing = 0.25
        if isinstance(rate, (int, float)) and not isinstance(rate, bool) and math.isfinite(rate) and rate > 0:
            spacing = max(spacing, 3.0 * float(rate) / 1000.0)
        return max(1.0, spacing * max(1, count))

    def _fetch_book(self, symbol: str) -> micro_book.BookSnapshot:
        engine = self.engine
        started = time.monotonic()
        raw = engine.data.order_book(symbol, limit=BOOK_LEVELS)
        received_mono = time.monotonic()
        received_wall = raw.get('_received_at') if isinstance(raw, dict) else None
        if not isinstance(received_wall, (int, float)) or isinstance(received_wall, bool):
            received_wall = time.time()
        snapshot = micro_book.from_ccxt_order_book(raw, exchange=engine.exchange_name, symbol=symbol,
                                                   received_wall=float(received_wall), received_mono=received_mono,
                                                   clock_offset_s=engine._clock_offset(), max_levels=BOOK_LEVELS)
        resets = self.books.resets(symbol)
        snapshot = self.books.accept(snapshot)
        if self.books.resets(symbol) > resets:
            observability.event('micro.book_sequence_restart', level='warning', symbol=symbol,
                                sequence=snapshot.venue_sequence)
            storage.audit('warning', f'{symbol}: the venue restarted its order-book update ids; '
                                     f'books are read again from id {snapshot.venue_sequence}.')
        self.metrics.observe('micro.book_fetch_ms', (received_mono - started) * 1000)
        self._mark(symbol, snapshot)
        return snapshot

    def _mark(self, symbol: str, snapshot) -> None:
        """Feed the engine's marks, which the risk gate's freshness checks read."""
        ticker = {'bid': float(snapshot.best_bid), 'ask': float(snapshot.best_ask),
                  'timestamp': snapshot.venue_timestamp_ms, '_received_at': snapshot.received_wall}
        try:
            self.engine._observe_ticker(symbol, ticker)
        except ValueError:
            pass

    def _book(self, symbol: str, *, interval: float, max_age: float, idle: bool = False,
              force: bool = False) -> micro_book.BookSnapshot | None:
        """A book no older than ``max_age``, read at most every ``interval``.

        Exits (``idle`` False) read a new book whenever none is fresh enough,
        so a holding is never left without one. An ``idle`` read honours its
        interval even then and returns None until it is due. ``force`` reads
        now whatever the schedule, unless a book younger than half of
        ``max_age`` can be reused (so it is still fresh when the decision is
        made), and the next read is then due ``interval`` after it.
        """
        now_mono = time.monotonic()
        current = self.books.latest(symbol, now_mono=now_mono, max_age_s=max_age)
        reuse = min(interval, max_age / 2.0) if force else interval
        if current is not None and current.age_seconds(now_mono) < reuse:
            return current
        if not force and now_mono < self._next_book.get(symbol, 0.0) and (current is not None or idle):
            return current
        self._next_book[symbol] = now_mono + interval
        try:
            return self._fetch_book(symbol)
        except micro_book.BookError as exc:
            self._note_error(symbol, f'order book refused: {exc.code}')
        except Exception as exc:
            self._note_error(symbol, f'order book unavailable: {type(exc).__name__}')
        return self.books.latest(symbol, now_mono=time.monotonic(), max_age_s=max_age)

    def _ticker_snapshot(self, symbol: str):
        """Top of book from the stream or a REST quote; depth unknown, for safety exits only.

        The paper venue sells against a top-of-book quote only up to its stated
        bid size, so a stream quote that states none gives way to the REST book
        ticker, which carries its own size, price and time (never a REST size on
        a stream price). If that read fails, the stream quote still prices the
        exit decision.
        """
        engine = self.engine
        streamed = None
        try:
            ticker = engine._quote(symbol, None)
            if isinstance(ticker, dict):
                streamed = self._quote_snapshot(symbol, ticker)
                if streamed is not None and streamed.bids[0][1] is not None:
                    return streamed
            quotes = engine.data.quotes([symbol])
            ticker = quotes.get(symbol) if isinstance(quotes, dict) else None
        except Exception:
            ticker = None
        return (self._quote_snapshot(symbol, ticker) if isinstance(ticker, dict) else None) or streamed

    def _quote_snapshot(self, symbol: str, ticker: dict):
        """A fresh one-level snapshot of ``ticker`` (and the engine mark it feeds), or None."""
        engine = self.engine
        try:
            received = ticker.get('_received_at')
            received = float(received) if isinstance(received, (int, float)) and not isinstance(received, bool) else time.time()
            snapshot = micro_book.from_ticker(ticker, exchange=engine.exchange_name, symbol=symbol,
                                              received_wall=received, received_mono=time.monotonic(),
                                              clock_offset_s=engine._clock_offset(),
                                              source='stream_ticker' if ticker.get('_source') else 'ticker')
        except Exception:
            return None
        age = time.time() - snapshot.received_wall
        if age > float(engine.cfg['micro_book_max_age_seconds']) or age < -5:
            return None
        self._mark(symbol, snapshot)
        return snapshot

    def _bar_due(self) -> int:
        """Open time (exchange seconds) of the last completed 1m bar."""
        now = time.time() - self.engine._clock_offset()
        return int(now // 60) * 60 - 60

    def _context_due(self, symbol: str, bar_due: int) -> bool:
        cached = self.context.get(symbol)
        if not cached or cached.get('bar_due') != bar_due:
            return True
        return not cached.get('current') and time.monotonic() >= cached.get('retry_mono', 0.0)

    def _prefetch_context(self, symbols: list[str]) -> None:
        """Start the due candle downloads together (exchanges whose rate limit allows overlap).

        ``MarketData.prefetch`` keeps the shared request pacing and the quote
        slot that order-book and exit reads use, so this only overlaps round
        trips; it never sends requests faster than the venue limit allows.
        """
        bar_due = self._bar_due()
        due = [symbol for symbol in symbols if self._context_due(symbol, bar_due)]
        if not due:
            return
        try:
            self.engine.data.prefetch([(symbol, '1m', 120) for symbol in due] + [(symbol, '5m', 60) for symbol in due])
        except Exception:
            pass      # each symbol's own read reports the failure

    def _refresh_context(self, symbol: str) -> dict | None:
        """Completed-bar features after each 1m close; cached until the next bar completes.

        A frame that does not yet contain the bar that just closed (a cached
        copy from the previous minute, or a venue that publishes the bar a
        moment late) is kept but marked not current: it is read again after
        ``CONTEXT_RETRY_S`` and, until then, an entry decision treats the
        data as not fresh rather than acting on the previous bar.
        """
        engine = self.engine
        bar_due = self._bar_due()
        cached = self.context.get(symbol)
        if cached and not self._context_due(symbol, bar_due):
            return cached
        started = time.monotonic()
        try:
            frame_1m = engine.data.candles(symbol, '1m', limit=120)
            frame_5m = engine.data.candles(symbol, '5m', limit=60)
            features, reason = strat.compute_features(symbol, frame_1m, frame_5m)
        except Exception as exc:
            features, reason = None, f'ERROR: candles unavailable ({type(exc).__name__})'
        self.metrics.observe('micro.context_ms', (time.monotonic() - started) * 1000)
        if features is not None:
            current = int(features.bar_ts_ms // 1000) >= bar_due
        else:
            current = str(reason or '').startswith('WARMUP')
        entry = {'bar_due': bar_due, 'features': features, 'reason': reason, 'computed_wall': time.time(),
                 'current': current, 'retry_mono': time.monotonic() + CONTEXT_RETRY_S}
        if features is not None and not current:
            entry['reason'] = 'STALE: the last completed 1m bar is not in the candle data yet'
        self.context[symbol] = entry
        return entry

    def _note_error(self, symbol: str, message: str) -> None:
        row = self.pairs.setdefault(symbol, {'symbol': symbol})
        row['error'] = message
        self.last_error = f'{symbol}: {message}'

    # -------------------------------------------------------------- tick
    def request_flatten(self) -> None:
        """Exit every holding from the running worker, retrying within FLATTEN_WINDOW_S of this run only."""
        self._flatten_until = time.monotonic() + FLATTEN_WINDOW_S
        self._flatten_tried = set()
        storage.audit('warning', 'Operator asked to exit all micro positions; the running bot is selling them.')

    def cancel_flatten(self, why: str) -> None:
        """An Exit-all request never outlives the run it was made in."""
        if self._flatten_until:
            self._flatten_until = 0.0
            remaining = [c['symbol'] for c in open_cycles(self.engine.exchange_name, 'paper') if c['state'] == 'HOLDING']
            if remaining:
                storage.audit('warning', f'Exit all micro positions ended ({why}) with {", ".join(remaining)} still held; '
                                         'press Exit all again to sell them.')

    def flatten_pending(self) -> float:
        """Seconds left on a pending Exit-all request (0 when none)."""
        return max(0.0, self._flatten_until - time.monotonic()) if self._flatten_until else 0.0

    def _run_flatten(self, params) -> None:
        if not self._flatten_until:
            return
        held = [c for c in open_cycles(self.engine.exchange_name, 'paper') if c['state'] == 'HOLDING']
        if not held:
            self._flatten_until = 0.0
            storage.audit('info', 'Exit all micro positions finished: nothing is held.')
            return
        if time.monotonic() >= self._flatten_until:
            self.cancel_flatten(f'no sale within {FLATTEN_WINDOW_S:g} s')
            return
        for cycle in held:
            if not self.engine._orders_allowed():
                return
            first = cycle['cycle_id'] not in self._flatten_tried
            self._flatten_tried.add(cycle['cycle_id'])
            # The first attempt ignores an exit back-off; a refused or unfilled one then waits like any exit.
            self._manage_exit(cycle, params, operator=True, ignore_backoff=first)

    def tick(self) -> float:
        """One pass of exits, context and entries. Returns seconds until the next pass."""
        engine = self.engine
        started = time.monotonic()
        refusal = micro_policy.live_refusal(engine.mode, horizon=engine.cfg.get('time_horizon'))
        if refusal:
            with engine.lock:
                engine.risk_block = refusal
            engine.stop_event.set()
            storage.audit('error', refusal)
            return 1.0
        self.venue   # bind the venue to the current exchange before anything else
        self.settle_stranded()      # no operation runs between ticks: a mid-operation state here was interrupted
        if not engine._orders_allowed():
            return 1.0
        params = self.params()
        held = [c for c in open_cycles(engine.exchange_name, 'paper') if c['state'] == 'HOLDING']
        checked = bool(engine.markets_loaded_at)
        if checked:
            # Holdings are checked before the hourly catalogue refresh, which can be slow or failing.
            self._run_flatten(params)
            self._manage_exits(params)
        try:
            self._refresh_catalogue(holding=bool(held))
        except Exception as exc:
            self.last_error = f'market catalogue unavailable: {type(exc).__name__}'
            return 1.0 if held and checked else 5.0
        if not checked:
            self._run_flatten(params)
        self._manage_exits(params, only_due=checked)
        self._mark_residuals()
        selected = self.selected()
        # Held pairs need their candle context for the regime exit even after leaving the selection.
        watched = selected + sorted({c['symbol'] for c in held} - set(selected))
        self._prefetch_context(watched)
        for symbol in watched:
            if not engine._orders_allowed():
                break
            self._refresh_context(symbol)
            self._manage_exits(params, only_due=True)
        if engine._orders_allowed():
            self._entries(params, selected)
        self.tick_count += 1
        self.last_tick = _now_iso()
        elapsed = (time.monotonic() - started) * 1000
        self.metrics.observe('micro.tick_ms', elapsed)
        current = engine.feed
        streaming = current is not None and getattr(current, 'healthy', lambda: False)()
        return 1.0 if streaming or held else 2.0

    def _mark_residuals(self) -> None:
        """Keep a fresh mark for a closed cycle's residual that no open cycle reads.

        The legacy monitor leaves micro-owned pairs alone, and the risk gate
        refuses every entry while any held asset's mark is stale; a residual
        on a pair that left the selection would otherwise block all micro
        entries. The interval keeps the mark well inside the gate's age limit.
        """
        engine = self.engine
        interval = max(1.0, min(IDLE_BOOK_SECONDS, float(engine.cfg['data_max_age_seconds']) / 3.0))
        max_age = float(engine.cfg['micro_book_max_age_seconds'])
        for symbol in sorted(self.owned_symbols() - self.open_symbols()):
            if not engine._orders_allowed():
                return
            # A schedule of its own, in both directions: the idle entry cadence of a pair that is still selected
            # must not delay the residual's mark, and the mark must not delay that pair's own reads.
            now_mono = time.monotonic()
            if now_mono < self._next_mark.get(symbol, 0.0):
                continue
            self._next_mark[symbol] = now_mono + interval
            if self.books.latest(symbol, now_mono=now_mono, max_age_s=min(max_age, interval)) is not None:
                continue                        # that book's read already refreshed the mark
            try:
                self._fetch_book(symbol)
            except micro_book.BookError as exc:
                self._note_error(symbol, f'order book refused: {exc.code}')
            except Exception as exc:
                self._note_error(symbol, f'order book unavailable: {type(exc).__name__}')

    # ------------------------------------------------------------- exits
    def _manage_exits(self, params, *, only_due: bool = False) -> None:
        engine = self.engine
        # Positions outside the micro cycles keep their stored-exit checks at every point the held cycles are
        # re-checked (between pairs, around the catalogue refresh); the monitor returns at once until it is due.
        engine._monitor_if_due()
        cycles = [c for c in open_cycles(engine.exchange_name, 'paper') if c['state'] == 'HOLDING']
        interval = self._poll_interval(len(cycles))
        for cycle in cycles:
            if not engine._orders_allowed():
                return
            if only_due and time.monotonic() < self._next_book.get(cycle['symbol'], 0.0):
                continue
            self._manage_exit(cycle, params, interval=interval)

    def _manage_exit(self, cycle: dict, params, *, operator: bool = False, interval: float = 1.0,
                     ignore_backoff: bool = False) -> None:
        engine = self.engine
        symbol = cycle['symbol']
        cycle = self._check_late_fills(cycle)
        if cycle['state'] != 'HOLDING':
            return
        started = time.monotonic()
        max_age = float(engine.cfg['micro_book_max_age_seconds'])
        rules = self.rules(symbol)
        cost = self.cost(rules)
        book = self._book(symbol, interval=interval, max_age=max_age)
        if book is None:
            book = self._ticker_snapshot(symbol)
        owned = _dec(cycle['owned_base'], money.ZERO)
        basis = _dec(cycle['basis_quote'], money.ZERO)
        target_net = _dec(cycle['target_net'], self.target_net())
        stop_net = _dec(cycle['stop_net'], money.mul(self.budget_cap(), money.bps(Decimal(str(params.max_loss_bps)))))
        estimate = None
        depth_model = 'unavailable'
        if book is not None and owned > 0:
            try:
                estimate_obj = econ.estimate_exit(owned_base=owned, basis_quote=basis, rules=rules, book=book, cost=cost,
                                                  target_net=target_net,
                                                  uncertainty_bps=money.parse(max(params.uncertainty_floor_bps,
                                                                                  float(cost.latency_bps))))
                estimate = estimate_obj.as_dict()
                depth_model = estimate_obj.depth_model
            except Exception as exc:
                self._note_error(symbol, f'exit estimate failed: {type(exc).__name__}')
        context = self.context.get(symbol) or {}
        # A regime exit reads only a context of the last completed bar (or the one before, while the new bar's
        # candles are read): a frozen context would fire, or fail to fire, on a market that has moved on.
        current = bool(context.get('current')) and (context.get('bar_due') or 0) >= self._bar_due() - 60
        features = context.get('features') if current else None
        regime = None
        if features is not None and book is not None:
            regime, _ = strat.classify_regime(features, spread_bps=float(book.spread_bps), depth_ok=True,
                                              data_fresh=True, params=params)
        peak = _dec(cycle.get('peak_est_net'))
        # stop_net is an amount for the whole cycle: what a partial exit already lost counts against it.
        verdict = strat.evaluate_exit(estimate=estimate, depth_model=depth_model, target_net=target_net,
                                      stop_net=stop_net, peak_est_net=peak, opened_wall=float(cycle['opened_wall'] or 0),
                                      max_hold_s=float(cycle['max_hold_s'] or params.max_hold_s), regime=regime,
                                      now=time.time(), realized_net=_dec(cycle.get('realized_net'), money.ZERO))
        self.metrics.observe('micro.exit_decision_ms', (time.monotonic() - started) * 1000)
        with self.lock:
            row = self.pairs.setdefault(symbol, {'symbol': symbol})
            row['exit'] = {'cycle_id': cycle['cycle_id'], 'estimate': estimate, 'verdict': verdict.as_dict(),
                           'depth_model': depth_model, 'at': _now_iso(), 'at_wall': time.time()}
        if verdict.new_peak is not None and (peak is None or verdict.new_peak > peak):
            gap = money.mul(self.budget_cap(), Decimal('0.0001'))
            if peak is None or verdict.new_peak - peak >= gap:
                try:
                    with storage.connection() as db:
                        cycle = transition(db, cycle, 'HOLDING', 'new estimated net peak',
                                           peak_est_net=money.text(verdict.new_peak))
                except CycleError:
                    return
        trigger, reason = verdict.trigger, verdict.reason
        if operator:
            trigger, reason = 'operator', 'Operator asked to exit all micro positions'
        elif verdict.action != 'exit':
            return
        if book is None:
            self._note_error(symbol, 'exit wanted but no fresh executable quote is available')
            return
        backoff = self.exit_backoff.get(cycle['cycle_id'])
        if backoff and time.monotonic() < backoff[1] and not ignore_backoff:
            return
        self._sell(cycle, book, rules, cost, trigger=trigger, reason=reason)

    def _sell(self, cycle: dict, book, rules, cost, *, trigger: str, reason: str) -> bool:
        engine = self.engine
        symbol = cycle['symbol']
        owned = _dec(cycle['owned_base'], money.ZERO)
        step = rules.amount_step
        sellable = money.q_down(owned, step) if step is not None else owned
        minimum_ok = (sellable > 0 and (rules.min_amount is None or sellable >= rules.min_amount)
                      and (rules.market_min_cost() is None
                           or money.mul(sellable, book.best_bid) >= rules.market_min_cost()))
        if not minimum_ok:
            # Only an unsellable remainder is left: the cycle closes and keeps it as residual.
            with storage.connection() as db:
                transition(db, cycle, 'CLOSED', 'only an unsellable remainder is left; retained as residual',
                           closed_at=_now_iso(), closed_wall=time.time(), residual_base=money.text(owned),
                           residual_basis=cycle['basis_quote'],
                           realized_status=cycle.get('realized_status') or 'confirmed')
            self._after_close(cycle)
            return False
        positions = engine._active_positions('paper')
        price = book.best_bid
        authorization = engine._authorize(symbol, 'SELL', 'micro_exit', float(money.mul(sellable, price)), float(price),
                                          positions=positions, cash=None, quantity=float(sellable),
                                          reason=f'{trigger}: {reason}', decision_id=cycle['cycle_id'])
        if not authorization.allowed:
            self._backoff(cycle)
            self._note_error(symbol, 'exit refused by the risk gate: ' + authorization.reason)
            return False
        try:
            with storage.connection() as db:
                cycle = transition(db, cycle, 'EXIT_SUBMITTED', f'{trigger}: {reason}', last_exit_trigger=trigger)
        except CycleError:
            return False
        submitted = time.monotonic()
        intent, order = engine._submit(self.venue, symbol, 'SELL', quantity=float(sellable), authorization=authorization,
                                       intended_price=float(price), decision_id=cycle['cycle_id'],
                                       venue_params={'book': book, 'adverse_bps': cost.latency_bps,
                                                     'taker_fee': cost.taker_fee})
        self.metrics.observe('micro.exit_submit_ms', (time.monotonic() - submitted) * 1000)
        if order is None:
            self._unsent_or_unknown(cycle, 'SELL')
            return False
        intent_row = storage.order_intent(intent)
        booked = self._book_exit(cycle, intent_row, self._final_answer(symbol, intent_row, order), trigger=trigger,
                                 reason=reason)
        if booked:
            self.exit_backoff.pop(cycle['cycle_id'], None)
        return booked

    def _backoff(self, cycle: dict) -> None:
        count, _ = self.exit_backoff.get(cycle['cycle_id'], (0, 0.0))
        delay = EXIT_RETRY_SECONDS[min(count, len(EXIT_RETRY_SECONDS) - 1)]
        self.exit_backoff[cycle['cycle_id']] = (count + 1, time.monotonic() + delay)

    def _unsent_or_unknown(self, cycle: dict, side: str) -> None:
        """After _submit returned nothing: a refusal/rejection or an uncertain outcome."""
        intent = self._intent(cycle['cycle_id'], side, attempt=True)
        fresh = load_cycle(cycle['cycle_id']) or cycle
        with storage.connection() as db:
            if intent is None or intent['status'] == 'rejected':
                if side == 'BUY':
                    transition(db, fresh, 'ABANDONED', 'entry not filled: ' + (intent or {}).get('note', 'not sent'),
                               reserved_quote='0')
                else:
                    transition(db, fresh, 'HOLDING', 'exit not filled: ' + (intent or {}).get('note', 'not sent'))
                    self._backoff(cycle)
            else:
                to_state = 'ENTRY_UNKNOWN' if side == 'BUY' else 'EXIT_UNKNOWN'
                transition(db, fresh, to_state, f'{side} outcome uncertain ({intent["status"]}); review required',
                           review_reason=f'The {side.lower()} order outcome is uncertain. Stop the bot and review the '
                                         'recovery explanation.')
        if intent is not None and intent['status'] != 'rejected':
            observability.event('micro.outcome_unknown', level='error', cycle_id=cycle['cycle_id'], side=side,
                                intent_id=intent['intent_id'])

    # ---------------------------------------------------------- booking
    @staticmethod
    def _fill(order: dict) -> dict:
        """Exact fill figures: the venue's exact strings when present, else the CCXT floats (repr)."""
        exact = (order.get('info') or {}).get('exact') if isinstance(order.get('info'), dict) else None
        if isinstance(exact, dict) and exact.get('filled') is not None:
            filled = money.parse(exact['filled'], field='filled')
            gross = money.parse(exact['cost'], field='cost')
            fee_cost = _dec(exact.get('fee_cost'))
            fee_currency = exact.get('fee_currency')
            status = 'simulated'
        else:
            filled = money.parse(order.get('filled'), field='filled')
            gross = money.parse(order.get('cost'), field='cost')
            fee = order.get('fee') if isinstance(order.get('fee'), dict) else {}
            fee_cost = _dec(fee.get('cost'))
            fee_currency = fee.get('currency')
            status = 'observed'
        if filled <= 0 or gross <= 0:
            raise money.MoneyError('the fill has no positive quantity and cost')
        return {'filled': filled, 'gross': gross, 'fee_cost': fee_cost, 'fee_currency': fee_currency,
                'fee_status': status if fee_cost is not None and fee_currency else 'unknown',
                'average': money.div(gross, filled)}

    def _split_fee(self, symbol: str, fill: dict) -> tuple[Decimal | None, Decimal | None, dict]:
        base, quote = symbol.split('/')
        fee, currency = fill['fee_cost'], fill['fee_currency']
        if fee is None or not currency:
            return None, None, {}
        if currency == quote:
            return fee, money.ZERO, {}
        if currency == base:
            return money.ZERO, fee, {}
        return money.ZERO, money.ZERO, {str(currency): fee}

    def _book_entry(self, cycle: dict, intent: dict | None, order: dict, *, recovery: bool = False) -> bool:
        engine = self.engine
        symbol = cycle['symbol']
        started = time.monotonic()
        try:
            fill = self._fill(order)
            quote_fee, base_fee, other = self._split_fee(symbol, fill)
            reserved = _dec(cycle['reserved_quote'], money.ZERO)
            acquisition = econ.account_entry(filled_base=fill['filled'], gross_quote=fill['gross'], quote_fee=quote_fee,
                                             base_fee=base_fee, other_fees=other, other_fee_value=None,
                                             fee_status=fill['fee_status'], reserved=reserved)
        except Exception as exc:
            engine._ledger_failure(f'MICRO BUY {symbol}: {type(exc).__name__}; fill accounting requires review',
                                   intent_id=(intent or {}).get('intent_id'), contain=True)
            return False
        order_id = order.get('id')
        sizing = json.loads(cycle.get('sizing') or '{}')
        try:
            with storage.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                key = engine._key('paper_cash')
                cash = float(db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()['value'])
                debit = float(acquisition.debit)
                if debit > cash + 1e-9:
                    raise ValueError('paper cash below the booked debit')
                db.execute('UPDATE meta SET value=? WHERE key=?', (repr(cash - debit), key))
                existing = db.execute('SELECT qty, cost FROM positions WHERE exchange=? AND mode=? AND symbol=?',
                                      (engine.exchange_name, 'paper', symbol)).fetchone()
                carried_base = _dec(cycle['carried_base'], money.ZERO)
                carried_basis = _dec(cycle['carried_basis'], money.ZERO)
                if existing is not None:
                    if not math.isclose(float(existing['qty']), float(carried_base), rel_tol=1e-9, abs_tol=1e-12):
                        raise ValueError('the carried remainder changed before booking')
                elif carried_base > 0:
                    raise ValueError('the carried remainder disappeared before booking')
                if carried_base > 0:
                    # This cycle now owns the remainder: the closed cycles that retained it no longer do.
                    db.execute("UPDATE micro_cycles SET residual_base='0', residual_basis='0' WHERE exchange=? "
                               "AND mode='paper' AND symbol=? AND state='CLOSED' AND cycle_id<>?",
                               (engine.exchange_name, symbol, cycle['cycle_id']))
                owned = money.add(acquisition.owned_base, carried_base)
                basis = money.add(acquisition.basis, carried_basis)
                price = float(fill['average'])
                stop_bid = sizing.get('stop_bid')
                target_bid = sizing.get('target_bid')
                stop_value = float(stop_bid) if stop_bid else price * (1 - float(engine.cfg['micro_max_loss_bps']) / 10000)
                target_value = float(target_bid) if target_bid else price * 1.01
                if existing is None:
                    db.execute('INSERT INTO positions(exchange,mode,symbol,qty,entry_price,cost,highest,stop,target,opened_at) '
                               'VALUES (?,?,?,?,?,?,?,?,?,?)',
                               (engine.exchange_name, 'paper', symbol, float(owned), price, float(basis), price,
                                stop_value, target_value, _now_iso()))
                else:
                    db.execute('UPDATE positions SET qty=?,entry_price=?,cost=?,highest=?,stop=?,target=?,opened_at=? '
                               'WHERE exchange=? AND mode=? AND symbol=?',
                               (float(owned), price, float(basis), price, stop_value, target_value, _now_iso(),
                                engine.exchange_name, 'paper', symbol))
                if order_id is not None and db.execute(
                        'SELECT 1 FROM trades WHERE exchange=? AND mode=? AND order_id=? LIMIT 1',
                        (engine.exchange_name, 'paper', order_id)).fetchone():
                    raise ValueError('this venue order is already booked')
                db.execute('INSERT INTO trades(created_at,exchange,mode,symbol,side,price,quantity,gross,fee,reason,order_id) '
                           'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           (_now_iso(), engine.exchange_name, 'paper', symbol, 'BUY', price,
                            float(acquisition.owned_base), float(fill['gross']),
                            float(money.add(quote_fee or money.ZERO, money.mul(base_fee or money.ZERO, fill['average']))),
                            'Micro cycle entry' + (' (recovered)' if recovery else ''), order_id))
                if intent is not None and not recovery:
                    storage.update_order_intent(intent['intent_id'], 'accounted', order_id=order_id, db=db,
                                                filled_quantity=float(fill['filled']), filled_price=price)
                elif intent is not None:
                    self._settle_intent_for_recovery(intent, db, accounted=True, order_id=order_id,
                                                     record={'average': price, 'filled': float(fill['filled'])})
                fresh = dict(db.execute('SELECT * FROM micro_cycles WHERE cycle_id=?', (cycle['cycle_id'],)).fetchone())
                partial = bool(order.get('status') != 'closed')
                transition(db, fresh, 'HOLDING', 'entry filled' + (' (partially)' if partial else ''),
                           data={'debit': money.text(acquisition.debit), 'owned': money.text(acquisition.owned_base),
                                 'fee_status': acquisition.fee_status, 'notes': list(acquisition.notes)},
                           reserved_quote='0', entry_intent_id=(intent or {}).get('intent_id'), entry_order_id=order_id,
                           entry_filled_base=money.text(fill['filled']), entry_gross=money.text(fill['gross']),
                           entry_fee_quote=money.text(quote_fee), entry_fee_base=money.text(base_fee),
                           entry_fee_other=json.dumps({k: money.text(v) for k, v in other.items()}) if other else None,
                           entry_fee_status=acquisition.fee_status, entry_debit=money.text(acquisition.debit),
                           entry_partial=int(partial), entry_price=money.text(fill['average']), opened_at=_now_iso(),
                           opened_wall=time.time(), owned_base=money.text(owned), basis_quote=money.text(basis),
                           review_reason=None)
        except Exception as exc:
            engine._ledger_failure(f'MICRO BUY {symbol}: {type(exc).__name__}; portfolio ledger requires review',
                                   intent_id=(intent or {}).get('intent_id'), contain=True)
            return False
        self.metrics.observe('micro.fill_to_commit_ms', (time.monotonic() - started) * 1000)
        try:
            storage.audit('trade', f'PAPER MICRO BUY {symbol}: {money.display(acquisition.owned_base, 8)} owned for '
                                   f'{money.display(acquisition.debit)} USDT all-in (cap {money.display(self.budget_cap(), 2)}).')
            observability.event('micro.entry_booked', cycle_id=cycle['cycle_id'], symbol=symbol,
                                debit=money.text(acquisition.debit), owned=money.text(acquisition.owned_base),
                                fee_status=acquisition.fee_status, order_id=order_id)
        except Exception:
            pass
        engine._observe_fills('paper')
        return True

    def _book_exit(self, cycle: dict, intent: dict | None, order: dict, *, trigger: str, reason: str,
                   recovery: bool = False) -> bool:
        engine = self.engine
        symbol = cycle['symbol']
        started = time.monotonic()
        try:
            fill = self._fill(order)
            quote_fee, base_fee, other = self._split_fee(symbol, fill)
            owned = _dec(cycle['owned_base'], money.ZERO)
            basis = _dec(cycle['basis_quote'], money.ZERO)
            realized = econ.realize_exit(owned_base=owned, basis_quote=basis, sold_base=fill['filled'],
                                         gross_quote=fill['gross'], quote_fee=quote_fee, base_fee=base_fee,
                                         other_fees=other, other_fee_value=None, fee_status=fill['fee_status'])
        except Exception as exc:
            engine._ledger_failure(f'MICRO SELL {symbol}: {type(exc).__name__}; fill accounting requires review',
                                   intent_id=(intent or {}).get('intent_id'), contain=True)
            return False
        order_id = order.get('id')
        rules = self.rules(symbol)
        residual = realized.residual_base
        step = rules.amount_step
        sellable_rest = money.q_down(residual, step) if step is not None else residual
        rest_sellable = bool(sellable_rest > 0 and (rules.min_amount is None or sellable_rest >= rules.min_amount))
        try:
            with storage.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                key = engine._key('paper_cash')
                cash = float(db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()['value'])
                db.execute('UPDATE meta SET value=? WHERE key=?', (repr(cash + float(realized.net_proceeds)), key))
                if residual == 0:
                    db.execute('DELETE FROM positions WHERE exchange=? AND mode=? AND symbol=?',
                               (engine.exchange_name, 'paper', symbol))
                else:
                    db.execute('UPDATE positions SET qty=?, cost=? WHERE exchange=? AND mode=? AND symbol=?',
                               (float(residual), float(realized.residual_basis), engine.exchange_name, 'paper', symbol))
                if order_id is not None and db.execute(
                        'SELECT 1 FROM trades WHERE exchange=? AND mode=? AND order_id=? LIMIT 1',
                        (engine.exchange_name, 'paper', order_id)).fetchone():
                    raise ValueError('this venue order is already booked')
                db.execute('INSERT INTO trades(created_at,exchange,mode,symbol,side,price,quantity,gross,fee,realized,reason,order_id) '
                           'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                           (_now_iso(), engine.exchange_name, 'paper', symbol, 'SELL', float(fill['average']),
                            float(fill['filled']), float(fill['gross']),
                            float(money.add(quote_fee or money.ZERO, money.mul(base_fee or money.ZERO, fill['average']))),
                            float(realized.realized_net), f'Micro {trigger}: {reason}'[:200], order_id))
                if intent is not None and not recovery:
                    storage.update_order_intent(intent['intent_id'], 'accounted', order_id=order_id, db=db,
                                                filled_quantity=float(fill['filled']),
                                                filled_price=float(fill['average']))
                elif intent is not None:
                    self._settle_intent_for_recovery(intent, db, accounted=True, order_id=order_id,
                                                     record={'average': float(fill['average']), 'filled': float(fill['filled'])})
                fresh = dict(db.execute('SELECT * FROM micro_cycles WHERE cycle_id=?', (cycle['cycle_id'],)).fetchone())
                status = realized.status if (fresh.get('realized_status') in (None, 'confirmed')) else fresh['realized_status']
                fields = dict(owned_base=money.text(residual), basis_quote=money.text(realized.residual_basis),
                              exit_count=int(fresh['exit_count']) + 1,
                              closed_base=money.text(money.add(_dec(fresh['closed_base'], money.ZERO), realized.closed_base)),
                              exit_gross=money.text(money.add(_dec(fresh['exit_gross'], money.ZERO), fill['gross'])),
                              exit_fees=money.text(money.add(_dec(fresh['exit_fees'], money.ZERO), quote_fee or money.ZERO)),
                              exit_proceeds_net=money.text(money.add(_dec(fresh['exit_proceeds_net'], money.ZERO),
                                                                     realized.net_proceeds)),
                              realized_net=money.text(money.add(_dec(fresh['realized_net'], money.ZERO),
                                                                realized.realized_net)),
                              realized_status=status, last_exit_trigger=trigger, review_reason=None)
                data = {'realized': realized.as_dict(), 'trigger': trigger}
                if rest_sellable:
                    transition(db, fresh, 'HOLDING', f'partial exit ({trigger}); the rest stays managed', data=data, **fields)
                else:
                    transition(db, fresh, 'CLOSED', f'exit filled ({trigger})', data=data, closed_at=_now_iso(),
                               closed_wall=time.time(), residual_base=money.text(residual),
                               residual_basis=money.text(realized.residual_basis), **fields)
        except Exception as exc:
            engine._ledger_failure(f'MICRO SELL {symbol}: {type(exc).__name__}; portfolio ledger requires review',
                                   intent_id=(intent or {}).get('intent_id'), contain=True)
            return False
        self.metrics.observe('micro.fill_to_commit_ms', (time.monotonic() - started) * 1000)
        try:
            storage.audit('trade', f'PAPER MICRO SELL {symbol} ({trigger}): realized net {money.display(realized.realized_net, signed=True)} '
                                   f'USDT ({realized.status}); residual {money.display(residual, 8)}.')
            observability.event('micro.exit_booked', cycle_id=cycle['cycle_id'], symbol=symbol, trigger=trigger,
                                realized_net=money.text(realized.realized_net), status=realized.status,
                                residual=money.text(residual), order_id=order_id)
        except Exception:
            pass
        try:
            cash_after = engine._cash('paper')
            engine.risk.observe_realized('paper', engine._equity('paper', cash_after, engine._active_positions('paper')))
        except Exception:
            pass
        engine._observe_fills('paper')
        if not rest_sellable:
            self._after_close(cycle)
        return True

    def _after_close(self, cycle: dict) -> None:
        try:
            self._check_loss_limits()
        except Exception as exc:
            storage.audit('warning', f'Micro loss-limit check failed ({type(exc).__name__}); it runs again before the '
                                     'next entry, which waits for it.')

    # ------------------------------------------------------------ entries
    def _last_close(self, symbol: str) -> dict | None:
        rows = storage.rows("SELECT closed_wall, realized_net FROM micro_cycles WHERE exchange=? AND mode='paper' "
                            "AND symbol=? AND state='CLOSED' ORDER BY closed_wall DESC LIMIT 10",
                            (self.engine.exchange_name, symbol))
        if not rows:
            return None
        streak = 0
        for row in rows:
            if (_dec(row['realized_net'], money.ZERO) or money.ZERO) < 0:
                streak += 1
            else:
                break
        return {'closed_wall': float(rows[0]['closed_wall'] or 0.0), 'loss_streak': streak}

    def _cooldown_remaining(self, symbol: str, now: float) -> tuple[float, dict | None]:
        last = self._last_close(symbol)
        if last is None:
            return 0.0, None
        base = float(self.engine.cfg['micro_cooldown_seconds'])
        wait = base * (2 ** min(last['loss_streak'], 4))
        return max(0.0, last['closed_wall'] + wait - now), last

    def _committed(self) -> tuple[Decimal, Decimal]:
        """(reserved by unfinished entries, basis of open cycles)."""
        reserved, basis = money.ZERO, money.ZERO
        for cycle in open_cycles(self.engine.exchange_name, 'paper'):
            reserved = money.add(reserved, _dec(cycle['reserved_quote'], money.ZERO))
            if cycle['state'] in OWNING_STATES or cycle['state'] == 'REVIEW':
                basis = money.add(basis, _dec(cycle['basis_quote'], money.ZERO))
        return reserved, basis

    def _entries(self, params, selected: list[str]) -> None:
        engine = self.engine
        now = time.time()
        open_symbols, blockers, latch, capacity = set(), [], None, False

        def read_gates() -> bool:
            """Read before every pair: an earlier pair's failed or unknown entry, or a close (and the latch it
            engages) booked by the exit check between pairs, must stop the later pairs of this pass."""
            nonlocal open_symbols, blockers, latch, capacity
            try:
                # Before every entry: a close whose check never ran (a crash, a recovery, a failure) still counts.
                self._check_loss_limits()
            except Exception as exc:
                for symbol in selected:
                    self._set_waiting(symbol, 'RISK', f'Micro loss limits could not be checked ({type(exc).__name__}); '
                                      'no entry this pass', [type(exc).__name__])
                return False
            opened = open_cycles(engine.exchange_name, 'paper')
            open_symbols = {c['symbol'] for c in opened}
            blockers = [c for c in opened if c['state'] in BLOCKING_STATES or c['state'] in TRANSIENT_STATES]
            blockers += self._closed_late_fills()
            latch = self.latch()
            capacity = len(opened) < int(engine.cfg['micro_max_cycles'])
            return True
        if not read_gates():
            return
        entered = 0
        max_age = float(engine.cfg['micro_book_max_age_seconds'])
        offset = engine._clock_offset()
        for index, symbol in enumerate(selected):
            if not engine._orders_allowed():
                return
            if index:
                # Every pair's book read can wait on the exchange: held cycles are re-checked between pairs.
                self._manage_exits(params, only_due=True)
                if not read_gates():
                    return
            if symbol in open_symbols:
                self._set_waiting(symbol, None, 'Cycle open', [])
                continue
            context = self.context.get(symbol) or {}
            features = context.get('features')
            cooldown, last = self._cooldown_remaining(symbol, now)
            fresh_after_exit = True
            if last is not None and features is not None:
                # The bar closed on the venue's clock; the exit was stamped on this PC's.
                fresh_after_exit = (features.bar_ts_ms / 1000.0 + 60.0 + offset) > last['closed_wall']
            # Features of an older bar than the one that just closed are not fresh data for an entry, even
            # when the bar closed during this pass after the pair's context was read (checked again below).
            features_current = features is None or (bool(context.get('current'))
                                                    and context.get('bar_due') == self._bar_due())
            pre_regime = None
            if features is not None:
                pre_regime, _ = strat.classify_regime(features, spread_bps=0.0, depth_ok=True, data_fresh=True,
                                                      params=params)
            plausible = pre_regime in ('trending_up', 'ranging') and capacity and not blockers and not latch \
                and cooldown <= 0 and fresh_after_exit
            decided_on = (context.get('bar_due'), features_current, pre_regime, capacity, bool(blockers), bool(latch),
                          cooldown > 0, fresh_after_exit)
            if plausible:
                book = self._book(symbol, interval=ENTRY_BOOK_SECONDS, max_age=max_age)
            else:
                # No setup is possible on this bar: the book is read once per new decision (a new bar, the bar's
                # candles becoming current, a changed gate such as a cycle that just closed, a latch or a cooldown
                # that ran out) and otherwise at most every IDLE_BOOK_SECONDS. Features that are not current decide
                # STALE_DATA whatever the book, so they do not force a read. Between reads the pair keeps its last
                # decision: the same book on the same inputs would only repeat it, and could pass its age limit
                # before it is checked. An 'enter' decision is never kept: it was acted on or refused.
                row = self.pairs.get(symbol) or {}
                last_decision = row.get('decision') or {}
                new_decision = (not last_decision or self._decided_bar.get(symbol) != decided_on
                                or last_decision.get('action') == 'enter')
                book = self._book(symbol, interval=IDLE_BOOK_SECONDS, max_age=max_age, idle=True,
                                  force=new_decision and features_current)
                if not new_decision and (book is None
                                         or book.generation == (row.get('book') or {}).get('generation')):
                    continue
            rules = self.rules(symbol)
            cost = self.cost(rules)
            sizing = sizing_failure = None
            if book is not None:
                reserved, basis = self._committed()
                try:
                    cash = self._cash()
                    sizing = econ.size_entry(rules=rules, book=book, cost=cost, budget_cap=self.budget_cap(),
                                             cash_available=max(money.ZERO, money.sub(cash, reserved)),
                                             allocation_remaining=max(money.ZERO, money.sub(self.allocation(),
                                                                                            money.add(reserved, basis))),
                                             target_net=self.target_net(),
                                             exit_stress_bps=money.parse(params.max_loss_bps),
                                             now_wall=now, max_rules_age_s=CATALOGUE_MAX_AGE_S)
                except Exception as exc:
                    sizing_failure = f'sizing failed: {type(exc).__name__}'
                    self._note_error(symbol, sizing_failure)
            fresh_book = book if book is not None and book.age_seconds(time.monotonic()) <= max_age else None
            # Read again after the book: the bar may have closed while the book was read.
            features_current = features is None or (bool(context.get('current'))
                                                    and context.get('bar_due') == self._bar_due())
            regime, regime_reasons = strat.classify_regime(
                features, spread_bps=float(fresh_book.spread_bps) if fresh_book else 1e9,
                depth_ok=bool(fresh_book and fresh_book.depth_known),
                data_fresh=fresh_book is not None and features_current, params=params)
            book_summary = {}
            if book is not None:
                book_summary = {'best_bid': money.text(book.best_bid), 'best_ask': money.text(book.best_ask),
                                'spread_bps': float(book.spread_bps), 'generation': book.generation,
                                'sequence': book.venue_sequence, 'age_s': round(book.age_seconds(time.monotonic()), 3),
                                'source': book.source}
            conflicts = self._symbol_conflicts(symbol, now)
            gates = {'selected': True, 'capacity': capacity, 'cooldown_remaining_s': round(cooldown, 1),
                     'fresh_after_exit': fresh_after_exit, 'review_block': bool(blockers) or bool(conflicts),
                     'risk_block': engine.risk_block if engine.risk.kill_switch()['engaged'] else None,
                     'latched': latch.get('reason') if latch else None}
            # A stand-in names its pair and its own cause: no book, or a sizing that raised on one (an
            # unmapped code, so the strategy reports it as ERROR with this detail).
            if sizing is not None:
                sizing_dict = sizing.as_dict()
            elif sizing_failure is not None:
                sizing_dict = {'symbol': symbol, 'eligible': False, 'code': 'SIZING_FAILED', 'detail': sizing_failure}
            else:
                sizing_dict = {'symbol': symbol, 'eligible': False, 'code': 'DEPTH_UNKNOWN',
                               'detail': 'No fresh order book'}
            started = time.monotonic()
            verdict = strat.evaluate_entry(symbol=symbol, features=features, regime=regime,
                                           regime_reasons=regime_reasons, sizing=sizing_dict, book_summary=book_summary,
                                           params=params, gates=gates, now=now - self.engine._clock_offset(),
                                           feature_reason=context.get('reason') if features is None else None)
            self.metrics.observe('micro.entry_decision_ms', (time.monotonic() - started) * 1000)
            if book is not None and verdict.action == 'enter':
                self.metrics.observe('micro.book_to_decision_ms',
                                     max(0.0, (time.monotonic() - book.received_mono) * 1000))
            self._record_decision(symbol, verdict, sizing_dict, book_summary, regime, context)
            self._decided_bar[symbol] = decided_on
            if conflicts:
                # The venue shows an order or a fill the ledger does not: never add exposure on top of it.
                self._set_waiting(symbol, 'REVIEW', 'Venue record conflict: ' + conflicts[0]['explanation'],
                                  [item['explanation'] for item in conflicts])
            if verdict.action != 'enter' or entered >= ENTRIES_PER_TICK:
                continue
            if self._symbol_conflicts(symbol, now, force=True):
                continue
            if self._enter(symbol, verdict, sizing, book, rules, cost, features, params):
                entered += 1

    def _record_decision(self, symbol, verdict, sizing_dict, book_summary, regime, context) -> None:
        with self.lock:
            row = self.pairs.setdefault(symbol, {'symbol': symbol})
            row.update({'decision': verdict.as_dict(), 'sizing': sizing_dict, 'book': book_summary, 'regime': regime,
                        'features': context.get('features').as_dict() if context.get('features') is not None else None,
                        'context_reason': context.get('reason'), 'updated_at': _now_iso()})
            if verdict.code != 'ERROR':
                row.pop('error', None)
        self._set_waiting(symbol, verdict.code, verdict.status, list(verdict.reasons))
        self.decisions.append(verdict.as_dict())
        if self.last_codes.get(symbol) != (verdict.action, verdict.code):
            self.last_codes[symbol] = (verdict.action, verdict.code)
            observability.event('micro.decision', decision_id=verdict.decision_id, symbol=symbol, action=verdict.action,
                                code=verdict.code, status=verdict.status, reasons=list(verdict.reasons),
                                candidate=verdict.candidate, regime=regime)
        self.metrics.incr(f'micro.decision.{verdict.action}')

    def _set_waiting(self, symbol: str, code: str | None, status: str, reasons: list) -> None:
        with self.lock:
            row = self.pairs.setdefault(symbol, {'symbol': symbol})
            previous = row.get('waiting', {}).get('code')
            if code and code != previous:
                self.waiting_since[symbol] = time.time()
            if code:
                self.counters[code] += 1
            row['waiting'] = {'code': code, 'status': status, 'reasons': reasons,
                              'since': self.waiting_since.get(symbol) if code else None}

    def _enter(self, symbol, verdict, sizing, book, rules, cost, features, params) -> bool:
        engine = self.engine
        proposal = verdict.proposal or {}
        if sizing is None or not sizing.eligible or sizing.max_debit is None:
            return False
        cycle_id = 'cyc_' + uuid.uuid4().hex[:24]
        stop_bps = proposal.get('stop_net_bps')      # includes the entry cost; never recomputed without it
        if stop_bps is None:
            return False
        stop_net = money.mul(self.budget_cap(), money.bps(money.parse(float(stop_bps))))
        target_net = self.target_net()
        sizing_record = sizing.as_dict()
        owned_guess = sizing.sellable_base or sizing.expected_base
        if owned_guess and owned_guess > 0:
            # Approximate trigger prices for the legacy position row and the dashboard only.
            sizing_record['stop_bid'] = money.text(money.div(money.sub(sizing.max_debit, stop_net), owned_guess))
        started = time.monotonic()
        try:
            with storage.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                cash = money.parse(float(db.execute('SELECT value FROM meta WHERE key=?',
                                                    (engine._key('paper_cash'),)).fetchone()['value']), field='paper cash')
                rows = db.execute("SELECT symbol, state, reserved_quote, basis_quote FROM micro_cycles WHERE exchange=? "
                                  "AND mode='paper' AND state NOT IN ('CLOSED','ABANDONED')",
                                  (engine.exchange_name,)).fetchall()
                reserved = money.add(*[_dec(r['reserved_quote'], money.ZERO) for r in rows])
                basis = money.add(*[_dec(r['basis_quote'], money.ZERO) for r in rows])
                if len(rows) >= int(engine.cfg['micro_max_cycles']):
                    raise CycleError('micro cycle capacity reached')
                if money.sub(cash, reserved) < sizing.max_debit:
                    raise CycleError('paper cash is already reserved by other entries')
                if money.add(reserved, basis, sizing.max_debit) > self.allocation():
                    raise CycleError('the micro allocation is fully committed')
                # The reservation is the last gate before any order: whatever happened earlier in this pass, an
                # unresolved outcome, an unbooked late fill or the micro stop refuses it here.
                unresolved = next((r for r in rows if r['state'] in BLOCKING_STATES or r['state'] in TRANSIENT_STATES),
                                  None)
                if unresolved is not None:
                    raise _refusal(f'the {unresolved["symbol"]} cycle is unresolved ({unresolved["state"]}); new '
                                   'entries wait for its recovery', 'REVIEW')
                late = db.execute("SELECT symbol FROM micro_cycles WHERE exchange=? AND mode='paper' AND state IN "
                                  "('CLOSED','ABANDONED') AND late_fill_reason IS NOT NULL LIMIT 1",
                                  (engine.exchange_name,)).fetchone()
                if late is not None:
                    raise _refusal(f'a late {late["symbol"]} fill after its cycle closed is not booked; new entries '
                                   'wait for its recovery', 'REVIEW')
                stop = db.execute('SELECT value FROM meta WHERE key=?', (self._key('stop'),)).fetchone()
                latched = self._latch_record(stop['value'] if stop is not None else None)
                if latched:
                    raise _refusal(f'the micro stop is latched ({latched.get("reason")})', 'RISK')
                position = db.execute('SELECT qty, cost FROM positions WHERE exchange=? AND mode=? AND symbol=?',
                                      (engine.exchange_name, 'paper', symbol)).fetchone()
                carried_base = carried_basis = money.ZERO
                if position is not None:
                    market = engine._catalogue().get(symbol)
                    if not engine._is_dust(dict(position), market, float(book.best_bid)):
                        raise CycleError(f'{symbol} is already held outside the micro cycles')
                    carried_base = money.parse(float(position['qty']), field='carried quantity')
                    carried_basis = money.parse(float(position['cost']), field='carried cost')
                stamp = _now_iso()
                db.execute('INSERT INTO micro_cycles(cycle_id, exchange, mode, symbol, state, created_at, updated_at, '
                           'strategy, decision_id, opportunity_key, budget_cap, reserved_quote, sizing, carried_base, '
                           'carried_basis, target_net, stop_net, max_hold_s, last_reason) '
                           'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (cycle_id, engine.exchange_name, 'paper', symbol, 'ENTRY_RESERVED', stamp, stamp,
                            verdict.candidate, verdict.decision_id, proposal['opportunity_key'],
                            money.text(self.budget_cap()), money.text(sizing.max_debit),
                            json.dumps(sizing_record, default=str), money.text(carried_base), money.text(carried_basis),
                            money.text(target_net), money.text(stop_net), float(params.max_hold_s), verdict.status[:300]))
                db.execute('INSERT INTO micro_cycle_events(cycle_id, at, from_state, to_state, reason, data) VALUES (?,?,?,?,?,?)',
                           (cycle_id, stamp, None, 'ENTRY_RESERVED', 'cash reserved for a new cycle',
                            json.dumps({'reserved': money.text(sizing.max_debit), 'decision_id': verdict.decision_id,
                                        'opportunity_key': proposal['opportunity_key']})))
        except CycleError as exc:
            code = getattr(exc, 'code', None) or ('CAPACITY' if 'capacity' in str(exc) or 'allocation' in str(exc)
                                                  else 'RISK')
            self._set_waiting(symbol, code, str(exc), [str(exc)])
            return False
        except Exception as exc:
            # A duplicate opportunity key lands here too: that opportunity was already used.
            self._set_waiting(symbol, 'RISK', f'Entry not reserved: {type(exc).__name__}', [type(exc).__name__])
            return False
        self.metrics.observe('micro.reservation_ms', (time.monotonic() - started) * 1000)
        cycle = load_cycle(cycle_id)
        positions = engine._active_positions('paper')
        authorization = engine._authorize(symbol, 'BUY', 'micro_entry', float(sizing.max_debit), float(book.best_ask),
                                          positions=positions, cash=engine._cash('paper'),
                                          stop_fraction=min(0.5, float(stop_bps) / 10000.0),
                                          spread=float(book.spread_bps) / 100.0, reason=verdict.status,
                                          decision_id=cycle_id, sizing=sizing.as_dict())
        if not authorization.allowed:
            with storage.connection() as db:
                transition(db, cycle, 'ABANDONED', 'risk gate refused: ' + authorization.reason, reserved_quote='0')
            self._set_waiting(symbol, 'RISK', 'Risk gate: ' + authorization.reason, list(authorization.failed))
            return False
        with storage.connection() as db:
            cycle = transition(db, cycle, 'ENTRY_SUBMITTED', 'order intent being sent')
        intent, order = engine._submit(self.venue, symbol, 'BUY', cost=float(sizing.gross_quote),
                                       authorization=authorization, intended_price=float(book.best_ask),
                                       decision_id=cycle_id,
                                       venue_params={'book': book, 'adverse_bps': cost.latency_bps,
                                                     'taker_fee': cost.taker_fee})
        if order is None:
            self._unsent_or_unknown(cycle, 'BUY')
            return False
        intent_row = storage.order_intent(intent)
        return self._book_entry(cycle, intent_row, self._final_answer(symbol, intent_row, order))

    # ------------------------------------------------------- operator exit
    def flatten(self) -> dict:
        """Exit every open micro holding now (a risk-reducing operator action).

        While the bot runs, the worker sells within FLATTEN_WINDOW_S; while it
        is stopping, this waits for the worker to finish and then sells here;
        while stopped, it sells here at once. The answer says which happened.
        """
        engine = self.engine
        with engine.lock:
            thread = engine.thread
            alive = engine.running and thread is not None and thread.is_alive()
            stopping = alive and (engine.stopping or engine.stop_event.is_set())
        if stopping:
            thread.join(timeout=FLATTEN_STOP_WAIT_S)
            if thread.is_alive():
                raise storage.ConfigValidationError('The bot is still stopping. Press Exit all again once it shows '
                                                    'Stopped.')
        elif alive:
            self.request_flatten()
            return {'queued': True, 'window_s': FLATTEN_WINDOW_S}
        with engine.lock:
            if engine._busy():
                raise storage.ConfigValidationError('Wait for the active operation to finish.')
            if engine.mode != 'paper':
                raise storage.ConfigValidationError(micro_policy.PAPER_ONLY)
            if not engine.scan_lock.acquire(blocking=False):
                raise storage.ConfigValidationError('Wait for the active market operation to finish.')
            engine.control_busy = engine._manual_order_active = True
            engine.stop_event.clear()
        results = []
        try:
            self.settle_stranded()
            self._refresh_catalogue(holding=any(c['state'] == 'HOLDING'
                                                for c in open_cycles(engine.exchange_name, 'paper')))
            params = self.params()
            for cycle in open_cycles(engine.exchange_name, 'paper'):
                if cycle['state'] == 'HOLDING':
                    self._manage_exit(cycle, params, operator=True, ignore_backoff=True)
                    results.append(cycle['symbol'])
        finally:
            try:
                # No pass follows while stopped: a sale whose booking failed, or that a failure interrupted after
                # its fill, is explained now (EXIT_UNKNOWN, with its recovery), before anything else can start.
                self.settle_stranded()
            except Exception as exc:
                storage.audit('error', f'Micro check after Exit all failed ({type(exc).__name__}); restart the app to '
                                       'explain unfinished cycles.')
            with engine.lock:
                engine._manual_order_active = engine.control_busy = False
                engine.scan_lock.release()
        return {'queued': False, 'attempted': results}

    # ------------------------------------------------------------ status
    def results(self) -> dict:
        rows = storage.rows("SELECT * FROM micro_cycles WHERE exchange=? AND mode='paper' AND state='CLOSED' "
                            'ORDER BY closed_wall DESC LIMIT 500', (self.engine.exchange_name,))
        values = [(_dec(r['realized_net'], money.ZERO) or money.ZERO) for r in reversed(rows)]
        wins = [v for v in values if v > 0]
        losses = [v for v in values if v < 0]
        target = self.target_net()
        equity, peak, drawdown = money.ZERO, money.ZERO, money.ZERO
        for value in values:
            equity = money.add(equity, value)
            peak = max(peak, equity)
            drawdown = max(drawdown, money.sub(peak, equity))
        fees = money.add(*[money.add(_dec(r['entry_fee_quote'], money.ZERO) or money.ZERO,
                                     _dec(r['exit_fees'], money.ZERO) or money.ZERO) for r in rows])
        turnover = money.add(*[money.add(_dec(r['entry_gross'], money.ZERO) or money.ZERO,
                                         _dec(r['exit_gross'], money.ZERO) or money.ZERO) for r in rows])
        # Gains before fees (net result plus that cycle's fees): the share fees take of what the price moves
        # earned. For a cycle that left a residual, its whole entry fee is counted (a slight overstatement).
        before_fees = [money.add(_dec(r['realized_net'], money.ZERO) or money.ZERO,
                                 _dec(r['entry_fee_quote'], money.ZERO) or money.ZERO,
                                 _dec(r['exit_fees'], money.ZERO) or money.ZERO) for r in rows]
        positive = [v for v in before_fees if v > 0]
        gross_gains = money.add(*positive) if positive else money.ZERO
        holds = [max(0.0, float(r['closed_wall'] or 0) - float(r['opened_wall'] or 0)) for r in rows if r['opened_wall']]
        triggers = Counter(r['last_exit_trigger'] or 'unknown' for r in rows)
        near_even = sum(1 for v in values if abs(v) <= money.mul(target, Decimal('0.25')))
        residuals = storage.rows("SELECT symbol, residual_base, residual_basis FROM micro_cycles WHERE exchange=? AND mode='paper' "
                                 "AND state='CLOSED' AND residual_base NOT IN ('0', '') ORDER BY closed_wall DESC LIMIT 20",
                                 (self.engine.exchange_name,))
        unconfirmed = [v for v, r in zip(reversed(values), rows) if r['realized_status'] != 'confirmed']
        bins = Counter()
        cap = self.budget_cap()
        for value in values:
            ratio = float(money.div(value, cap)) * 10000 if cap else 0.0
            edge = int(math.floor(ratio / 10.0)) * 10
            bins[f'{edge}..{edge + 10} bps'] += 1
        return {
            'closed_cycles': len(values), 'wins': len(wins), 'losses': len(losses), 'flat': len(values) - len(wins) - len(losses),
            'realized_net_total': money.text(money.add(*values)) if values else '0',
            'realized_unconfirmed_count': len(unconfirmed),
            'average_net': money.text(money.div(money.add(*values), Decimal(len(values)))) if values else None,
            'worst_loss': money.text(min(values)) if values else None,
            'best_gain': money.text(max(values)) if values else None,
            'max_drawdown': money.text(drawdown), 'fees_paid': money.text(fees), 'turnover': money.text(turnover),
            'fees_share_of_gross_gains': (round(float(money.div(fees, gross_gains)), 4) if gross_gains > 0 else None),
            'average_hold_s': round(sum(holds) / len(holds), 1) if holds else None,
            'near_break_even_exits': near_even, 'exit_triggers': dict(triggers), 'distribution_bps_of_cap': dict(bins),
            'residuals': [{'symbol': r['symbol'], 'base': r['residual_base'], 'basis': r['residual_basis']} for r in residuals],
            'note': 'PAPER simulated fills. Win rate and cycle count are not success measures; see net result, '
                    'drawdown and fees.',
        }

    def status(self) -> dict:
        engine = self.engine
        try:
            cap = self.budget_cap()
            reserved, basis = self._committed()
            opened = open_cycles(engine.exchange_name, 'paper')
        except Exception as exc:
            return {'active': self.active(), 'error': f'micro status unavailable: {type(exc).__name__}'}
        cycles = []
        for cycle in opened:
            row = self.pairs.get(cycle['symbol'], {})
            exit_view = row.get('exit') or {}
            if exit_view.get('cycle_id') != cycle['cycle_id']:
                exit_view = {}                      # an earlier cycle's last estimate is not this cycle's
            cycles.append({key: cycle[key] for key in (
                'cycle_id', 'symbol', 'state', 'strategy', 'opened_at', 'owned_base', 'basis_quote', 'entry_debit',
                'entry_price', 'entry_fee_status', 'entry_partial', 'carried_base', 'target_net', 'stop_net',
                'max_hold_s', 'peak_est_net', 'realized_net', 'exit_count', 'last_reason', 'review_reason',
                'reserved_quote')} | {
                'age_s': round(time.time() - float(cycle['opened_wall']), 1) if cycle['opened_wall'] else None,
                'pending_s': None if cycle['opened_wall'] else _seconds_since(cycle.get('created_at')),
                'estimate': exit_view.get('estimate'), 'exit_verdict': exit_view.get('verdict'),
                'depth_model': exit_view.get('depth_model'),
                'estimate_age_s': round(time.time() - exit_view['at_wall'], 1) if exit_view.get('at_wall') else None})
        books = self.books.status(time.monotonic())
        with self.lock:
            pairs = []
            for symbol in self.selected() + sorted(set(self.pairs) - set(self.selected())):
                row = dict(self.pairs.get(symbol, {'symbol': symbol}))
                row['selected'] = symbol in self.selected()
                # The book as it is now, not as it was at the pair's last entry decision (a held pair is not
                # re-decided, and nothing is decided while stopped).
                live = books.get(symbol) or {}
                row['book'] = ({key: live.get(key) for key in ('best_bid', 'best_ask', 'spread_bps', 'generation',
                                                                'sequence', 'age_s', 'source', 'state')}
                               if live.get('best_bid') is not None else {'state': live.get('state', 'missing')})
                waiting = row.get('waiting') or {}
                if waiting.get('since'):
                    waiting = {**waiting, 'waiting_s': round(time.time() - waiting['since'], 1)}
                    row['waiting'] = waiting
                pairs.append(row)
            counters = dict(self.counters)
        latch = self.latch()
        return {
            'active': self.active(), 'paper_only': True, 'paper_only_reason': micro_policy.PAPER_ONLY,
            'running': engine.running and self.active(),
            'budget': {'cap': money.text(cap), 'absolute_cap': money.text(econ.MICRO_MAX_ALL_IN_USDT),
                       'configured': str(engine.cfg['micro_budget_usdt']), 'allocation': money.text(self.allocation()),
                       'reserved': money.text(reserved), 'committed_basis': money.text(basis),
                       'max_cycles': int(engine.cfg['micro_max_cycles']),
                       'meaning': 'All-in quote debit per entry including the fee reserve; never raised automatically.'},
            'params': {key: engine.cfg[key] for key in storage.MICRO_FIELDS} | {
                'uncertainty_floor_bps': UNCERTAINTY_FLOOR_BPS, 'max_spread_bps': self.params().max_spread_bps,
                'max_vol_bps': MAX_VOL_BPS, 'edge_ratio': EDGE_RATIO},
            'latch': latch, 'review_block': self.review_block(), 'recovery': list(self.recovery),
            'exit_all_pending_s': round(self.flatten_pending(), 1),
            'pairs': pairs, 'cycles': cycles, 'results': self.results(), 'counters': counters,
            'books': books, 'last_tick': self.last_tick, 'last_error': self.last_error,
            'heuristic_note': 'Setup checks are transparent heuristics, not calibrated probabilities of profit.',
        }

    def recent_decisions(self, limit: int = 50) -> list[dict]:
        with self.lock:
            items = list(self.decisions)[-max(1, min(limit, 300)):]
        return list(reversed(items))

    def reset_memory(self) -> None:
        with self.lock:
            self.pairs.clear()
            self._decided_bar.clear()
            self._next_book.clear()
            self._next_mark.clear()
            self.counters.clear()
            self.waiting_since.clear()
            self.context.clear()
            self.decisions.clear()
            self.recovery = []
