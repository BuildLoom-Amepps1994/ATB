"""Local SPOT-only service. Paper by default; uncertain execution fails closed."""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import ccxt

import credentials
import decision
import feed
import observability
import orders
import paper_exchange
import risk
import storage
from market_data import MarketData
from strategy import entry_price_ok, entry_signal, regime_signal, research_report, setup_quality, stop_fraction


MARKET_SYMBOL = 'BTC/USDT'

KNOWN_MEMES = {'DOGE', 'SHIB', 'PEPE', 'BONK', 'FLOKI', 'WIF', 'BRETT', 'POPCAT',
               'MOG', 'TURBO', 'FARTCOIN', 'PNUT', 'BOME', 'PENGU', 'NEIRO', 'BABYDOGE'}

HORIZONS = {'rapid': ('1m', '5m'), 'balanced': ('1h', '4h'),
            'swing': ('4h', '1d'), 'position': ('1d', '1w')}
TIMEFRAME_SECONDS = {'1m': 60, '5m': 300, '1h': 3600, '4h': 14400,
                     '1d': 86400, '1w': 604800}


def is_meme(symbol: str) -> bool:
    return symbol.split('/')[0] in KNOWN_MEMES


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def finite_positive(number: Any) -> float:
    try:
        value = float(number)
        return value if not isinstance(number, bool) and math.isfinite(value) and value > 0 else 0.0
    except (ValueError, TypeError, OverflowError):
        return 0.0


class TradingEngine:
    def __init__(self):
        storage.init_db()
        self.lock = threading.RLock()
        self.private_lock = threading.RLock()
        self.scan_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.cfg = storage.settings()
        self.exchange_name = self.cfg['exchange']
        self.public = self._new_exchange(self.exchange_name)
        self.data = MarketData(self.public, wait=self._retry_wait,
                               concurrency=self._public_concurrency(self.public))
        self.markets_loaded_at = 0.0
        self._halt_pending = None
        self.private = self.api_key = self.api_secret = None
        self.credential_source = 'none'
        self.mode = 'paper'
        self.running = self.stopping = False
        self.control_busy = False
        self._manual_order_active = False
        self.manual_order_context = str(uuid.uuid4())
        self.stop_generation = 0
        self.thread = None
        self.last_scan = self.last_successful_scan = self.last_error = None
        self.last_monitor = self.last_successful_monitor = self.monitor_error = None
        self._next_monitor = 0.0
        self.market_rows: list[dict] = []
        self.last_prices: dict[str, float] = {}
        self.last_asks: dict[str, float] = {}
        self.last_price_times: dict[str, float] = {}
        self.price_times = self.last_price_times
        # What each stored quote's time is measured from ('exchange' or
        # 'receipt') and when it arrived here, so ordering compares like with like.
        self.last_quote_basis: dict[str, str] = {}
        self.last_received_at: dict[str, float] = {}
        # Last (action, status, reasons) written to the event log per symbol.
        self._entry_trail: dict[tuple, tuple] = {}
        self.latest_balance = self.balance_updated_at = None
        self.last_universe_update = 0.0
        self.discovered: list[str] = []
        self.risk_block = None
        self.live_halted = self._durable_live_block()
        self.risk = risk.RiskGate(self)
        self.metrics = observability.METRICS
        self._last_authorization: risk.Authorization | None = None
        self.paper = self._new_paper_exchange()
        self.unresolved_probe: list[dict] = []
        self.decisions = decision.DecisionLog()
        self.feed: feed.QuoteFeed | None = None
        # A simulated order has no exchange-side effect; a crash mid-paper-order
        # leaves nothing to reconcile, so its intent is closed at startup.
        storage.abandon_stale_paper_intents(self.exchange_name)
        self._restore_from_keyring()
        observability.event('engine.start', mode=self.mode, exchange=self.exchange_name,
                            live_halted=self.live_halted, kill_switch=self.risk.kill_switch()['engaged'])

    @staticmethod
    def _new_exchange(name: str, key: str = '', secret: str = ''):
        if name not in ('mexc', 'kraken'):
            raise ValueError('Supported exchanges: MEXC, Kraken')
        options = {'enableRateLimit': True, 'timeout': 18000, 'maxRetriesOnFailure': 0,
                   'options': {'defaultType': 'spot', 'maxRetriesOnFailure': 0}}
        if name == 'mexc':
            options['options'].update({'adjustForTimeDifference': True, 'recvWindow': 5000})
        if key and secret:
            options.update({'apiKey': key, 'secret': secret})
        return getattr(ccxt, name)(options)

    @staticmethod
    def _public_concurrency(client) -> int:
        """Overlap candle downloads only where the rate limit, not latency, is slack.

        MEXC paces public requests 50 ms apart, so several round trips can
        overlap. Kraken's one-per-second pacing is the bottleneck by itself.
        """
        rate_limit = getattr(client, 'rateLimit', None)
        if isinstance(rate_limit, bool) or not isinstance(rate_limit, (int, float)) or not 0 < rate_limit <= 100:
            return 1
        return 4

    def _new_paper_exchange(self):
        """Simulated counterparty sharing the live submit/confirm/ledger path."""
        return paper_exchange.PaperExchange(self.exchange_name, catalogue=self._catalogue,
                                            settings=lambda: self.cfg, balances=self._paper_balances)

    def _paper_balances(self) -> dict:
        free = {'USDT': self._cash('paper')}
        for position in self._active_positions('paper'):
            base = position['symbol'].split('/')[0]
            free[base] = free.get(base, 0.0) + float(position['qty'])
        return free

    def _may_execute(self) -> bool:
        """Live orders need a running worker or an active manual ticket.

        A simulated order from an idle engine has no counterparty, but a Stop
        requested during activity ends paper orders too.
        """
        if self.mode == 'live':
            return self._orders_allowed()
        return not (self.stop_event.is_set() and (self.running or self._manual_order_active))

    def _key(self, name: str) -> str:
        return storage.scope_key(name, self.exchange_name)

    def _retry_wait(self, seconds: float) -> bool:
        """Back-off between public read retries.

        A Stop interrupts the wait only while an order could follow the read.
        Idle dashboard reads (charts, backtests, manual scans) keep their
        retries even though the event stays set after the last Stop.
        """
        if self.running or self._manual_order_active:
            return self.stop_event.wait(seconds)
        time.sleep(seconds)
        return False

    def _catalogue(self) -> dict:
        """Loaded public market definitions. CCXT keeps None until load_markets."""
        markets = getattr(self.public, 'markets', None)
        return markets if isinstance(markets, dict) else {}

    @staticmethod
    def _order_id(order) -> str | None:
        identifier = order.get('id') if isinstance(order, dict) else None
        return str(identifier) if identifier not in (None, '') else None

    def _durable_live_block(self) -> bool:
        pending = self._halt_pending
        if pending:
            # A halt whose flag could not be written stays in force and is
            # re-persisted at the first opportunity.
            try:
                storage.set_meta(storage.scope_key('live_halt', pending), '1')
                self._halt_pending = None
            except Exception:
                pass
            if pending == self.exchange_name:
                return True
        return bool(storage.unresolved_orders() or storage.legacy_live_records()
                    or storage.get_meta(self._key('live_halt')) == '1')

    def _live_block_detail(self) -> str:
        """Name what blocks live trading, per exchange, so the recovery step is unambiguous."""
        counts: dict[str, int] = {}
        for row in storage.unresolved_orders():
            counts[row['exchange']] = counts.get(row['exchange'], 0) + 1
        parts = [f'{name}: {count} unresolved live order intent{"s" if count != 1 else ""}' for name, count in sorted(counts.items())]
        if storage.legacy_live_records():
            parts.append('legacy live records need a reviewed ledger repair')
        if storage.get_meta(self._key('live_halt')) == '1':
            parts.append(f'{self.exchange_name}: durable halt flag')
        return '; '.join(parts) or 'see the recovery report'

    def _busy(self) -> bool:
        return bool(self.control_busy or self.running or (self.thread and self.thread.is_alive()) or self.scan_lock.locked())

    def _require_idle(self):
        if self._busy():
            raise storage.ConfigValidationError('Wait for the bot and any active scan to finish before changing this setting.')

    def _restore_from_keyring(self):
        pair = credentials.load(self.exchange_name)
        if pair:
            self.api_key, self.api_secret = pair
            self.credential_source = 'Operating-system credential manager'
            # Loading saved secrets never authenticates or enables live mode.

    def _account_guard(self, key: str, *, bind: bool = False):
        fingerprint = hashlib.sha256((self.exchange_name + '\0' + key).encode('utf8')).hexdigest()
        meta = self._key('live_account_fingerprint')
        previous = storage.get_meta(meta)
        tracked = self._active_positions('live') or storage.unresolved_orders(self.exchange_name)
        today = datetime.now(timezone.utc).date().isoformat()
        daily_state = (storage.get_meta(self._key(f'baseline.live.{today}'))
                       or storage.get_meta(self._key(f'breached.live.{today}')))
        if (tracked or daily_state) and previous != fingerprint:
            raise storage.ConfigValidationError('Live positions, orders, or current-day risk state belong to another or unverified API identity; reconcile before replacing credentials.')
        if bind and previous != fingerprint:
            storage.set_meta(meta, fingerprint)

    def _read_balance(self, client=None) -> dict:
        # MEXC selects its spot wallet by type; Kraken forwards unknown fields
        # to its balance endpoint, so it receives no extra parameters.
        params = {'type': 'spot'} if self.exchange_name == 'mexc' else {}
        with self.private_lock:
            balance = (client or self._ensure_private()).fetch_balance(params)
        if not isinstance(balance, dict) or not isinstance(balance.get('free'), dict):
            raise ValueError('Exchange returned an invalid spot balance')
        free = balance['free'].get('USDT', 0)
        if isinstance(free, bool) or not isinstance(free, (int, float)) or not math.isfinite(free) or free < 0:
            raise ValueError('Exchange returned an invalid free USDT balance')
        with self.lock:
            self.latest_balance, self.balance_updated_at = balance, time.time()
        return balance

    def connect(self, key: str, secret: str, remember: bool = False):
        if not isinstance(key, str) or not key.strip() or not isinstance(secret, str) or not secret.strip():
            raise storage.ConfigValidationError('API key and secret are both required.')
        with self.lock:
            self._require_idle()
            if self.mode != 'paper':
                raise storage.ConfigValidationError('Return to paper mode before changing credentials.')
            self._account_guard(key)
            self.control_busy = True
        try:
            storage.register_secrets(key, secret)
            client = self._new_exchange(self.exchange_name, key, secret)
            with self.private_lock:
                client.load_markets()
                self._read_balance(client)
            self._account_guard(key, bind=True)
            with self.lock:
                self.private, self.api_key, self.api_secret = client, key, secret
                self.credential_source = 'session'
                self.manual_order_context = str(uuid.uuid4())
            if remember:
                try:
                    credentials.save(self.exchange_name, key, secret)
                    with self.lock:
                        self.credential_source = 'Operating-system credential manager'
                except credentials.CredentialStoreUnavailable:
                    storage.audit('warning', 'Secure credential storage unavailable; credentials remain in process memory.')
            else:
                try:
                    saved = credentials.load(self.exchange_name)
                except Exception:
                    saved = None
                if saved and saved[0] != key:
                    storage.audit('warning', 'Different saved credentials remain in the operating-system credential manager and reload after a restart. Disconnect to remove them.')
            storage.audit('info', f'{self.exchange_name.upper()} spot account connected. Live trading still requires explicit opt-in.')
            # Read-only: tell the operator what the exchange knows about any
            # unresolved intent. Clearing a halt remains a confirmed manual step.
            try:
                self.probe_unresolved(client)
            except Exception as exc:
                storage.audit('warning', f'Unresolved-order probe unavailable: {type(exc).__name__}.')
        finally:
            with self.lock:
                self.control_busy = False

    def probe_unresolved(self, client=None) -> list[dict]:
        """Ask the exchange, read-only, about each unresolved live intent.

        Findings are audited and returned for the dashboard. The journal is
        never changed here: reconciliation stays with the confirmed offline
        procedure in recovery.py, so no outcome is ever guessed.
        """
        findings = []
        pending = storage.unresolved_orders(self.exchange_name)
        if not pending:
            with self.lock:
                self.unresolved_probe = []
            return findings
        client = client or self._ensure_private()
        for intent in pending:
            finding = {'intent_id': intent['intent_id'], 'symbol': intent['symbol'], 'side': intent['side'],
                       'journal_status': intent['status'], 'exchange_status': None, 'filled': None, 'order_id': None}
            try:
                client_id = intent.get('client_order_id')
                with self.private_lock:
                    if intent.get('order_id'):
                        found = client.fetch_order(intent['order_id'], intent['symbol'])
                    elif client_id:
                        found = orders.locate(client, intent['symbol'], client_id, since_ms=0)
                    else:
                        found = None
                        finding['exchange_status'] = 'no client order id recorded (pre-upgrade intent)'
                if isinstance(found, dict):
                    summary = orders.fill_summary(found)
                    finding.update({'exchange_status': found.get('status') or 'unknown', 'filled': summary['filled'],
                                    'order_id': self._order_id(found)})
                elif finding['exchange_status'] is None:
                    finding['exchange_status'] = 'not found in open or recent orders'
            except Exception as exc:
                finding['exchange_status'] = f'lookup failed: {type(exc).__name__}'
            findings.append(finding)
            storage.audit('warning', f"Unresolved {intent['side']} {intent['symbol']} intent {intent['intent_id'][:8]}: "
                                     f"exchange reports {finding['exchange_status']}"
                                     + (f", filled {finding['filled']}" if finding['filled'] else '')
                                     + '. Confirm against the exchange order history, then clear the halt with recovery.py.')
        observability.event('execution.probe', findings=findings)
        with self.lock:
            self.unresolved_probe = findings
        return findings

    def disconnect(self):
        with self.lock:
            self._require_idle()
            if self.mode != 'paper':
                raise storage.ConfigValidationError('Switch to paper mode before disconnecting.')
            self.private = self.api_key = self.api_secret = None
            self.manual_order_context = str(uuid.uuid4())
            self.latest_balance = self.balance_updated_at = None
            self.credential_source = 'none'
            credentials.delete(self.exchange_name)
            storage.audit('info', 'Session credentials cleared and saved credential removal verified.')
            observability.event('credentials.disconnected', exchange=self.exchange_name)

    def _ensure_private(self):
        with self.private_lock:
            if self.api_key:
                self._account_guard(self.api_key)
            if self.private is None:
                if not self.api_key or not self.api_secret:
                    raise storage.ConfigValidationError('Connect exchange API credentials before enabling live trading.')
                self._account_guard(self.api_key)
                client = self._new_exchange(self.exchange_name, self.api_key, self.api_secret)
                client.load_markets()
                self._read_balance(client)
                self._account_guard(self.api_key, bind=True)
                with self.lock:
                    self.private = client
            return self.private

    def set_mode(self, mode: str, confirmation: str = '', eligible: bool = False):
        with self.lock:
            self._require_idle()
            if mode == 'paper':
                self.mode = mode
                self.manual_order_context = str(uuid.uuid4())
                self.risk_block = None
                storage.audit('info', 'Trading mode: PAPER (simulation).')
                observability.event('mode.changed', mode='paper', exchange=self.exchange_name)
            elif mode == 'live':
                if storage.settings_health()['review_required']:
                    raise storage.ConfigValidationError('Review and explicitly save the recovered settings before enabling live trading.')
                if confirmation != 'ENABLE LIVE TRADING' or eligible is not True:
                    raise storage.ConfigValidationError('Live trading requires the exact confirmation and exchange eligibility acknowledgement.')
                if self.live_halted or self._durable_live_block():
                    self.live_halted = True
                    raise storage.ConfigValidationError('Live trading is blocked by unresolved orders or legacy account records '
                                                        f'({self._live_block_detail()}); reconcile them manually first.')
                self.control_busy = True
            else:
                raise storage.ConfigValidationError('Invalid trading mode.')
        if mode == 'live':
            try:
                # The identity binds only after the account read succeeds.
                self._ensure_private()
                with self.lock:
                    self.mode = mode
                    self.manual_order_context = str(uuid.uuid4())
                storage.audit('warning', f'LIVE SPOT explicitly enabled for {self.exchange_name.upper()}.')
                observability.event('mode.changed', level='warning', mode='live', exchange=self.exchange_name)
            finally:
                with self.lock:
                    self.control_busy = False

    def update_config(self, data: dict):
        with self.lock:
            self._require_idle()
            revised = storage.validate_settings(data, base=self.cfg)
            changed_exchange = revised['exchange'] != self.exchange_name
            if changed_exchange and self.mode != 'paper':
                raise storage.ConfigValidationError('Return to paper mode before changing exchanges.')
            candidate_public = candidate_data = None
            if changed_exchange:
                # Constructor/adapter failures cannot leave an old authenticated
                # client paired with a new exchange ledger scope.
                candidate_public = self._new_exchange(revised['exchange'])
                candidate_data = MarketData(candidate_public, wait=self._retry_wait,
                                            concurrency=self._public_concurrency(candidate_public))
            # The kill switch is kept per exchange; an engaged one must not be
            # left behind when the operator moves to the other exchange.
            carried_switch = self.risk.kill_switch() if changed_exchange else None
            storage.save_settings(revised)
            changed_keys = sorted(key for key in revised if revised.get(key) != self.cfg.get(key))
            self.cfg = revised
            if not revised.get('streaming_quotes'):
                self._stop_feed()   # nothing may keep quoting from a stream the operator switched off
            changed_from = self.exchange_name
            if changed_exchange:
                self.exchange_name = revised['exchange']
                self.public, self.data = candidate_public, candidate_data
                self.paper = self._new_paper_exchange()
                self._stop_feed()
                self.private = self.api_key = self.api_secret = None
                self.credential_source = 'none'
                self.latest_balance = self.balance_updated_at = None
                self.last_prices.clear()
                self.last_asks.clear()
                self.last_price_times.clear()
                self.last_quote_basis.clear()
                self.last_received_at.clear()
                self.market_rows, self.discovered = [], []
                self.last_universe_update = self.markets_loaded_at = 0
                self.live_halted = self._durable_live_block()
                self._restore_from_keyring()
            self.risk_block = None
            target_switch = self.risk.kill_switch() if carried_switch and carried_switch['engaged'] else None
            # Carried when the target is released, or when it allows exits that the carried one blocks.
            if target_switch is not None and (not target_switch['engaged']
                                              or (target_switch['allow_exits'] and not carried_switch['allow_exits'])):
                self.risk.engage_kill_switch(f'Carried over from {changed_from.upper()}: {carried_switch.get("reason") or "kill switch"}',
                                             allow_exits=carried_switch['allow_exits'])
            self.manual_order_context = str(uuid.uuid4())
            storage.audit('info', 'Trading configuration validated and saved.')
            observability.event('settings.saved', changed=changed_keys, exchange=self.exchange_name)

    def reset_paper_portfolio(self):
        """Serialize reset with manual orders, scans, starts and account changes."""
        with self.lock:
            if self._busy() or self.mode != 'paper':
                raise storage.ConfigValidationError('Stop the bot, wait for active operations, and switch to paper mode before resetting.')
            storage.reset_paper(self.exchange_name)
            self.risk_block = None
            self.manual_order_context = str(uuid.uuid4())
            observability.event('paper.reset', exchange=self.exchange_name)

    def start(self):
        with self.lock:
            self._require_idle()
            if self.mode == 'live':
                if storage.settings_health()['review_required']:
                    raise storage.ConfigValidationError('Review and explicitly save the recovered settings before starting live trading.')
                if self.live_halted or self._durable_live_block():
                    raise storage.ConfigValidationError('Live trading is halted pending manual order/account reconciliation '
                                                        f'({self._live_block_detail()}).')
            self.control_busy = True
            generation = self.stop_generation
        try:
            if self.mode == 'live':
                self._ensure_private()
            positions = self._active_positions(self.mode)
            if self._marks_fresh(positions):
                cash = self._cash(self.mode)
                self._daily_allowed(self.mode, self._equity(self.mode, cash, positions))
            with self.lock:
                if generation != self.stop_generation:
                    return
                self.running, self.stopping = True, False
                self.stop_event.clear()
                self.thread = threading.Thread(target=self._loop, daemon=True, name='TradingEngine')
                self.thread.start()
            self._start_feed(generation)
            storage.audit('warning' if self.mode == 'live' else 'info', f'Bot started in {self.mode.upper()} mode.')
            observability.event('engine.started', level='warning' if self.mode == 'live' else 'info', mode=self.mode,
                                exchange=self.exchange_name, streaming=bool(self.cfg.get('streaming_quotes')),
                                settings_sha256=hashlib.sha256(repr(sorted(self.cfg.items())).encode('utf8')).hexdigest()[:16])
        finally:
            with self.lock:
                self.control_busy = False

    def stop(self):
        with self.lock:
            thread = self.thread
            self.stop_generation += 1
            self.stopping = bool(thread and thread.is_alive())
            self.stop_event.set()
            if not self.stopping:
                self.running = False
            storage.audit('info', 'Stop requested. Waiting for any in-flight exchange operation to finish.')
        observability.event('engine.stopped', mode=self.mode, exchange=self.exchange_name, in_flight=self.stopping)
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._stop_feed()

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Block until the worker and any manual order have finished, or ``timeout`` passes.

        Used at process shutdown so an order that is being confirmed is
        recorded before the interpreter exits. The durable journal still
        covers a hard kill: an unconfirmed intent halts live trading at the
        next start until it is reconciled.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        while True:
            with self.lock:
                worker = self.thread
                busy = self._manual_order_active or bool(worker and worker.is_alive()
                                                         and worker is not threading.current_thread())
            if not busy:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def _loop(self):
        next_scan = 0.0
        try:
            while not self.stop_event.is_set():
                exited = self._monitor_if_due()
                if time.monotonic() >= next_scan and not self.stop_event.is_set():
                    self.scan_once(allow_orders=True, exited_symbols=exited)
                    # Never catch up with a burst of scans after a slow API.
                    # A signal candle is assessed moments after it closes
                    # rather than up to a whole scan interval later.
                    next_scan = time.monotonic() + min(float(self.cfg['scan_seconds']),
                                                       self._seconds_to_next_close())
                delay = max(.05, min(next_scan, self._next_monitor) - time.monotonic())
                if self.stop_event.wait(delay):
                    break
        except Exception as exc:
            self._ledger_failure(f'Trading worker stopped: {type(exc).__name__}; inspect local portfolio and monitoring status')
        finally:
            with self.lock:
                self.running = self.stopping = False
            self._stop_feed()

    CLOSE_GRACE_SECONDS = 5.0
    # A stream quote older than this is not used; REST takes over for that symbol.
    STREAM_MAX_AGE_SECONDS = 10.0
    # With every held asset streaming, protective checks run this often (no REST cost).
    STREAM_MONITOR_SECONDS = 1.0

    # ------------------------------------------------------------ quote feed
    def _feed_symbols(self, symbols=None) -> list[str]:
        """Symbols to stream, held positions first, limited to pairs the loaded catalogue lists.

        One unknown pair fails a whole combined subscription, so a typo or a
        delisted watchlist entry is left to REST instead.
        """
        if symbols is None:
            held = [p['symbol'] for p in self._active_positions(self.mode)]
            symbols = held + list(self.cfg['watchlist']) + list(self.discovered)
        catalogue = self._catalogue()
        listed = [symbol for symbol in dict.fromkeys(symbols) if not catalogue or symbol in catalogue]
        return listed[:40]

    def _start_feed(self, generation: int | None = None) -> None:
        """Start the websocket quote stream when the setting asks for it.

        Failure to start is logged and leaves REST polling in charge; the
        stream is an accelerator, never a dependency. ``generation`` is the
        start's stop generation: a Stop (or a worker exit) that lands while
        the stream is being prepared must not leave it running on its own.
        """
        if not self.cfg.get('streaming_quotes'):
            self._stop_feed()
            return
        started = None
        try:
            started = self.feed
            if started is None or started.state in ('stopped', 'failed'):
                started = feed.QuoteFeed(self.exchange_name, self._feed_symbols(), metrics=self.metrics,
                                         heartbeat_seconds=max(5.0, min(60.0, float(self.cfg['data_max_age_seconds']) / 4)))
                self.feed = started
            started.start()
        except Exception as exc:
            self.feed = None
            storage.audit('warning', f'Streaming quotes unavailable ({type(exc).__name__}); REST polling remains in use.')
        if generation is not None:
            # stop() bumps the generation before its own _stop_feed, and the
            # worker clears ``running`` before its; either that call sees this
            # feed, or this check sees the stop. A Stop that ran between this
            # feed's creation and its start() already took it out of
            # self.feed, so it is stopped by reference too.
            with self.lock:
                abandoned = generation != self.stop_generation or not self.running
            if abandoned:
                self._stop_feed()
                if started is not None:
                    try:
                        started.stop(timeout=3)
                    except Exception:
                        pass

    def _stop_feed(self) -> None:
        current, self.feed = self.feed, None
        if current is not None:
            try:
                current.stop(timeout=3)
            except Exception:
                pass

    def _ticker_time(self, ticker) -> float | None:
        raw = ticker.get('timestamp') if isinstance(ticker, dict) else None
        if raw is not None and finite_positive(raw):
            return finite_positive(raw) / 1000 + self._clock_offset()
        received = finite_positive(ticker.get('_received_at')) if isinstance(ticker, dict) else 0.0
        return received or None

    def _quote(self, symbol: str, rest_ticker=None, *, need_volume: bool = False):
        """The freshest executable quote: the stream while live and recent, else REST.

        A REST ticker keeps its turnover and any newer timestamp; only bid,
        ask and the quote time come from the stream, and only when the stream
        is healthy and the quote is younger than ``STREAM_MAX_AGE_SECONDS``.
        Turnover exists only on REST tickers, so a caller that needs it gets
        a single REST read when no bulk snapshot was available.
        """
        current = self.feed
        streamed = current.ticker(symbol, max_age=self.STREAM_MAX_AGE_SECONDS) if current is not None else None
        if streamed is None:
            return rest_ticker
        if need_volume and not isinstance(rest_ticker, dict):
            rest_ticker = self.data.ticker(symbol)
        self.metrics.observe('feed.age_at_use_ms', max(0.0, time.time() - streamed['_received_at']) * 1000)
        if isinstance(rest_ticker, dict):
            rest_time, stream_time = self._ticker_time(rest_ticker), self._ticker_time(streamed)
            if rest_time is not None and stream_time is not None and rest_time > stream_time:
                return rest_ticker
            merged = dict(rest_ticker)
            merged.update({key: streamed[key] for key in ('bid', 'ask', 'timestamp', '_received_at', '_source')})
            return merged
        return streamed

    def _seconds_to_next_close(self) -> float:
        """Seconds until the next primary candle close plus a short grace.

        The grace outlasts the completed-candle cache expiry (two seconds
        after a close), so the scan downloads the newly closed candle.
        """
        duration = TIMEFRAME_SECONDS[HORIZONS[self.cfg['time_horizon']][0]]
        exchange_now = time.time() - self._clock_offset()
        return duration - exchange_now % duration + self.CLOSE_GRACE_SECONDS

    def _orders_allowed(self) -> bool:
        return bool((self.running or self._manual_order_active) and not self.stop_event.is_set()
                    and (self.mode == 'paper' or not self.live_halted))

    def _monitor_if_due(self, *, force: bool = False) -> set[str]:
        """Check stored exits independently of strategy scans and candle data.

        The worker also calls this between candle downloads. Calls are serialized
        on the same worker; an in-flight exchange request cannot be preempted.
        """
        if not self._orders_allowed() or (not force and time.monotonic() < self._next_monitor):
            return set()
        interval = max(2.0, min(60.0, float(self.cfg.get('protective_poll_seconds', 5))))
        self._next_monitor = time.monotonic() + interval
        positions = self._active_positions(self.mode)
        exited, failures, observed = set(), 0, 0
        with self.lock:
            self.last_monitor, self.monitor_error = now(), None
        if not positions:
            return exited
        started = time.monotonic()
        held = [p['symbol'] for p in positions]
        current = self.feed          # read once: the settings thread can stop and clear it meanwhile
        streamed = current is not None and current.covers(held, max_age=self.STREAM_MAX_AGE_SECONDS)
        if streamed:
            # Every held asset has a live stream quote: no REST round trip, and
            # the protective check repeats every second instead of every poll.
            interval = min(interval, self.STREAM_MONITOR_SECONDS)
            tickers = {}
        else:
            try:
                tickers = self.data.quotes(held)
            except Exception:
                tickers = {}
        self.metrics.incr('monitor.source.stream' if streamed else 'monitor.source.rest')
        for position in positions:
            if not self._orders_allowed():
                break
            symbol = position['symbol']
            try:
                ticker = self._quote(symbol, tickers.get(symbol))
                try:
                    price, _, _ = self._observe_ticker(symbol, ticker or self.data.ticker(symbol))
                except ValueError:
                    if not ticker:
                        raise
                    price, _, _ = self._observe_ticker(symbol, self.data.ticker(symbol))
                observed += 1
                self._protect_positions(symbol, price)
                if self._orders_allowed():
                    primary_tf = HORIZONS[self.cfg['time_horizon']][0]
                    saved = self.data.cached_candles(symbol, primary_tf, limit=175)
                    if saved is not None and not saved.empty:
                        # Trail with already validated ATR; monitoring never
                        # downloads candles or weakens a persisted stop.
                        self._mark_positions(symbol, price, finite_positive(saved.iloc[-1]['atr']), False,
                                             stored_checked_at=price, source='monitor')
                if not any(p['symbol'] == symbol for p in self._active_positions(self.mode)):
                    exited.add(symbol)
            except Exception:
                failures += 1
        # Finish-relative scheduling avoids hot loops on slow or failing reads.
        self._next_monitor = time.monotonic() + interval
        self.metrics.observe('monitor.cycle_ms', (time.monotonic() - started) * 1000)
        with self.lock:
            if observed:
                self.last_successful_monitor = now()
            if failures:
                self.monitor_error = f'Protective quotes unavailable for {failures} held asset(s)'
        remaining = self._active_positions(self.mode)
        if self._marks_fresh(remaining) and self._orders_allowed():
            try:
                # Filled orders invalidate this timestamp. Otherwise reuse a
                # recent cash read while every poll still marks held assets.
                if (self.mode == 'live' and self.latest_balance is not None
                        and self.balance_updated_at is not None
                        and 0 <= time.time() - self.balance_updated_at <= 30):
                    cash = float(self.latest_balance['free'].get('USDT', 0))
                else:
                    cash = self._cash(self.mode)
                self._daily_allowed(self.mode, self._equity(self.mode, cash, remaining))
            except Exception:
                with self.lock:
                    self.monitor_error = self.monitor_error or 'Portfolio risk balance unavailable; fresh account data is required for new entries'
        self._next_monitor = time.monotonic() + interval
        return exited

    @staticmethod
    def _spot(symbol: str, market: dict | None) -> bool:
        if (not isinstance(market, dict) or not symbol.endswith('/USDT')
                or market.get('spot') is not True or market.get('active') is not True
                or any(market.get(k) for k in ('contract', 'swap', 'future', 'option'))
                or market.get('type', 'spot') != 'spot'):
            return False
        info = market.get('info')
        if isinstance(info, dict):
            # MEXC lists pairs that accept limit orders only; every bot order is a market order.
            order_types = info.get('orderTypes')
            if isinstance(order_types, list) and 'MARKET' not in order_types:
                return False
        base = symbol.split('/')[0]
        # Leveraged tokens carry a direction suffix after a real base (BTC3L,
        # ETHBULL). A base that is only the suffix, or one letter plus it (JUP), is a coin.
        return not any(base.endswith(suffix) and len(base) - len(suffix) >= 2
                       for suffix in ('3L', '3S', '5L', '5S', 'UP', 'DOWN', 'BULL', 'BEAR'))

    def _fetch_markets(self):
        """Load the public catalogue once, then refresh it hourly.

        A failed refresh keeps the previous catalogue rather than trading on
        nothing; a listing change is still caught at the exchange.
        """
        if self.markets_loaded_at and time.monotonic() - self.markets_loaded_at < 3600:
            return self.data.markets()
        try:
            markets = self.data.read('load_markets', True) if self.markets_loaded_at else self.data.markets()
        except Exception as exc:
            if not self.markets_loaded_at:
                raise
            storage.audit('warning', f'Market catalogue refresh unavailable: {type(exc).__name__}; the previous catalogue remains in use.')
            return self._catalogue()
        self.markets_loaded_at = time.monotonic()
        return markets

    def _universe(self) -> list[str]:
        held = [p['symbol'] for p in self._active_positions(self.mode)]
        self._fetch_markets()
        if self.cfg['auto_discovery'] and time.monotonic() - self.last_universe_update > 3600:
            try:
                if getattr(self.public, 'has', {}).get('fetchTickers') is not True:
                    raise ValueError('Bulk tickers unsupported')
                tickers = self.data.read('fetch_tickers')
                ranking = []
                catalogue = self._catalogue()
                for symbol, ticker in tickers.items():
                    if self._spot(symbol, catalogue.get(symbol)):
                        volume = finite_positive(ticker.get('quoteVolume'))
                        if volume >= self.cfg['min_quote_volume']:
                            ranking.append((volume, symbol))
                ranking.sort(reverse=True)
                with self.lock:
                    self.discovered = [symbol for _, symbol in ranking[:int(self.cfg['discovered_pairs'])]]
                    self.last_universe_update = time.monotonic()
            except Exception as exc:
                storage.audit('warning', f'Automatic discovery unavailable: {type(exc).__name__}.')
        # Held symbols never compete with watchlist/discovery truncation.
        extras = list(dict.fromkeys(self.cfg['watchlist'] + self.discovered))[:32]
        return list(dict.fromkeys(held + extras))

    def _active_positions(self, mode: str) -> list[dict]:
        return storage.rows('SELECT * FROM positions WHERE exchange=? AND mode=? ORDER BY opened_at',
                            (self.exchange_name, mode))

    def _cash(self, mode: str) -> float:
        if mode == 'paper':
            raw = storage.get_meta(self._key('paper_cash'), str(self.cfg['paper_start_usdt']))
            value = float(raw)
            if not math.isfinite(value) or value < 0:
                raise ValueError('Paper cash ledger is invalid')
            return value
        return float(self._read_balance()['free'].get('USDT', 0))

    def _quote_basis_time(self, symbol: str) -> float | None:
        """When the stored quote existed, on this PC's clock.

        An exchange timestamp ahead of the moment the quote arrived (a stale
        clock offset) is capped at the arrival time, so it cannot make the
        quote look younger than it is.
        """
        timestamp = self.last_price_times.get(symbol)
        if timestamp is None:
            return None
        received = self.last_received_at.get(symbol)
        return min(timestamp, received) if received else timestamp

    def _price_age(self, symbol: str) -> float | None:
        basis = self._quote_basis_time(symbol)
        return max(0.0, time.time() - basis) if basis is not None else None

    def _fresh(self, symbol: str) -> bool:
        age, basis = self._price_age(symbol), self._quote_basis_time(symbol)
        # A quote stored "in the future" means this PC's clock stepped back
        # since; its age is unknown, so it cannot count as fresh.
        return bool(finite_positive(self.last_prices.get(symbol)) and age is not None
                    and age <= self.cfg['data_max_age_seconds'] and basis <= time.time() + 1.0)

    def _marks_fresh(self, positions: list[dict]) -> bool:
        with self.lock:
            return all(self._fresh(p['symbol']) for p in positions)

    def _observe_ticker(self, symbol: str, ticker: dict, *, for_buy: bool = False) -> tuple[float, float, float | None]:
        """Observe bid/ask quotes; last trade prices cannot value an exit.

        A ticker's best quotes are indicative, not a depth or fill guarantee.
        CCXT fields are optional, so missing or crossed quotes fail closed.
        """
        def invalid(message):
            with self.lock:
                self.last_price_times.pop(symbol, None)
                self.last_asks.pop(symbol, None)
                self.last_quote_basis.pop(symbol, None)
                self.last_received_at.pop(symbol, None)
            raise ValueError(message)
        if not isinstance(ticker, dict):
            invalid('Invalid ticker response')
        bid, ask = finite_positive(ticker.get('bid')), finite_positive(ticker.get('ask'))
        ask_absent = ticker.get('ask') is None
        if not bid or (not ask and (for_buy or not ask_absent)) or (ask and bid > ask):
            invalid('Missing or crossed bid-ask quotes')
        observed = time.time()
        raw_timestamp = ticker.get('timestamp')
        received = finite_positive(ticker.get('_received_at'))
        received = received if received and received <= observed else observed
        if raw_timestamp is None:
            # Exchanges without ticker timestamps (Kraken, MEXC's book ticker)
            # are aged from the moment the response arrived, not from this
            # later observation.
            timestamp, basis = received, 'receipt'
        else:
            timestamp, basis = finite_positive(raw_timestamp) / 1000 + self._clock_offset(), 'exchange'
        if (not timestamp or timestamp > observed + 30
                or observed - timestamp > self.cfg['data_max_age_seconds']):
            invalid('Ticker timestamp is stale or invalid')
        with self.lock:
            previous = self.last_price_times.get(symbol)
            previous_basis, previous_received = self.last_quote_basis.get(symbol), self.last_received_at.get(symbol)
            stored_bid, stored_ask = self.last_prices.get(symbol), self.last_asks.get(symbol)
            # A bulk snapshot taken before a later observation (a protective
            # poll or the stream) must not roll the mark back in time. Two
            # exchange timestamps compare directly; otherwise arrival order
            # decides, because an exchange clock that runs ahead of this PC
            # would make every receipt-stamped quote look older than it is.
            if previous is None:
                older = False
            elif basis == previous_basis == 'exchange':
                older = timestamp < previous - self.QUOTE_REGRESSION_SECONDS
            elif previous_received is not None and previous_received > observed:
                older = False      # the PC clock stepped back since: the stored time cannot be trusted
            elif previous_received is not None:
                older = received < previous_received - self.QUOTE_REGRESSION_SECONDS
            else:
                older = timestamp < previous - self.QUOTE_REGRESSION_SECONDS
            keep_stored = bool(older and finite_positive(stored_bid) and (stored_ask or not for_buy))
            if keep_stored:
                bid, ask = stored_bid, stored_ask
            else:
                self.last_prices[symbol] = bid
                if ask:
                    self.last_asks[symbol] = ask
                else:
                    self.last_asks.pop(symbol, None)
                self.last_price_times[symbol] = timestamp
                self.last_quote_basis[symbol] = basis
                self.last_received_at[symbol] = received
        if keep_stored:
            self.metrics.incr('quotes.superseded')
        spread = 100 * (ask - bid) / ((ask + bid) / 2) if ask else None
        return ask if for_buy else bid, finite_positive(ticker.get('quoteVolume')), spread

    # A quote more than this much older than the one already held is a stale snapshot.
    QUOTE_REGRESSION_SECONDS = 1.0

    def _clock_offset(self) -> float:
        """Seconds to add to an exchange timestamp to compare it with this PC's clock.

        CCXT measures local-minus-server once per catalogue load when
        adjustForTimeDifference is enabled (MEXC). Without it, no offset applies.
        """
        options = getattr(self.public, 'options', None)
        difference = options.get('timeDifference') if isinstance(options, dict) else None
        if isinstance(difference, bool) or not isinstance(difference, (int, float)) or not math.isfinite(difference):
            return 0.0
        return difference / 1000

    def _equity(self, mode: str, cash: float, positions: list[dict]) -> float:
        liquidation_factor = (1 - self.cfg['slippage_pct'] / 100) * (1 - self.cfg['fee_pct'] / 100)
        with self.lock:
            return cash + sum(p['qty'] * self.last_prices.get(p['symbol'], p['entry_price'])
                              * liquidation_factor for p in positions)

    def _daily_allowed(self, mode: str, equity: float) -> tuple[bool, float]:
        if not math.isfinite(equity) or equity <= 0:
            self.risk_block = 'Invalid portfolio equity'
            return False, 100.0
        today = datetime.now(timezone.utc).date().isoformat()
        baseline_key, breach_key = self._key(f'baseline.{mode}.{today}'), self._key(f'breached.{mode}.{today}')
        baseline = finite_positive(storage.get_meta(baseline_key))
        if not baseline:
            baseline = equity
            storage.set_meta(baseline_key, baseline)
        loss = 100 * (baseline - equity) / baseline
        if loss >= self.cfg['daily_loss_limit_pct']:
            storage.set_meta(breach_key, '1')
        allowed = storage.get_meta(breach_key) != '1'
        # The same fresh valuation feeds the durable peak/drawdown stop and
        # latches the realised-loss stop as soon as an exit books the loss.
        self.risk.observe_equity(mode, equity)
        self.risk.observe_realized(mode, equity)
        if not allowed:
            with self.lock:
                self.risk_block = 'Daily loss limit reached; entries blocked until the next UTC day'
        return allowed, round(loss, 2)

    def _halt_uncertain(self, description: str, *, intent_id: str | None = None):
        with self.lock:
            self.live_halted = True
            self.risk_block = self.last_error = description
            self.stop_event.set()
        self.risk.record_failure('uncertain')
        observability.event('execution.halt', level='error', description=description, intent_id=intent_id)
        # Persist independently of an order journal so an intent/ledger failure
        # cannot be bypassed by restarting the process.
        try:
            storage.set_meta(self._key('live_halt'), '1')
        except Exception:
            with self.lock:
                self._halt_pending = self.exchange_name
        try:
            storage.audit('error', 'LIVE HALT: ' + description + '. Reconcile manually; no automatic resubmission.')
        except Exception:
            pass

    def _ledger_failure(self, description: str, *, intent_id: str | None = None, mode: str | None = None):
        """Stop on an order or ledger outcome that needs review; a live one halts durably.

        Every caller that has an order intent passes its id, so the failure
        line correlates with the journal row and the submit/fill events.
        """
        mode = mode or self.mode
        if mode == 'live':
            self._halt_uncertain(description, intent_id=intent_id)
        else:
            with self.lock:
                self.risk_block = self.last_error = description
                self.stop_event.set()
            self.risk.record_failure('ledger')
        observability.event('ledger.failure', level='error', description=description, intent_id=intent_id, mode=mode)

    CONFIRM_DELAYS = (.25, .5, 1., 2., 3.)

    def _confirm_filled(self, client, symbol: str, order: dict, *, client_order_id: str | None = None) -> dict:
        """Poll until the accepted order reports a final state.

        The order already exists at the exchange, so a Stop request never
        shortens this wait: an unread outcome would only become a halt. An
        acceptance that carried no exchange id is re-read by client order id.
        A final state confirmed by a fresh read with no fill at all raises
        :class:`orders.Unfilled`, which the caller records as a rejection.
        """
        last = len(self.CONFIRM_DELAYS) - 1
        fetched = False
        for attempt, delay in enumerate(self.CONFIRM_DELAYS):
            if not isinstance(order, dict):
                break
            state = orders.classify(order)
            if state == 'final_filled' and order.get('id'):
                return order  # A final partial fill is accounted, never silently erased.
            if state == 'final_empty' and fetched and order.get('id'):
                raise orders.Unfilled(f'{symbol} order {order.get("id")} ended {order.get("status")} with no fill')
            if attempt == last:
                break
            if state != 'final_empty':
                time.sleep(delay)
            try:
                with self.private_lock:
                    if order.get('id'):
                        order = client.fetch_order(order['id'], symbol)
                    elif client_order_id:
                        order = orders.locate(client, symbol, client_order_id) or order
                    else:
                        break
                fetched = True
            except Exception:
                continue
        raise RuntimeError('Order acceptance/final fill is uncertain; manual reconciliation is required')

    def _resolve_fees(self, client, symbol: str, order: dict) -> dict:
        """Attach fees from the order's own trades when the order reports none.

        MEXC order records carry no fee; its trade records do. A failed or
        empty read leaves the order unchanged, so the configured estimate
        applies rather than a blocked ledger.
        """
        reported = order.get('fees') or ([order['fee']] if isinstance(order.get('fee'), dict) else [])
        if any(isinstance(fee, dict) and fee.get('currency') and fee.get('cost') is not None for fee in reported):
            return order
        supported = getattr(client, 'has', {}).get('fetchOrderTrades')
        order_id = self._order_id(order)
        if not supported or not order_id:
            return order
        params = {}
        if supported == 'emulated':
            # Kraken resolves trades from the identifiers its order record lists.
            trades = order.get('trades')
            if not isinstance(trades, list) or not trades:
                return order
            params['trades'] = trades
        try:
            with self.private_lock:
                trades = client.fetch_order_trades(order_id, symbol, None, None, params)
        except Exception as exc:
            storage.audit('warning', f'Executed fees for {symbol} unavailable: {type(exc).__name__}; the configured fee estimate applies.')
            return order
        if not isinstance(trades, list) or not trades:
            return order
        fees = [trade.get('fee') for trade in trades if isinstance(trade, dict)]
        if len(fees) != len(trades) or not all(isinstance(fee, dict) and fee.get('currency') for fee in fees):
            return order
        return {**order, 'fees': fees}

    def _locate_after_ambiguity(self, client, symbol: str, client_order_id: str, exc: Exception):
        """One read-only lookup by client order id after an ambiguous submit error.

        Never resends. ``None`` means the exchange could not confirm the
        order, which the caller must treat as uncertain, not as absent.
        """
        self.metrics.incr('execution.ambiguous_submit')
        observability.event('execution.ambiguous', level='warning', symbol=symbol, error=type(exc).__name__)
        found = None
        for pause in (0.0, 1.0):
            if pause:
                time.sleep(pause)
            try:
                found = orders.locate(client, symbol, client_order_id)
            except Exception as lookup_exc:
                observability.event('execution.lookup_failed', level='warning', symbol=symbol,
                                    error=type(lookup_exc).__name__)
                return None
            if found is not None:
                break
        if found is not None:
            storage.audit('warning', f'{symbol}: the order request failed with {type(exc).__name__} but the exchange '
                                     'reports the order under its client order id; continuing with the exchange record. '
                                     'No order was resent.')
            observability.event('execution.located', level='warning', symbol=symbol, order_id=self._order_id(found))
            self.metrics.incr('execution.located_after_ambiguity')
        return found

    def _submit(self, client, symbol: str, side: str, *, quantity=None, cost=None,
                authorization: risk.Authorization | None = None, intended_price: float | None = None,
                decision_id: str | None = None):
        """Durable intent precedes exactly one submission; reads alone may repeat.

        Paper and live orders share this path; only ``client`` differs. Nothing
        reaches the exchange without a fresh risk authorization issued for this
        symbol and side; a caller that skips the gate is refused and audited.
        The intent id doubles as the exchange client order id, so an ambiguous
        transport error is resolved by a read-only lookup, never by resending.
        """
        intent = None
        accepted = False   # the counterparty is known to hold the order
        mode = self.mode
        if authorization is None or not authorization.valid_for(symbol, side):
            self.metrics.incr('risk.unauthorized_submit')
            storage.audit('error', f'{side} {symbol} refused: no valid risk authorization for this order.')
            observability.event('risk.unauthorized_submit', level='error', symbol=symbol, side=side)
            return None, None
        started = time.perf_counter()
        try:
            if not self._may_execute():
                return None, None
            intent = storage.create_order_intent(self.exchange_name, symbol, side, quantity=quantity, cost=cost,
                                                  mode=mode, intended_price=intended_price, decision_id=decision_id)
            client_id = storage.client_order_id(intent)
            if client is self.paper:
                params = {'clientOrderId': client_id, 'reference_price': intended_price}
            else:
                params = orders.order_params(self.exchange_name, client_id)
            with observability.bind(intent_id=intent, decision_id=decision_id, mode=mode):
                with self.private_lock:
                    if not self._may_execute():
                        storage.update_order_intent(intent, 'rejected', note='stopped before submission')
                        return None, None
                    # The authorization was computed moments ago; a kill switch
                    # engaged since then still stops this order from leaving.
                    blocked = self.risk.switch_blocks(authorization.request)
                    if blocked:
                        storage.update_order_intent(intent, 'rejected', note='kill switch engaged before submission')
                        self.metrics.incr('risk.blocked_at_submit')
                        storage.audit('warning', f'{side} {symbol} not sent: {blocked}.')
                        observability.event('risk.blocked_at_submit', level='warning', symbol=symbol, side=side,
                                            reason=blocked)
                        return None, None
                    # The request leaves now; a crash after this point may have an order at the exchange.
                    storage.update_order_intent(intent, 'pending', submitted_at=storage._now())
                    # Carries the intent and decision ids (bound above) and the
                    # authorization token, which names the risk.authorize line.
                    observability.event('execution.submitted', symbol=symbol, side=side, client_order_id=client_id,
                                        token=authorization.token, quantity=quantity, cost=cost,
                                        intended_price=intended_price)
                    sent = time.perf_counter()
                    try:
                        if side == 'BUY':
                            order = client.create_market_buy_order_with_cost(symbol, cost, params)
                        else:
                            order = client.create_order(symbol, 'market', 'sell', quantity, None, params)
                        accepted = True
                    except Exception as exc:
                        if isinstance(exc, orders.definite_rejections()):
                            raise
                        located = self._locate_after_ambiguity(client, symbol, client_id, exc)
                        if located is None:
                            raise
                        order, accepted = located, True
                ack_ms = (time.perf_counter() - sent) * 1000
                self.metrics.observe('execution.submit_to_ack_ms', ack_ms)
                order_id = self._order_id(order)
                storage.update_order_intent(intent, 'acked' if order_id else 'submitted', order_id=order_id)
                order = self._confirm_filled(client, symbol, order, client_order_id=client_id)
                summary = orders.fill_summary(order)
                storage.update_order_intent(intent, 'filled', order_id=self._order_id(order),
                                            filled_price=summary['average'], filled_quantity=summary['filled'])
                total_ms = (time.perf_counter() - started) * 1000
                self.metrics.observe('execution.ack_to_final_ms', total_ms - ack_ms)
                self.metrics.observe('execution.submit_total_ms', total_ms)
                observability.event('execution.filled', symbol=symbol, side=side, order_id=self._order_id(order),
                                    filled=summary['filled'], average=summary['average'],
                                    slippage_bps=orders.slippage_bps(intended_price, summary['average'], side),
                                    submit_to_ack_ms=ack_ms, total_ms=total_ms)
                try:
                    order = self._resolve_fees(client, symbol, order)
                except Exception:
                    pass  # A fee read is optional; the fill itself is confirmed.
                return intent, order
        except orders.Unfilled as exc:
            # A fresh read confirmed a final state with zero fill: nothing changed hands.
            try:
                storage.update_order_intent(intent, 'rejected', note='final state without any fill')
                with self.lock:
                    self.risk_block = f'{side} {symbol} ended without a fill: {exc}'
                storage.audit('warning', self.risk_block)
                self.risk.record_failure('unfilled')
                observability.event('execution.unfilled', level='warning', symbol=symbol, side=side, intent_id=intent)
                return None, None
            except Exception:
                pass
            self._ledger_failure(f'{side} {symbol}: unfilled order could not be journaled; review required',
                                 intent_id=intent)
            return None, None
        except Exception as exc:
            # A documented exchange rejection confirms no order was accepted.
            # Transport errors and unrecognized responses always remain uncertain.
            if intent and not accepted and isinstance(exc, orders.definite_rejections()):
                try:
                    storage.update_order_intent(intent, 'rejected', note=type(exc).__name__)
                    with self.lock:
                        self.risk_block = f'{side} {symbol} rejected by the exchange: {type(exc).__name__}'
                    storage.audit('warning', self.risk_block)
                    self.risk.record_failure('rejected')
                    observability.event('execution.rejected', level='warning', symbol=symbol, side=side,
                                        error=type(exc).__name__, intent_id=intent)
                    return None, None
                except Exception:
                    pass
            if intent:
                try:
                    storage.update_order_intent(intent, 'uncertain', note=type(exc).__name__)
                except Exception:
                    pass
            self._ledger_failure(f'{side} {symbol}: {type(exc).__name__}; order or journal outcome requires review',
                                 intent_id=intent)
            return None, None

    def _fill_details(self, order: dict, symbol: str) -> tuple[float, float, float, float, float, float, float]:
        """Return filled quantity, price, gross, quote/base fees, fee value in USDT, and its estimated part.

        The last value is the estimate booked for a fee billed in a third
        asset; it is already inside the fee value and is 0 otherwise.
        """
        filled, gross = finite_positive(order.get('filled')), finite_positive(order.get('cost'))
        if not filled or not gross:
            raise ValueError('Confirmed order lacks finite executed quantity/cost')
        derived_price = gross / filled
        if not finite_positive(derived_price):
            raise ValueError('Executed quantity and cost imply an invalid price')
        average = order.get('average')
        price = finite_positive(average) if average is not None else derived_price
        if not price or abs(price / derived_price - 1) > .01:
            raise ValueError('Executed average price contradicts quantity and cost')
        fees = order.get('fees')
        if not fees:
            fees = [order['fee']] if isinstance(order.get('fee'), dict) else []
        if not fees:
            return filled, price, gross, gross * self.cfg['fee_pct'] / 100, 0.0, gross * self.cfg['fee_pct'] / 100, 0.0
        if not isinstance(fees, list):
            raise ValueError('Unrecognized exchange fee response')
        quote_fee = base_fee = 0.0
        base, quote = symbol.split('/')
        other: dict[str, float] = {}
        for fee in fees:
            if not isinstance(fee, dict):
                raise ValueError('Malformed exchange fee')
            amount = fee.get('cost')
            if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount < 0:
                raise ValueError('Unknown or invalid exchange fee amount')
            if amount == 0:
                continue
            if fee.get('currency') == quote:
                quote_fee += amount
            elif fee.get('currency') == base:
                base_fee += amount
            else:
                other[str(fee.get('currency'))] = other.get(str(fee.get('currency')), 0.0) + float(amount)
        value, estimate = quote_fee + base_fee * price, 0.0
        if other:
            # A fee billed in a third asset (MEXC's MX deduction) touches neither the
            # quote cash nor the base quantity of this fill, so cash and quantity are
            # booked exactly; only the fee's value is an estimate, and it enters the
            # cost basis and realised P/L. Dropping a confirmed fill from the ledger
            # would leave a real holding unprotected. When part of the fee was billed
            # in the quote or base asset, only the remainder of the configured rate
            # is estimated, so a mixed fee is not counted twice.
            estimate = max(0.0, gross * self.cfg['fee_pct'] / 100 - value)
            value += estimate
            charged = ', '.join(f'{amount:.8g} {currency}' for currency, amount in sorted(other.items()))
            storage.audit('warning', f'{symbol}: fee charged in another asset ({charged}); its value is booked as the '
                                     f'configured estimate {estimate:.4f} USDT. Check the exchange fee ledger.')
            observability.event('execution.fee_unvalued', level='warning', symbol=symbol, fees=other, estimate=estimate)
        return filled, price, gross, quote_fee, base_fee, value, estimate

    @staticmethod
    def _is_dust(position: dict, market: dict | None, price: float) -> bool:
        """A tracked remainder below an explicit exchange minimum stays owned."""
        if not isinstance(market, dict) or not finite_positive(price):
            return False
        quantity = finite_positive(position.get('qty'))
        limits = market.get('limits') or {}
        minimum_amount = finite_positive((limits.get('amount') or {}).get('min'))
        minimum_cost = finite_positive((limits.get('cost') or {}).get('min'))
        return bool(quantity and ((minimum_amount and quantity < minimum_amount)
                                  or (minimum_cost and quantity * price < minimum_cost)))

    @staticmethod
    def _complete_manual_request(db, completion, remaining: float):
        """Commit the user-visible result with its fills, never in a later write."""
        if completion is None:
            return
        payload = completion['payload']
        result = {**payload, 'status': 'partial' if payload['side'] == 'SELL' and remaining > 0 else 'filled',
                  'remaining_quantity': remaining, 'request_id': completion['request_id']}
        record = {**completion['record'], 'status': 'filled', 'result': result}
        db.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)',
                   (completion['scope'], json.dumps(record, allow_nan=False)))

    def _authorize(self, symbol: str, side: str, kind: str, notional: float, price: float, *,
                   positions: list[dict], cash: float | None, quantity: float | None = None,
                   stop_fraction: float | None = None, signal_close: float | None = None,
                   atr: float | None = None, spread: float | None = None, reason: str = '',
                   decision_id: str | None = None) -> risk.Authorization:
        """Build the request for the risk gate from the engine's current marks.

        Every production caller observes a ticker immediately before pricing
        an order, so a quote that has since gone stale is blocked. A symbol
        whose last quote was discarded as invalid keeps its stale mark and is
        therefore judged stale too. Only a caller that priced the order itself
        without any observation ever (tests, benchmarks) is judged on the price
        it supplied.
        """
        with self.lock:
            observed = symbol in self.last_price_times or symbol in self.last_prices
            mode = self.mode
        equity = self._equity(mode, cash, positions) if cash is not None else None
        request = risk.OrderRequest(exchange=self.exchange_name, mode=mode, symbol=symbol, side=side, kind=kind,
                                    notional=float(notional), price=float(price), quantity=quantity,
                                    signal_close=signal_close, atr=atr, stop_fraction=stop_fraction,
                                    quote_age_seconds=self._price_age(symbol), spread_pct=spread,
                                    reason=reason, decision_id=decision_id)
        with self.metrics.timer('risk.authorize_ms'):
            authorization = self.risk.authorize(request, positions=positions, cash=cash, equity=equity,
                                                marks_fresh=self._marks_fresh(positions),
                                                symbol_fresh=self._fresh(symbol) if observed else bool(finite_positive(price)))
        if not authorization.allowed:
            storage.audit('warning', f'RISK BLOCK {side} {symbol}: {authorization.reason}')
        with self.lock:
            self._last_authorization = authorization
        return authorization

    def _buy(self, symbol: str, budget: float, price: float, fraction: float, *,
             reason: str = 'Completed-candle momentum breakout', merge_dust: bool = False,
             completion=None, entry_candle: int | None = None, kind: str = 'entry',
             signal_close: float | None = None, atr: float | None = None,
             spread: float | None = None, decision_id: str | None = None) -> bool:
        existing = next((p for p in self._active_positions(self.mode) if p['symbol'] == symbol), None)
        if (not all(finite_positive(v) for v in (budget, price, fraction)) or fraction >= 1
                or (existing and (not merge_dust or not self._is_dust(
                    existing, self._catalogue().get(symbol), price)))):
            return False
        mode, exchange = self.mode, self.exchange_name
        fee_rate, slip = self.cfg['fee_pct'] / 100, self.cfg['slippage_pct'] / 100
        intent = order_id = None
        positions = self._active_positions(mode)
        gate_fields = dict(stop_fraction=fraction, signal_close=signal_close, atr=atr, spread=spread,
                           reason=reason, decision_id=decision_id)
        if mode == 'paper':
            authorization = self._authorize(symbol, 'BUY', kind, budget, price, positions=positions,
                                            cash=self._cash('paper'), **gate_fields)
            if not authorization.allowed:
                return False
            # The simulated counterparty fills the fee-exclusive amount at the
            # observed ask plus slippage; the same submit/confirm/journal path
            # as live runs, only the client differs.
            intent, order = self._submit(self.paper, symbol, 'BUY', cost=budget / (1 + fee_rate),
                                         authorization=authorization, intended_price=price, decision_id=decision_id)
            if order is None:
                return False
            try:
                filled, fill_price, gross, quote_fee, base_fee, fee, estimated = self._fill_details(order, symbol)
                qty = filled - base_fee
                # A cost-bounded simulated buy spends exactly its budget; a
                # partial simulated fill (failure injection) spends what filled.
                cost = budget if abs(gross + quote_fee - budget) <= budget * 1e-9 else gross + quote_fee
                order_id = self._order_id(order)
            except Exception as exc:
                self._ledger_failure(f'PAPER BUY {symbol}: {type(exc).__name__}; simulated fill accounting requires review',
                                     intent_id=intent)
                return False
        else:
            if self.live_halted or self._durable_live_block():
                self._halt_uncertain('Live execution blocked by unresolved order/account state')
                return False
            client = self._ensure_private()
            if getattr(client, 'has', {}).get('createMarketBuyOrderWithCost') is False:
                raise storage.ConfigValidationError('This exchange adapter does not support bounded-cost market buys.')
            with self.private_lock:
                market = client.market(symbol)
                # The authenticated client keeps the catalogue it loaded at
                # connection; the public copy is refreshed hourly.
                if not self._spot(symbol, market) or not self._spot(symbol, self._catalogue().get(symbol)):
                    raise storage.ConfigValidationError('Live orders require a verified active spot market.')
                info = market.get('info')
                if isinstance(info, dict) and info.get('quoteOrderQtyMarketAllowed') is False:
                    raise storage.ConfigValidationError('This pair does not accept market buys by USDT amount on the exchange.')
                if existing and not self._is_dust(existing, market, price):
                    raise storage.ConfigValidationError('The existing position exceeds the verified exchange dust limit.')
                spend = float(client.cost_to_precision(symbol, budget / (1 + fee_rate)))
            limits = market.get('limits') or {}
            minimum = finite_positive((limits.get('cost') or {}).get('min'))
            maximum = finite_positive((limits.get('cost') or {}).get('max'))
            if not finite_positive(spend) or spend > budget or spend < minimum or (maximum and spend > maximum):
                raise ValueError('Order cost does not meet exchange limits/precision')
            free_usdt = float(self._read_balance(client)['free'].get('USDT', 0))
            if free_usdt < budget:
                raise ValueError('Insufficient free USDT including the fee reserve')
            authorization = self._authorize(symbol, 'BUY', kind, budget, price, positions=positions,
                                            cash=free_usdt, **gate_fields)
            if not authorization.allowed:
                return False
            intent, order = self._submit(client, symbol, 'BUY', cost=spend, authorization=authorization,
                                         intended_price=price, decision_id=decision_id)
            if order is None:
                return False
            try:
                filled, fill_price, gross, quote_fee, base_fee, fee, estimated = self._fill_details(order, symbol)
                qty, cost = filled - base_fee, gross + quote_fee
                if qty <= 0 or cost > budget * 1.03:
                    raise ValueError('Executed quantity/cost differs from the reserved budget')
                order_id = self._order_id(order)
            except Exception as exc:
                self._ledger_failure(f'BUY {symbol}: {type(exc).__name__}; fill/fee accounting requires review',
                                     intent_id=intent, mode='live')
                return False
        try:
            with storage.connection() as db:
                if mode == 'paper':
                    key = self._key('paper_cash')
                    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
                    cash = float(row['value'])
                    if cost > cash:
                        return False
                    db.execute('UPDATE meta SET value=? WHERE key=?', (str(cash - cost), key))
                if existing:
                    # Only explicit manual buys can consolidate verified dust.
                    # Retain every unit and its old cost basis in one transaction.
                    current = db.execute('SELECT qty,cost FROM positions WHERE exchange=? AND mode=? AND symbol=?',
                                         (exchange, mode, symbol)).fetchone()
                    if (not current or float(current['qty']) != float(existing['qty'])
                            or float(current['cost']) != float(existing['cost'])):
                        raise ValueError('Tracked remainder changed before consolidation')
                    total_qty, total_cost = qty + float(existing['qty']), cost + estimated + float(existing['cost'])
                    average = (qty * fill_price + float(existing['qty']) * float(existing['entry_price'])) / total_qty
                    db.execute('''UPDATE positions SET qty=?,entry_price=?,cost=?,highest=?,stop=?,target=?,opened_at=?
                                  WHERE exchange=? AND mode=? AND symbol=?''',
                               (total_qty, average, total_cost, max(average, fill_price), average * (1 - fraction),
                                average * (1 + 2.75 * fraction), now(), exchange, mode, symbol))
                else:
                    db.execute('''INSERT INTO positions(exchange,mode,symbol,qty,entry_price,cost,highest,stop,target,opened_at)
                                  VALUES (?,?,?,?,?,?,?,?,?,?)''',
                               (exchange, mode, symbol, qty, fill_price, cost + estimated, fill_price,
                                fill_price * (1 - fraction), fill_price * (1 + 2.75 * fraction), now()))
                db.execute('''INSERT INTO trades(created_at,exchange,mode,symbol,side,price,quantity,gross,fee,reason,order_id)
                              VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                           (now(), exchange, mode, symbol, 'BUY', fill_price, qty, gross,
                            fee, reason, order_id))
                if entry_candle is not None:
                    db.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)',
                               (self._key(f'last_entry.{mode}.{symbol}'), str(entry_candle)))
                self._complete_manual_request(db, completion, qty + (float(existing['qty']) if existing else 0))
                if intent:
                    storage.update_order_intent(intent, 'accounted', order_id=order_id, db=db)
            # Refreshing an account is a read; a failure cannot erase a filled ledger entry.
            if mode == 'live':
                with self.lock:
                    self.balance_updated_at = None
            storage.audit('trade', f'{mode.upper()} BUY {symbol}: {qty:.7g} at {fill_price:.7g}; cost {cost:.2f} USDT.')
            observability.event('ledger.accounted', intent_id=intent, decision_id=decision_id, mode=mode, symbol=symbol,
                                side='BUY', order_id=order_id, qty=qty, price=fill_price, cost=cost, fee=fee)
            self._observe_fills(mode)
            return True
        except Exception as exc:
            self._ledger_failure(f'{mode.upper()} BUY {symbol}: {type(exc).__name__}; portfolio ledger requires review',
                                 intent_id=intent)
            return False

    def _sell(self, position: dict, market_price: float, reason: str, *, completion=None,
              decision_id: str | None = None) -> bool:
        symbol, tracked, mode = position['symbol'], float(position['qty']), position['mode']
        if position.get('exchange', self.exchange_name) != self.exchange_name or mode != self.mode:
            raise ValueError('Position belongs to a different exchange or trading mode')
        if not finite_positive(market_price) or not finite_positive(tracked):
            return False
        fee_rate = self.cfg['fee_pct'] / 100
        intent = order_id = None
        kind = 'manual_exit' if completion is not None else 'exit'
        positions = self._active_positions(mode)
        if mode == 'paper':
            authorization = self._authorize(symbol, 'SELL', kind, tracked * market_price, market_price,
                                            positions=positions, cash=None, quantity=tracked, reason=reason,
                                            decision_id=decision_id)
            if not authorization.allowed:
                return False
            intent, order = self._submit(self.paper, symbol, 'SELL', quantity=tracked, authorization=authorization,
                                         intended_price=market_price, decision_id=decision_id)
            if order is None:
                return False
            try:
                filled, fill_price, gross, quote_fee, base_fee, fee, estimated = self._fill_details(order, symbol)
                consumed = filled + base_fee
                if consumed > tracked * (1 + 1e-10):
                    raise ValueError('Simulated fill would consume untracked holdings')
                order_id = self._order_id(order)
            except Exception as exc:
                self._ledger_failure(f'PAPER SELL {symbol}: {type(exc).__name__}; simulated fill accounting requires review',
                                     intent_id=intent)
                return False
        else:
            if self.live_halted or self._durable_live_block():
                self._halt_uncertain('Live exit blocked by unresolved order/account state')
                return False
            client = self._ensure_private()
            if getattr(client, 'has', {}).get('createMarketOrder') is False:
                raise storage.ConfigValidationError('This exchange adapter does not support market exits.')
            balance = self._read_balance(client)
            available = finite_positive(balance.get('free', {}).get(symbol.split('/')[0]))
            if not available:
                self.risk_block = f'No freely available tracked balance for {symbol}; manual review required'
                storage.audit('error', self.risk_block)
                return False
            if available < tracked * (1 - 1e-9):
                storage.audit('warning', f'{symbol}: free exchange balance {available:.8g} is below the tracked quantity {tracked:.8g}; only the available amount can be sold.')
            with self.private_lock:
                market = client.market(symbol)
                if not self._spot(symbol, market) or not self._spot(symbol, self._catalogue().get(symbol)):
                    raise storage.ConfigValidationError('Live exits require a verified active spot market.')
                requested = min(tracked, available)
                try:
                    amount = float(client.amount_to_precision(symbol, requested))
                except (ccxt.InvalidOrder, ValueError, TypeError):
                    amount = 0.0  # Below the smallest tradable step: retained below.
            limits = market.get('limits') or {}
            min_quantity = finite_positive((limits.get('amount') or {}).get('min'))
            min_cost = finite_positive((limits.get('cost') or {}).get('min'))
            if (not finite_positive(amount) or amount > requested * (1 + 1e-12)
                    or amount < min_quantity or amount * market_price < min_cost):
                self.risk_block = f'{symbol} remainder is below an exchange limit; retained in the ledger'
                storage.audit('warning', self.risk_block)
                return False
            authorization = self._authorize(symbol, 'SELL', kind, amount * market_price, market_price,
                                            positions=positions, cash=None, quantity=amount, reason=reason,
                                            decision_id=decision_id)
            if not authorization.allowed:
                return False
            intent, order = self._submit(client, symbol, 'SELL', quantity=amount, authorization=authorization,
                                         intended_price=market_price, decision_id=decision_id)
            if order is None:
                return False
            try:
                filled, fill_price, gross, quote_fee, base_fee, fee, estimated = self._fill_details(order, symbol)
                consumed = filled + base_fee
                if consumed > tracked * (1 + 1e-10) or filled > amount * (1 + 1e-10):
                    raise ValueError('Fill or base fee would consume untracked holdings')
                order_id = self._order_id(order)
            except Exception as exc:
                self._ledger_failure(f'SELL {symbol}: {type(exc).__name__}; fill/fee accounting requires review',
                                     intent_id=intent, mode='live')
                return False
        ratio = min(1.0, consumed / tracked)
        basis = float(position['cost']) * ratio
        # Cash receives the exact proceeds; P/L also bears a third-asset fee estimate.
        proceeds, pnl = gross - quote_fee, gross - quote_fee - estimated - basis
        remaining = max(0.0, tracked - consumed)
        if remaining <= tracked * 1e-12:
            remaining = 0.0
        try:
            with storage.connection() as db:
                if mode == 'paper':
                    key = self._key('paper_cash')
                    cash = float(db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()['value'])
                    db.execute('UPDATE meta SET value=? WHERE key=?', (str(cash + proceeds), key))
                if remaining <= tracked * 1e-12:
                    db.execute('DELETE FROM positions WHERE exchange=? AND mode=? AND symbol=?',
                               (self.exchange_name, mode, symbol))
                else:
                    db.execute('UPDATE positions SET qty=?,cost=? WHERE exchange=? AND mode=? AND symbol=?',
                               (remaining, max(0.0, float(position['cost']) - basis), self.exchange_name, mode, symbol))
                db.execute('''INSERT INTO trades(created_at,exchange,mode,symbol,side,price,quantity,gross,fee,realized,reason,order_id)
                              VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                           (now(), self.exchange_name, mode, symbol, 'SELL', fill_price, filled,
                            gross, fee, pnl, reason, order_id))
                primary_tf = HORIZONS[self.cfg['time_horizon']][0]
                duration = TIMEFRAME_SECONDS[primary_tf] * 1000
                candle = (int(time.time() * 1000) // duration - 1) * duration
                db.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)',
                           (self._key(f'last_exit.{mode}.{symbol}.{primary_tf}'), str(candle)))
                self._complete_manual_request(db, completion, remaining)
                if intent:
                    storage.update_order_intent(intent, 'accounted', order_id=order_id, db=db)
            if mode == 'live':
                with self.lock:
                    self.balance_updated_at = None
            storage.audit('trade', f'{mode.upper()} SELL {symbol}: {filled:.7g} at {fill_price:.7g}; P/L {pnl:+.2f} USDT ({reason}).')
            observability.event('ledger.accounted', intent_id=intent, decision_id=decision_id, mode=mode, symbol=symbol,
                                side='SELL', order_id=order_id, qty=filled, price=fill_price, proceeds=proceeds, fee=fee,
                                realized=pnl, remaining=remaining, reason=reason)
            # Every booked exit is checked against the realised-loss limit at
            # once: a manual close runs with the worker stopped, so no later
            # valuation may see the crossing before a profit or the UTC day hides it.
            try:
                cash_after = (cash if mode == 'paper' else float(balance['free'].get('USDT', 0) or 0)) + proceeds
                self.risk.observe_realized(mode, self._equity(mode, cash_after, self._active_positions(mode)))
            except Exception as exc:
                storage.audit('warning', f'Realised-loss check after the {symbol} exit failed ({type(exc).__name__}); '
                                         'it runs again at the next valuation.')
            self._observe_fills(mode)
            # Reentry cooldown and fill were committed together above.
            return True
        except Exception as exc:
            self._ledger_failure(f'{mode.upper()} SELL {symbol}: {type(exc).__name__}; portfolio ledger requires review',
                                 intent_id=intent)
            return False

    def _observe_fills(self, mode: str) -> None:
        """Judge the slippage breaker as soon as a fill is booked.

        Its hold then starts, and is reported, when the bad fill lands rather
        than at the next entry attempt. A failure here never undoes the booking;
        the next entry authorization checks the breaker again.
        """
        try:
            self.risk.observe_fills(mode)
        except Exception as exc:
            storage.audit('warning', f'Slippage check after a fill failed ({type(exc).__name__}); '
                                     'the next entry checks it again.')

    def _protect_positions(self, symbol: str, price: float):
        """Stored stop/target check at an observed bid; no trailing update here."""
        for position in self._active_positions(self.mode):
            if position['symbol'] == symbol:
                verdict = decision.evaluate_exit(position, price, dust=self._is_dust(
                    position, self._catalogue().get(symbol), price))
                if verdict['action'] == 'exit':
                    exit_id = observability.new_id('exit')
                    observability.event('decision.exit', decision_id=exit_id, symbol=symbol, reason=verdict['reason'],
                                        price=price, stop=float(position['stop']), target=float(position['target']),
                                        highest=float(position['highest']), source='protective',
                                        trigger=verdict['trigger'])
                    self._sell(position, price, verdict['reason'], decision_id=exit_id)

    def _mark_positions(self, symbol: str, price: float, atr: float, trend_broken: bool, *,
                        stored_checked_at: float | None = None, source: str = 'scan'):
        """Ratchet and persist the trailing levels, then exit on the trailing stop or a trend break.

        ``stored_checked_at`` is the bid :meth:`_protect_positions` already
        judged in this pass. A stored stop or target at that same bid was
        tried there, so it is not submitted a second time in the same tick
        (a rejected exit would otherwise produce two orders per poll).
        """
        for position in self._active_positions(self.mode):
            if position['symbol'] != symbol:
                continue
            verdict = decision.evaluate_exit(position, price, atr=atr if finite_positive(atr) else None,
                                             trend_broken=trend_broken,
                                             dust=self._is_dust(position, self._catalogue().get(symbol), price))
            highest, stop = verdict['highest'], verdict['stop']
            # An unchanged level needs no durable (fsync) write on every poll.
            if highest != float(position['highest']) or stop != float(position['stop']):
                try:
                    with storage.connection() as db:
                        db.execute('UPDATE positions SET highest=?,stop=? WHERE exchange=? AND mode=? AND symbol=?',
                                   (highest, stop, self.exchange_name, self.mode, symbol))
                except Exception as exc:
                    self._ledger_failure(f'Trailing stop persistence failed: {type(exc).__name__}')
                    return
            already_tried = verdict['trigger'] == 'stored' and stored_checked_at is not None and price == stored_checked_at
            if verdict['action'] == 'exit' and not already_tried and self._orders_allowed():
                exit_id = observability.new_id('exit')
                observability.event('decision.exit', decision_id=exit_id, symbol=symbol, reason=verdict['reason'],
                                    price=price, stop=stop, target=float(position['target']), highest=highest, source=source,
                                    trigger=verdict['trigger'])
                self._sell(position, price, verdict['reason'], decision_id=exit_id)

    def scan_once(self, allow_orders: bool = False, exited_symbols=None):
        # Acquiring this while holding the state lock makes config/start checks
        # atomic with a scan. The state lock is released before every network read.
        with self.lock:
            if not allow_orders:
                if self.control_busy:
                    raise storage.ConfigValidationError('An account operation is in progress.')
                if self.running or (self.thread and self.thread.is_alive()):
                    raise storage.ConfigValidationError('Automatic scanning is already active.')
            acquired = self.scan_lock.acquire(blocking=False)
            if not acquired and not allow_orders:
                raise storage.ConfigValidationError('Wait for the active chart, backtest or order operation to finish.')
        if not acquired:
            # A worker queues behind a chart/backtest rather than losing an
            # entire risk-management scan until its next polling interval.
            while not self.stop_event.is_set():
                if self.scan_lock.acquire(timeout=.25):
                    acquired = True
                    break
            if not acquired:
                return []
        with self.lock:
            self.last_scan, self.last_error = now(), None
        assessments, candidates = [], []
        market_state = {}
        # Exits the worker's last protective poll performed stay excluded here.
        exited_symbols = set(exited_symbols or ())
        successful_assessments = 0
        try:
            horizon = self.cfg['time_horizon']
            primary_tf, higher_tf = HORIZONS[horizon]
            if allow_orders:
                exited_symbols.update(self._monitor_if_due())
            try:
                symbols = self._universe()
            except Exception as exc:
                # A catalogue/discovery outage must not suppress already-known
                # held positions whose stored protective levels can be checked.
                held = [p['symbol'] for p in self._active_positions(self.mode)]
                symbols = list(dict.fromkeys(held + self.cfg['watchlist']))
                storage.audit('warning', f'Market catalogue refresh unavailable: {type(exc).__name__}.')
            current = self.feed
            if current is not None:
                current.set_symbols(self._feed_symbols(symbols))
            try:
                tickers = self.data.tickers(symbols)
            except Exception:
                tickers = {}
            catalogue = self._catalogue()
            tradable = [symbol for symbol in symbols if self._spot(symbol, catalogue.get(symbol))]
            wanted = [(symbol, primary_tf, 175) for symbol in tradable]
            wanted += [(symbol, higher_tf, 85) for symbol in tradable]
            if self.cfg.get('market_filters', True) and self._spot(MARKET_SYMBOL, catalogue.get(MARKET_SYMBOL)):
                wanted.append((MARKET_SYMBOL, higher_tf, 85))
            # Background downloads overlap round trips; the loop below still
            # runs each symbol's protective checks before using its candles.
            self.data.prefetch(wanted)
            for symbol in symbols:
                if allow_orders and self.stop_event.is_set():
                    break
                if allow_orders:
                    exited_symbols.update(self._monitor_if_due())
                if not self._spot(symbol, catalogue.get(symbol)):
                    assessments.append({'symbol': symbol, 'score': 0, 'price': 0,
                                        'status': 'Pair unavailable, inactive, or not an allowed spot market'})
                    continue
                try:
                    ticker = self._quote(symbol, tickers.get(symbol), need_volume=True)
                    try:
                        mark, volume, spread = self._observe_ticker(symbol, ticker or self.data.ticker(symbol))
                    except ValueError:
                        if not ticker:
                            raise
                        # A bulk snapshot can age while earlier pairs are read.
                        mark, volume, spread = self._observe_ticker(symbol, self.data.ticker(symbol))
                    price = self.last_asks.get(symbol, mark)
                    # These exits deliberately precede *all* candle downloads.
                    held_before = any(p['symbol'] == symbol for p in self._active_positions(self.mode))
                    protected_at = None
                    if allow_orders and self._orders_allowed():
                        protected_at = self.last_prices[symbol]
                        self._protect_positions(symbol, protected_at)
                    primary = self.data.candles(symbol, primary_tf, limit=175)
                    if allow_orders:
                        exited_symbols.update(self._monitor_if_due())
                    if len(primary) < 110:
                        raise ValueError('Insufficient completed primary candles')
                    last = primary.iloc[-1]
                    atr = finite_positive(last['atr'])
                    if allow_orders and self._orders_allowed():
                        self._mark_positions(symbol, self.last_prices[symbol], atr, bool(last['close'] < last['ema50']),
                                             stored_checked_at=protected_at)
                    if held_before and not any(p['symbol'] == symbol for p in self._active_positions(self.mode)):
                        exited_symbols.add(symbol)
                    # HTF data gates entries only. Stops and trend exits above
                    # remain independent of a higher-timeframe API failure.
                    filters = self.cfg.get('market_filters', True)
                    higher_error = higher_candle_ts = None
                    regime_ok, market_ok = False, None
                    try:
                        higher = self.data.candles(symbol, higher_tf, limit=85)
                        regime_ok = regime_signal(higher)
                        higher_candle_ts = int(higher.iloc[-1]['timestamp']) if len(higher) else None
                        market_ok = (self._market_regime(higher_tf, market_state)
                                     if filters and symbol != MARKET_SYMBOL else None)
                    except Exception as exc:
                        higher_error = type(exc).__name__
                    # One pure, replayable decision per symbol; the record keeps
                    # every check, the data timestamps and the abstention reason.
                    decided_at = time.time()
                    with self.lock:
                        quote_ts = self.last_price_times.get(symbol)
                    held_now = any(p['symbol'] == symbol for p in self._active_positions(self.mode))
                    verdict = decision.evaluate_entry(
                        symbol=symbol, last=last, regime_ok=regime_ok, market_ok=market_ok, rapid=horizon == 'rapid',
                        filters=filters, price=price, volume=volume, spread_pct=spread, quote_age_s=self._price_age(symbol),
                        quote_ts=quote_ts, cfg=self.cfg, now=decided_at, held=held_now,
                        exited_this_candle=(symbol in exited_symbols
                                            or self._same_candle_blocked(symbol, primary_tf, int(last['timestamp']))),
                        higher_candle_ts=higher_candle_ts,
                        higher_error=higher_error, signal_fn=entry_signal, price_ok_fn=entry_price_ok,
                        quality_fn=setup_quality)
                    self.decisions.add(verdict)
                    self.metrics.incr(f'decision.{verdict.action}')
                    if quote_ts is not None:
                        self.metrics.observe('decision.quote_to_decision_ms', max(0.0, (decided_at - quote_ts) * 1000))
                    # Every entry and every change of a symbol's action, status or
                    # reasons is written in full; an unchanged repeat is only
                    # counted (the in-memory decision log keeps every record), so a
                    # fast scan cannot rotate the order trail out of the event log.
                    trail_key = (self.exchange_name, self.mode, symbol)
                    summary = (verdict.action, verdict.status, verdict.reasons)
                    if verdict.action == 'enter' or self._entry_trail.get(trail_key) != summary:
                        self._entry_trail[trail_key] = summary
                        observability.event('decision.entry', decision_id=verdict.decision_id, symbol=symbol,
                                            action=verdict.action, status=verdict.status, score=verdict.score,
                                            confidence=verdict.confidence, reasons=list(verdict.reasons),
                                            signal_candle_ts_ms=verdict.inputs['signal_candle_ts_ms'],
                                            quote_age_s=verdict.inputs['quote_age_s'], record=verdict.as_dict())
                    else:
                        self.metrics.incr('decision.entry_unchanged')
                    reason, score, candidate = verdict.status, verdict.score, verdict.action == 'enter'
                    signal_close = float(last['close'])
                    if candidate:
                        # Every candidate passed all checks; rank by setup quality, then turnover.
                        candidates.append((verdict.proposal['quality'], volume, symbol, price, atr, int(last['timestamp']),
                                           signal_close, verdict.decision_id))
                    rsi = float(last['rsi'])
                    assessments.append({'symbol': symbol, 'price': price, 'bid': self.last_prices[symbol],
                                        'ask': self.last_asks.get(symbol), 'price_basis': 'ask' if symbol in self.last_asks else 'bid',
                                        'score': score, 'volume': volume,
                                        'spread': round(spread, 3) if spread is not None else None,
                                        'rsi': round(rsi, 1) if math.isfinite(rsi) else None,
                                        'trend_efficiency': (round(float(last['efficiency']), 2)
                                                             if math.isfinite(float(last.get('efficiency', math.nan))) else None),
                                        'market': ('unavailable' if 'error' in market_state else 'uptrend'
                                                   if market_state.get('ok') else 'weak' if market_state else None),
                                        'atr_pct': round(atr / price * 100, 2),
                                        'regime': 'uptrend' if regime_ok else 'weak',
                                        'kind': 'MEME' if is_meme(symbol) else 'CORE' if symbol.split('/')[0] in ('BTC', 'ETH') else 'ALT',
                                        'status': reason, 'candidate': candidate, 'timeframe': primary_tf,
                                        'price_age_seconds': self._price_age(symbol),
                                        'decision_id': verdict.decision_id, 'decision': verdict.action,
                                        'confidence': verdict.confidence, 'reasons': list(verdict.reasons)})
                    successful_assessments += 1
                except Exception as exc:
                    with self.lock:
                        fallback = self.last_prices.get(symbol, 0)
                    assessments.append({'symbol': symbol, 'score': 0, 'price': fallback,
                                        'status': f'Scan unavailable: {type(exc).__name__}',
                                        'price_age_seconds': self._price_age(symbol)})
            with self.lock:
                self.market_rows = assessments
                if successful_assessments:
                    self.last_successful_scan = now()
            # This guard is evaluated even when the strategy found zero setups.
            if allow_orders and self._orders_allowed():
                exited_symbols.update(self._monitor_if_due())
                self._enter_candidates(sorted((c for c in candidates if c[2] not in exited_symbols), reverse=True))
            else:
                if allow_orders:   # a Stop landed during the scan
                    self._withhold(sorted(candidates, reverse=True), 'orders_not_allowed')
                if self.mode == 'paper':
                    positions = self._active_positions('paper')
                    if self._marks_fresh(positions):
                        self._daily_allowed('paper', self._equity('paper', self._cash('paper'), positions))
            return assessments
        except Exception as exc:
            with self.lock:
                self.last_error = f'{type(exc).__name__}: market/account data request failed'
            storage.audit('error', 'Market scan: ' + self.last_error)
            return assessments
        finally:
            self.scan_lock.release()

    def _market_regime(self, higher_tf: str, state: dict) -> bool:
        """BTC's confirmation-timeframe trend, read once per scan.

        Altcoin breakouts rarely hold against a falling BTC. Missing or
        failing BTC data blocks altcoin entries; exits never depend on it.
        """
        if 'ok' not in state:
            state['ok'] = False
            try:
                if not self._spot(MARKET_SYMBOL, self._catalogue().get(MARKET_SYMBOL)):
                    raise ValueError('BTC/USDT is not an active spot market')
                state['ok'] = regime_signal(self.data.candles(MARKET_SYMBOL, higher_tf, limit=85))
            except Exception as exc:
                state['error'] = type(exc).__name__
        return state['ok']

    def _portfolio_risk(self, positions: list[dict]) -> float:
        proceeds_factor = (1 - self.cfg['fee_pct'] / 100) * (1 - self.cfg['slippage_pct'] / 100)
        with self.lock:
            return sum(max(0.0, max(float(p['cost']), float(p['qty']) * self.last_prices.get(p['symbol'], p['entry_price']))
                           - float(p['qty']) * float(p['stop']) * proceeds_factor) for p in positions)

    def _entry_budget(self, symbol: str, positions: list[dict], cash: float,
                      equity: float, fraction: float) -> tuple[float, float, float]:
        """One budget calculation for both automated and explicit manual buys."""
        fee_rate, slip = self.cfg['fee_pct'] / 100, self.cfg['slippage_pct'] / 100
        loss_fraction = 1 - (1 - fraction) * (1 - slip) * (1 - fee_rate) / (1 + fee_rate)
        with self.lock:
            used = sum(p['qty'] * self.last_prices[p['symbol']] for p in positions)
            meme_used = sum(p['qty'] * self.last_prices[p['symbol']] for p in positions if is_meme(p['symbol']))
        available_risk = max(0.0, equity * self.cfg['max_portfolio_risk_pct'] / 100 - self._portfolio_risk(positions))
        budget = min(equity * self.cfg['risk_pct'] / 100 / loss_fraction,
                     available_risk / loss_fraction,
                     equity * self.cfg['max_position_pct'] / 100,
                     max(0.0, equity * self.cfg['max_exposure_pct'] / 100 - used), cash * .90)
        if is_meme(symbol):
            budget = min(budget, equity * self.cfg['max_meme_position_pct'] / 100,
                         max(0.0, equity * self.cfg['max_meme_exposure_pct'] / 100 - meme_used))
        return budget, available_risk, loss_fraction

    def manual_trade(self, symbol: str, side: str, quote_amount=None,
                     confirmation: str = '', request_id: str = '', expected_context: str = '') -> dict:
        """A stopped-worker, idempotent spot order through the normal ledger.

        Callers must reuse request_id after an HTTP timeout. A pending or failed
        request is never automatically repeated, even after application restart.
        """
        error = storage.ConfigValidationError
        if not isinstance(symbol, str) or not isinstance(side, str):
            raise error('Choose a USDT spot pair and a buy or sell action.')
        symbol, side = symbol.strip().upper(), side.strip().upper()
        if side not in ('BUY', 'SELL') or not symbol.endswith('/USDT'):
            raise error('Choose a USDT spot pair and a buy or sell action.')
        amount = finite_positive(quote_amount) if side == 'BUY' else None
        if side == 'BUY' and not amount:
            raise error('A positive USDT buy budget is required.')
        if side == 'SELL' and quote_amount is not None:
            raise error('Sell closes the tracked position; do not supply a buy budget.')
        if not request_id:
            request_id = str(uuid.uuid4())
        try:
            request_id = str(uuid.UUID(request_id))
        except (ValueError, TypeError, AttributeError):
            raise error('A valid order request identifier is required.') from None
        with self.lock:
            if self._busy():
                raise error('Stop the bot and wait for active operations before placing a manual trade.')
            if self.mode == 'live' and confirmation != f'PLACE LIVE {side}':
                raise error(f'Type PLACE LIVE {side} to confirm this real-money order.')
            if ((expected_context and expected_context != self.manual_order_context)
                    or (self.mode == 'live' and not expected_context)):
                raise error('The account, exchange, mode or settings changed. Review the current order details and confirm again.')
            scope = self._key(f'manual_request.{self.mode}.{request_id}')
            payload = {'symbol': symbol, 'side': side, 'quote_amount': amount,
                       'exchange': self.exchange_name, 'mode': self.mode}
            fingerprint = hashlib.sha256(json.dumps({**payload, 'context': expected_context}, sort_keys=True).encode('utf8')).hexdigest()
            saved = storage.get_meta(scope)
            if saved:
                record = json.loads(saved)
                if record.get('fingerprint') != fingerprint:
                    raise error('This order identifier was already used for a different request.')
                if record.get('status') == 'filled':
                    return dict(record['result'])
                raise error('This order request was already attempted. Review trade history and order recovery before creating a new request.')
            if self.mode == 'live' and side == 'BUY' and storage.settings_health()['review_required']:
                raise error('Review and explicitly save the recovered settings before adding live exposure.')
            if self.mode == 'live' and (self.live_halted or self._durable_live_block()):
                raise error('Live execution is halted; reconcile unresolved orders before placing another trade.')
            if not self.scan_lock.acquire(blocking=False):
                raise error('Wait for the active market operation to finish.')
            self.control_busy = self._manual_order_active = True
            self.stop_event.clear()
        record = {'fingerprint': fingerprint, 'status': 'pending', 'created_at': now()}
        try:
            # Durable deduplication precedes every exchange operation.
            storage.set_meta(scope, json.dumps(record))
            observability.event('manual_order.requested', level='warning' if self.mode == 'live' else 'info',
                                request_id=request_id, symbol=symbol, side=side, quote_amount=amount, mode=self.mode)
            catalogue = self._fetch_markets()
            if not self._spot(symbol, catalogue.get(symbol)):
                raise error('This pair is not an active supported USDT spot market.')
            positions = self._active_positions(self.mode)
            position = next((p for p in positions if p['symbol'] == symbol), None)
            if side == 'SELL' and position is None:
                raise error('No tracked position exists in this exchange and trading mode.')
            price, volume, spread = self._observe_ticker(symbol, self.data.ticker(symbol), for_buy=side == 'BUY')
            if side == 'BUY':
                if position and not self._is_dust(position, catalogue.get(symbol), price):
                    raise error('A position in this pair is already tracked; sell it before opening another.')
                if volume < self.cfg['min_quote_volume'] or spread is None or spread > self.cfg['max_spread_pct']:
                    raise error('Quote liquidity or bid-ask spread does not meet the configured limits.')
                # A stale asset must not disappear from the exposure calculation.
                for held in positions:
                    self._observe_ticker(held['symbol'], self.data.ticker(held['symbol']))
                if not self._marks_fresh(positions):
                    raise error('A held asset has no fresh price; new buys are blocked.')
                tradable = [p for p in positions if not self._is_dust(p, catalogue.get(p['symbol']), self.last_prices[p['symbol']])]
                if len(tradable) >= self.cfg['max_positions']:
                    raise error('The maximum number of open positions has been reached.')
                if any(self.last_prices[p['symbol']] <= p['stop'] or
                       self.last_prices[p['symbol']] >= p['target'] for p in tradable):
                    raise error('A tracked asset is awaiting a protective exit; close it before adding risk.')
                primary_tf = HORIZONS[self.cfg['time_horizon']][0]
                primary = self.data.candles(symbol, primary_tf, limit=175)
                if len(primary) < 110:
                    raise error('Insufficient completed history to size a protective stop.')
                atr = finite_positive(primary.iloc[-1]['atr'])
                if not atr:
                    raise error('Historical volatility is unavailable; a protective stop cannot be sized.')
                fraction = stop_fraction(atr, price, rapid=self.cfg['time_horizon'] == 'rapid')
                cash = self._cash(self.mode)
                equity = self._equity(self.mode, cash, positions)
                if not self._daily_allowed(self.mode, equity)[0]:
                    raise error('The daily loss limit is latched; new buys are blocked until the next UTC day.')
                cap, _, _ = self._entry_budget(symbol, positions, cash, equity, fraction)
                if amount < 12 or amount > cap + 1e-8:
                    raise error(f'Buy budget must be at least 12 USDT and within the current risk cap of {max(0, cap):.2f} USDT.')
                # Refresh after history/balance reads; stale sizing cannot submit.
                refreshed, volume, spread = self._observe_ticker(symbol, self.data.ticker(symbol), for_buy=True)
                if (not entry_price_ok(refreshed, price, atr, observed_price=price) or volume < self.cfg['min_quote_volume']
                        or spread is None or spread > self.cfg['max_spread_pct']):
                    raise error('The quote moved or liquidity changed while preparing the order; review and submit a new request.')
                if not self._marks_fresh(positions) or not self._orders_allowed():
                    raise error('Order preparation was stopped or portfolio prices became stale.')
                # Recompute using the final quote and current liquidation marks.
                fraction = stop_fraction(atr, refreshed, rapid=self.cfg['time_horizon'] == 'rapid')
                equity = self._equity(self.mode, cash, positions)
                cap, _, _ = self._entry_budget(symbol, positions, cash, equity, fraction)
                if amount > cap + 1e-8 or not self._daily_allowed(self.mode, equity)[0]:
                    raise error('Portfolio risk changed while preparing this order; review the budget before trying again.')
                completion = {'scope': scope, 'record': record, 'payload': payload, 'request_id': request_id}
                filled = self._buy(symbol, amount, refreshed, fraction, reason='Explicit manual spot buy',
                                   merge_dust=position is not None, completion=completion,
                                   entry_candle=int(primary.iloc[-1]['timestamp']), kind='manual_entry',
                                   spread=spread, decision_id=f'manual_{request_id}')
            else:
                if not self._orders_allowed():
                    raise error('Order preparation was stopped.')
                completion = {'scope': scope, 'record': record, 'payload': payload, 'request_id': request_id}
                filled = self._sell(position, price, 'Explicit manual close of tracked spot position', completion=completion,
                                    decision_id=f'manual_{request_id}')
            if not filled:
                if self.live_halted:
                    raise error('Order outcome requires manual reconciliation. Live trading is halted; do not retry this order.')
                if self.risk_block and 'rejected by the exchange:' in self.risk_block:
                    raise error(self.risk_block + '. No order was accepted; correct the account or order settings before creating a new request.')
                with self.lock:
                    last = self._last_authorization
                if (last is not None and not last.allowed and last.request.symbol == symbol
                        and last.request.side == side and time.monotonic() - last.issued_at <= risk.AUTHORIZATION_TTL_SECONDS):
                    raise error('Blocked by risk controls: ' + last.reason + '. No order was sent.')
                raise error('The order was not completed. Review risk status, exchange limits and trade history before trying again.')
            completed = json.loads(storage.get_meta(scope))
            if completed.get('status') != 'filled' or completed.get('fingerprint') != fingerprint:
                raise error('The fill result is unavailable. Review trade history before another order.')
            observability.event('manual_order.completed', request_id=request_id, symbol=symbol, side=side, mode=self.mode,
                                status=completed['result'].get('status') if isinstance(completed.get('result'), dict) else None)
            return dict(completed['result'])
        except Exception as exc:
            try:
                with storage.connection() as db:
                    saved = db.execute('SELECT value FROM meta WHERE key=?', (scope,)).fetchone()
                    completed = json.loads(saved['value']) if saved else {}
                    # Post-commit reporting/logging errors cannot turn a known
                    # fill into a failed request or permit another submission.
                    if completed.get('status') == 'filled' and completed.get('fingerprint') == fingerprint:
                        observability.event('manual_order.completed', request_id=request_id, symbol=symbol, side=side,
                                            mode=self.mode, status='filled',
                                            note=f'filled and booked; a later step raised {type(exc).__name__}')
                        return dict(completed['result'])
                    record['status'] = 'failed'
                    db.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)', (scope, json.dumps(record)))
            except Exception:
                pass
            observability.event('manual_order.failed', level='warning', request_id=request_id, symbol=symbol, side=side,
                                mode=self.mode, error=type(exc).__name__)
            raise
        finally:
            with self.lock:
                self._manual_order_active = self.control_busy = False
                self.scan_lock.release()

    def _withhold(self, candidates, reason: str) -> None:
        """Record why 'enter' decisions did not become orders (one event per candidate)."""
        for candidate in candidates:
            decision_id = candidate[7] if len(candidate) > 7 else None
            self.metrics.incr(f'decision.withheld.{reason}')
            observability.event('decision.withheld', decision_id=decision_id, symbol=candidate[2], reason=reason)

    def _enter_candidates(self, candidates):
        candidates = list(candidates)
        cursor = [0]
        try:
            self._enter_pass(candidates, cursor)
        except Exception:
            # Whatever ended the pass early, the rest still say why they were not traded.
            self._withhold(candidates[cursor[0]:], 'entry_pass_error')
            raise

    def _enter_pass(self, candidates, cursor):
        """Try each candidate in order; ``cursor[0]`` is the first one without an outcome yet."""
        if self.mode == 'live' and storage.settings_health()['review_required']:
            self.risk_block = 'Recovered settings require an explicit review and save; new live entries blocked'
            self._withhold(candidates, 'settings_review')
            return
        positions = self._active_positions(self.mode)
        if not self._marks_fresh(positions):
            with self.lock:
                self.risk_block = 'A held asset has a stale or missing price; new entries blocked'
            self._withhold(candidates, 'stale_marks')
            return
        with self.lock:
            catalogue = self._catalogue()
            tradable = [p for p in positions if not self._is_dust(p, catalogue.get(p['symbol']), self.last_prices[p['symbol']])]
            protective_pending = any(self.last_prices[p['symbol']] <= p['stop']
                                     or self.last_prices[p['symbol']] >= p['target'] for p in tradable)
            if protective_pending:
                self.risk_block = 'A held position is awaiting a protective exit; new entries blocked'
        if protective_pending:
            self._withhold(candidates, 'protective_exit_pending')
            return
        cash = self._cash(self.mode)
        equity = self._equity(self.mode, cash, positions)
        permitted, loss_today = self._daily_allowed(self.mode, equity)
        if not permitted:
            storage.audit('warning', f'Daily loss guard latched: {loss_today:.2f}%; protective exits remain active.')
            self._withhold(candidates, 'daily_loss_guard')
            return
        with self.lock:
            self.risk_block = None
        count = 0
        for index, candidate in enumerate(candidates):
            cursor[0] = index
            _score, _volume, symbol, observed_price, atr, candle = candidate[:6]
            signal_close = candidate[6] if len(candidate) > 6 else observed_price
            decision_id = candidate[7] if len(candidate) > 7 else None
            if count >= 2 or not self._orders_allowed():
                self._withhold(candidates[index:], 'per_scan_cap' if count >= 2 else 'orders_not_allowed')
                break
            positions = self._active_positions(self.mode)
            tradable = [p for p in positions if not self._is_dust(p, catalogue.get(p['symbol']), self.last_prices[p['symbol']])]
            if len(tradable) >= self.cfg['max_positions'] or any(p['symbol'] == symbol for p in positions):
                self._withhold([candidate], 'max_positions' if len(tradable) >= self.cfg['max_positions'] else 'held')
                continue
            if not self._marks_fresh(positions):
                # Held assets value the equity every candidate is sized from.
                with self.lock:
                    self.risk_block = 'A held asset has a stale or missing price; new entries blocked'
                self._withhold(candidates[index:], 'stale_marks')
                break
            if self._same_candle_blocked(symbol, HORIZONS[self.cfg['time_horizon']][0], candle):
                self._withhold([candidate], 'same_candle')
                continue
            # Re-read a candidate immediately before reserving funds. A scan of
            # many coins can otherwise place an entry using an old quote.
            try:
                price, volume, spread = self._observe_ticker(symbol, self.data.ticker(symbol), for_buy=True)
            except Exception:
                # A network error after retries is as unquotable as bad data,
                # and one unquotable candidate must not end the pass.
                self._withhold([candidate], 'requote_unavailable')
                continue
            if volume < self.cfg['min_quote_volume'] or spread is None or spread > self.cfg['max_spread_pct']:
                self._withhold([candidate], 'requote_liquidity')
                continue
            if not entry_price_ok(price, signal_close, atr, observed_price=observed_price):
                self._withhold([candidate], 'requote_moved')
                continue
            cash = self._cash(self.mode)
            equity = self._equity(self.mode, cash, positions)
            fraction = stop_fraction(atr, price, rapid=self.cfg['time_horizon'] == 'rapid')
            budget, available_risk, loss_fraction = self._entry_budget(symbol, positions, cash, equity, fraction)
            if budget < 12:
                if available_risk < 12 * loss_fraction:
                    with self.lock:
                        self.risk_block = 'Aggregate protective-stop risk cap reached'
                self._withhold([candidate], 'budget')
                continue
            try:
                filled = self._buy(symbol, budget, price, fraction, entry_candle=candle, kind='entry',
                                   signal_close=signal_close, atr=atr, spread=spread, decision_id=decision_id)
            except Exception as exc:
                # The order path records its own outcome; anything escaping it is
                # unexpected. Paper continues with the next candidate, live stops
                # the pass so no further order is prepared in an unknown state.
                storage.audit('error', f'{symbol}: entry failed unexpectedly ({type(exc).__name__}); '
                                       + ('remaining candidates skipped this scan.' if self.mode == 'live'
                                          else 'continuing with the next candidate.'))
                observability.event('execution.entry_error', level='error', symbol=symbol,
                                    error=type(exc).__name__, decision_id=decision_id)
                self.risk.record_failure('entry_error')
                if self.mode == 'live':
                    self._withhold(candidates[index + 1:], 'entry_error')
                    break
                continue
            if filled:
                count += 1
            elif not (decision_id and storage.rows('SELECT 1 FROM order_journal WHERE decision_id=? LIMIT 1', (decision_id,))):
                # Nothing reached the journal: the gate refused it (its
                # risk.blocked event names the check) or it was never prepared.
                with self.lock:
                    last = self._last_authorization
                refused = (last is not None and not last.allowed and last.request.symbol == symbol
                           and last.request.decision_id == decision_id)
                self._withhold([candidate], 'risk_gate' if refused else 'not_submitted')
            if self.mode == 'live' and self.live_halted:
                self._withhold(candidates[index + 1:], 'live_halted')
                break
        cursor[0] = len(candidates)

    def _same_candle_blocked(self, symbol: str, primary_tf: str, candle) -> bool:
        """The durable no-reentry rule _enter_candidates applies: entered or exited on this candle."""
        return (storage.get_meta(self._key(f'last_entry.{self.mode}.{symbol}')) == str(candle)
                or self._exited_since(symbol, primary_tf, candle))

    def _exited_since(self, symbol: str, primary_tf: str, candle) -> bool:
        """True when a software exit happened on or after the signal candle."""
        saved = storage.get_meta(self._key(f'last_exit.{self.mode}.{symbol}.{primary_tf}'))
        if not saved:
            return False
        try:
            return int(candle) <= int(float(saved))
        except (TypeError, ValueError):
            return True  # An unreadable record blocks rather than permits reentry.

    def _acquire_analysis(self, *, research=False):
        with self.lock:
            if research and (self.running or (self.thread and self.thread.is_alive())):
                raise storage.ConfigValidationError('Stop the bot before downloading a research backtest so protective monitoring can continue.')
            if self.control_busy or not self.scan_lock.acquire(blocking=False):
                raise storage.ConfigValidationError('Wait for the active scan or account operation to finish.')

    def history(self, symbol: str, tf: str = '1h') -> list[dict]:
        if tf not in TIMEFRAME_SECONDS:
            raise storage.ConfigValidationError('Invalid chart timeframe.')
        with self.lock:
            active = self.running or (self.thread and self.thread.is_alive())
            if active:
                # Never wait for the public-client network lock from the UI.
                # Cached frames are immutable; MarketData replaces cache entries.
                saved = self.data.cache.get((symbol, tf, 180)) or self.data.cache.get((symbol, tf, 175))
                wall, monotonic = time.time(), time.monotonic()
                if (not saved or not saved[2] <= wall < saved[0]
                        or monotonic >= saved[3]):
                    raise storage.ConfigValidationError('Waiting for a cached chart from the active scan; stop the bot to download another timeframe.')
                frame = saved[1].copy()
        if active:
            return self._history_rows(frame)
        self._acquire_analysis(research=True)
        try:
            catalogue = self._fetch_markets()
            if not self._spot(symbol, catalogue.get(symbol)):
                raise storage.ConfigValidationError('Pair is not an allowed active spot market.')
            frame = self.data.candles(symbol, tf, limit=180)
            return self._history_rows(frame)
        finally:
            self.scan_lock.release()

    @staticmethod
    def _history_rows(frame) -> list[dict]:
        result = []
        for row in frame.to_dict('records'):
            point = {'t': int(row['timestamp'])}
            for key in ('open', 'high', 'low', 'close', 'ema20', 'ema50', 'volume'):
                value = float(row[key])
                point[key] = value if math.isfinite(value) else None
            result.append(point)
        return result

    def research_backtest(self, symbol: str, tf: str = '1h') -> dict:
        higher_tf = {'1m': '5m', '5m': '1h', '1h': '4h', '4h': '1d', '1d': '1w'}.get(tf)
        if higher_tf is None:
            raise storage.ConfigValidationError('Backtests support 1m, 5m, 1h, 4h, and 1d timeframes.')
        self._acquire_analysis(research=True)
        try:
            catalogue = self._fetch_markets()
            if not self._spot(symbol, catalogue.get(symbol)):
                raise storage.ConfigValidationError('Pair is not an allowed active spot market.')
            cutoff = int(time.time() * 1000)
            rows = self.data.read('fetch_ohlcv', symbol, tf, limit=900)
            higher = self.data.read('fetch_ohlcv', symbol, higher_tf, limit=900)
            # The live BTC gate is replayed from BTC's own confirmation candles.
            market = (self.data.read('fetch_ohlcv', MARKET_SYMBOL, higher_tf, limit=900)
                      if self.cfg.get('market_filters', True) and symbol != MARKET_SYMBOL else None)
            return research_report(rows, dict(self.cfg), timeframe=tf, higher_rows=higher,
                                   higher_timeframe=higher_tf, now_ms=cutoff, market_rows=market)
        finally:
            self.scan_lock.release()

    def snapshot(self) -> dict:
        """Only local state and cached balances. Polling never contacts an exchange."""
        current_feed = self.feed
        with self.lock:
            mode, exchange = self.mode, self.exchange_name
            positions = self._active_positions(mode)
            paper_cash = self._cash('paper')
            if mode == 'paper':
                cash = paper_cash
            elif self.latest_balance is not None and self.balance_updated_at is not None:
                cash = float(self.latest_balance['free'].get('USDT', 0))
            else:
                cash = None
            equity = self._equity(mode, cash, positions) if cash is not None else None
            liquidation_factor = (1 - self.cfg['slippage_pct'] / 100) * (1 - self.cfg['fee_pct'] / 100)
            detailed = []
            for position in positions:
                symbol = position['symbol']
                mark = self.last_prices.get(symbol)
                age = self._price_age(symbol)
                dust = bool(self._fresh(symbol) and self._is_dust(
                    position, self._catalogue().get(symbol), mark))
                detailed.append({**position, 'mark': mark, 'mark_stale': not self._fresh(symbol),
                                 'price_basis': 'bid', 'dust': dust, 'exit_untradable': dust,
                                 'price_age_seconds': round(age, 1) if age is not None else None,
                                 'unrealized': round(position['qty'] * mark * liquidation_factor - position['cost'], 2) if mark is not None else None})
            realized = storage.rows("SELECT COALESCE(SUM(realized),0) AS value FROM trades WHERE exchange=? AND mode=? AND side='SELL'",
                                    (exchange, mode))[0]['value']
            unresolved = storage.unresolved_orders()
            return {'running': self.running,
                    'control_busy': self.control_busy, 'manual_order_active': self._manual_order_active,
                    'manual_order_context': self.manual_order_context,
                    'settings_health': storage.settings_health(),
                    'stopping': bool(self.stopping or (self.running and self.stop_event.is_set())),
                    'mode': mode, 'exchange': exchange,
                    'connected': self.private is not None,
                    'credentials_stored': bool(self.api_key and self.api_secret),
                    'credential_source': self.credential_source,
                    'live_halted': self.live_halted, 'last_scan': self.last_scan,
                    'last_successful_scan': self.last_successful_scan, 'error': self.last_error,
                    'last_monitor': self.last_monitor, 'last_successful_monitor': self.last_successful_monitor,
                    'monitor_error': self.monitor_error,
                    'risk_block': self.risk_block, 'paper_cash': round(paper_cash, 2),
                    'cash': round(cash, 2) if cash is not None else None,
                    'equity': round(equity, 2) if equity is not None else None,
                    'valuation_price_basis': 'bid',
                    'valuation_stale': not self._marks_fresh(positions),
                    'realized': round(realized, 2), 'positions': detailed,
                    'markets': [dict(row) for row in self.market_rows], 'config': dict(self.cfg),
                    'balance_updated_at': self.balance_updated_at,
                    'price_age_seconds': {symbol: round(self._price_age(symbol), 1)
                                          for symbol in self.last_price_times},
                    'metrics': dict(self.data.metrics.copy()),
                    'risk': self.risk.status(),
                    'feed': current_feed.status() if current_feed is not None else {
                        'state': 'off', 'enabled': bool(self.cfg.get('streaming_quotes'))},
                    'unresolved_orders': len(unresolved), 'unresolved_probe': list(self.unresolved_probe),
                    'legacy_live_records': storage.legacy_live_records(),
                    'trades': storage.rows('SELECT * FROM trades WHERE exchange=? AND mode=? ORDER BY id DESC LIMIT 70',
                                           (exchange, mode)),
                    'audit': storage.rows('SELECT * FROM audit ORDER BY id DESC LIMIT 55')}
