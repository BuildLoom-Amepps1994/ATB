"""Durable local state, scoped by exchange. Exchange secrets never belong here."""
from __future__ import annotations

import json
import hashlib
import math
import os
import re
import sqlite3
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ['CRYPTOPILOT_DATA_DIR']).expanduser().resolve() if os.environ.get('CRYPTOPILOT_DATA_DIR') else Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.cryptopilot'))) / 'CryptoPilot'
DB_PATH = DATA_DIR / 'portfolio.sqlite3'
CONFIG_PATH = DATA_DIR / 'settings.json'
LOCK = threading.RLock()
_SECRETS: set[str] = set()
_SETTINGS_CACHE = None
EXCHANGES = ('mexc', 'kraken')

DEFAULTS = {
    'exchange': 'mexc',
    'watchlist': ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'DOGE/USDT', 'PEPE/USDT', 'SHIB/USDT', 'BONK/USDT', 'FLOKI/USDT'],
    'auto_discovery': False, 'discovered_pairs': 8, 'min_quote_volume': 5000000,
    'max_spread_pct': 0.85, 'time_horizon': 'balanced', 'scan_seconds': 180,
    'risk_pct': 0.65, 'max_position_pct': 8.0, 'max_exposure_pct': 40.0,
    'max_positions': 5, 'max_meme_position_pct': 3.0, 'max_meme_exposure_pct': 10.0,
    'daily_loss_limit_pct': 3.0, 'fee_pct': 0.1, 'slippage_pct': 0.2,
    'paper_start_usdt': 10000.0, 'max_portfolio_risk_pct': 3.0,
    'data_max_age_seconds': 120, 'protective_poll_seconds': 5,
    'market_filters': True,
    # Hard controls enforced by risk.RiskGate on every order path.
    'max_order_notional_usdt': 1000.0, 'max_asset_notional_usdt': 2500.0,
    'max_orders_per_minute': 6, 'daily_realized_loss_limit_pct': 3.0,
    'max_drawdown_pct': 10.0, 'streaming_quotes': False,
}
BOUNDS = {
    'discovered_pairs': (0, 12), 'min_quote_volume': (1000000, 1000000000),
    'max_spread_pct': (0.05, 2.0), 'scan_seconds': (10, 3600),
    'risk_pct': (0.1, 2.0), 'max_position_pct': (1.0, 20.0),
    'max_exposure_pct': (5.0, 60.0), 'max_positions': (1, 8),
    'max_meme_position_pct': (0.5, 10.0), 'max_meme_exposure_pct': (1.0, 25.0),
    'daily_loss_limit_pct': (0.5, 8.0), 'fee_pct': (0, 1.0),
    'slippage_pct': (0, 2.0), 'paper_start_usdt': (100, 100000000),
    'max_portfolio_risk_pct': (0.5, 8), 'data_max_age_seconds': (30, 600),
    'protective_poll_seconds': (2, 60),
    'max_order_notional_usdt': (12, 10000000), 'max_asset_notional_usdt': (12, 10000000),
    'max_orders_per_minute': (1, 60), 'daily_realized_loss_limit_pct': (0.5, 20.0),
    'max_drawdown_pct': (1.0, 50.0),
}
# Fields added after 1.3.0 whose default is the stricter choice. A saved
# profile from before they existed adopts the default without a review latch.
LATER_FIELDS = {'market_filters', 'max_order_notional_usdt', 'max_asset_notional_usdt',
                'max_orders_per_minute', 'daily_realized_loss_limit_pct', 'max_drawdown_pct',
                'streaming_quotes'}
INTEGER_FIELDS = {'discovered_pairs', 'scan_seconds', 'max_positions', 'data_max_age_seconds',
                  'protective_poll_seconds', 'max_orders_per_minute'}


class ConfigValidationError(ValueError):
    """Only messages constructed here are safe to show in the browser."""


def validate_settings(data: dict, base: dict | None = None, allow_paper_start: bool = False) -> dict:
    if not isinstance(data, dict):
        raise ConfigValidationError('Settings must be an object.')
    allowed = set(DEFAULTS) if allow_paper_start else set(DEFAULTS) - {'paper_start_usdt'}
    if set(data) - allowed:
        raise ConfigValidationError('Unknown or protected setting supplied.')
    cfg = {**DEFAULTS, **(base or {}), **data}
    if set(cfg) - set(DEFAULTS):
        raise ConfigValidationError('Unknown or protected setting supplied.')
    if cfg['exchange'] not in EXCHANGES:
        raise ConfigValidationError('Supported exchanges are MEXC and Kraken.')
    watchlist = cfg['watchlist']
    if not isinstance(watchlist, list) or not 1 <= len(watchlist) <= 20 or not all(isinstance(s, str) for s in watchlist):
        raise ConfigValidationError('Watchlist must contain 1 to 20 USDT spot pairs.')
    cfg['watchlist'] = list(dict.fromkeys(s.upper().strip() for s in watchlist))
    if not all(re.fullmatch(r'[A-Z0-9][A-Z0-9._-]{0,23}/USDT', s) for s in cfg['watchlist']):
        raise ConfigValidationError('Only USDT spot pairs such as BTC/USDT are supported.')
    for key, (lo, hi) in BOUNDS.items():
        value = cfg[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
            raise ConfigValidationError(f'{key} must be between {lo} and {hi}.')
        if key in INTEGER_FIELDS:
            if value != int(value):
                raise ConfigValidationError(f'{key} must be a whole number.')
            cfg[key] = int(value)
    if not isinstance(cfg['auto_discovery'], bool):
        raise ConfigValidationError('auto_discovery must be boolean.')
    if not isinstance(cfg['market_filters'], bool):
        raise ConfigValidationError('market_filters must be boolean.')
    if not isinstance(cfg['streaming_quotes'], bool):
        raise ConfigValidationError('streaming_quotes must be boolean.')
    if cfg['max_order_notional_usdt'] > cfg['max_asset_notional_usdt']:
        raise ConfigValidationError('The per-order notional cap cannot exceed the per-asset notional cap.')
    if cfg['time_horizon'] not in ('rapid', 'balanced', 'swing', 'position'):
        raise ConfigValidationError('Invalid strategy horizon.')
    if cfg['max_position_pct'] > cfg['max_exposure_pct']:
        raise ConfigValidationError('Position size cannot exceed total exposure.')
    if cfg['max_meme_position_pct'] > cfg['max_position_pct'] or cfg['max_meme_exposure_pct'] > cfg['max_exposure_pct']:
        raise ConfigValidationError('Meme coin caps cannot exceed portfolio caps.')
    if cfg['max_meme_position_pct'] > cfg['max_meme_exposure_pct']:
        raise ConfigValidationError('Meme coin position cap cannot exceed meme coin exposure.')
    return cfg


SETTINGS_REASONS = {
    'invalid_json', 'invalid_values', 'unreadable', 'missing',
    'save_in_progress', 'recovery_record_invalid', 'backup_unavailable',
}


def _settings_paths():
    return (CONFIG_PATH.with_name('settings.last-good.json'),
            CONFIG_PATH.with_name('settings.recovery.json'))


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('wb', dir=path.parent, prefix='settings-', suffix='.tmp', delete=False) as handle:
            temporary = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()


def _atomic_json(path: Path, value: dict) -> None:
    _atomic_bytes(path, json.dumps(value, indent=2, allow_nan=False).encode('utf8'))


def _read_settings_file(path: Path, *, require_complete: bool = False):
    """Return validated values, a safe error code, raw evidence and exchange."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, 'missing', None, None
    except OSError:
        return None, 'unreadable', None, None
    try:
        saved = json.loads(raw.decode('utf8'))
    except (ValueError, UnicodeError, RecursionError):
        return None, 'invalid_json', raw, None
    exchange = saved.get('exchange') if isinstance(saved, dict) else None
    if exchange not in EXCHANGES:
        exchange = None
    try:
        if not isinstance(saved, dict):
            raise ConfigValidationError('Invalid saved settings.')
        if require_complete and set(DEFAULTS) - LATER_FIELDS - set(saved):
            raise ConfigValidationError('Previously validated settings are incomplete.')
        safe = validate_settings({k: v for k, v in saved.items() if k in DEFAULTS}, allow_paper_start=True)
        return safe, None, raw, exchange
    except (ValueError, TypeError, OverflowError):
        return None, 'invalid_values', raw, exchange


def _read_recovery_record():
    _, path = _settings_paths()
    try:
        value = json.loads(path.read_text(encoding='utf8'))
        if not isinstance(value, dict) or not isinstance(value.get('reason'), str) or value['reason'] not in SETTINGS_REASONS:
            return {'reason': 'recovery_record_invalid'}
        evidence = value.get('evidence_file')
        return {'reason': value['reason'],
                'evidence_file': evidence if isinstance(evidence, str) and re.fullmatch(r'settings\.invalid-[0-9a-f]{16}\.json', evidence) else None}
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError, RecursionError):
        return {'reason': 'recovery_record_invalid'}


def _preserve_settings_evidence(raw: bytes | None) -> str | None:
    if raw is None:
        return None
    name = 'settings.invalid-' + hashlib.sha256(raw).hexdigest()[:16] + '.json'
    target = CONFIG_PATH.with_name(name)
    if not target.exists():
        _atomic_bytes(target, raw)
    return name


def _latch_settings_review(reason: str, raw: bytes | None = None):
    existing = _read_recovery_record()
    if existing is not None:
        return existing
    value = {'reason': reason, 'recorded_at': _now(), 'version': 1}
    try:
        value['evidence_file'] = _preserve_settings_evidence(raw)
        _atomic_json(_settings_paths()[1], value)
    except OSError:
        # The invalid/missing file itself continues to require review after
        # restart when a read-only disk prevents recording a separate latch.
        pass
    return value


def _settings_signature():
    result = []
    for path in (CONFIG_PATH, *_settings_paths()):
        try:
            stat = path.stat()
            result.append((str(path), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size))
        except OSError as exc:
            result.append((str(path), type(exc).__name__))
    result.append((str(DB_PATH), DB_PATH.exists()))
    return tuple(result)


def _copy_settings(cfg):
    return {**cfg, 'watchlist': list(cfg['watchlist'])}


def _settings_state():
    global _SETTINGS_CACHE
    signature = _settings_signature()
    if _SETTINGS_CACHE is not None and _SETTINGS_CACHE[0] == signature:
        return _copy_settings(_SETTINGS_CACHE[1]), dict(_SETTINGS_CACHE[2])
    backup_path, recovery_path = _settings_paths()
    backup, backup_fault, backup_raw, _ = _read_settings_file(backup_path)
    # Legacy partial configurations can be adopted once. After a last-good
    # copy was ever written, removed risk fields must not silently use defaults.
    current, fault, raw, exchange = _read_settings_file(CONFIG_PATH, require_complete=backup_fault != 'missing')
    record = _read_recovery_record()
    # Only a genuinely new profile may be initialized without explicit review.
    if (fault == 'missing' and not backup_path.exists() and not recovery_path.exists()
            and not DB_PATH.exists()):
        save_settings(DEFAULTS)
        current, fault, raw, exchange = _read_settings_file(CONFIG_PATH)
        backup, backup_fault, backup_raw, _ = _read_settings_file(backup_path)
        record = _read_recovery_record()
    if fault is not None:
        record = _latch_settings_review(fault, raw)
    elif backup_fault not in (None, 'missing'):
        # A damaged last-good copy is evidence of a disk problem. Keep it for
        # review instead of overwriting it with the only remaining copy.
        record = _latch_settings_review('backup_unavailable', backup_raw)
    elif record is None and backup is None:
        # Adopt a valid pre-upgrade configuration as the first last-good copy.
        try:
            _atomic_json(backup_path, current)
            backup = current
        except OSError:
            record = _latch_settings_review('backup_unavailable')
    selected = current or backup or validate_settings({'exchange': exchange} if exchange else {}, allow_paper_start=True)
    evidence = record.get('evidence_file') if record else None
    health = {
        'status': 'review_required' if record else 'ok', 'review_required': bool(record),
        'reason': record.get('reason') if record else None,
        'message': 'Settings recovery requires review. Review the displayed limits and save settings before enabling live trading.' if record else 'Settings are valid.',
        'using_last_known_good': bool(current is None and backup is not None),
        'backup_available': backup is not None,
        'evidence_available': bool(evidence and CONFIG_PATH.with_name(evidence).is_file()),
    }
    _SETTINGS_CACHE = (_settings_signature(), _copy_settings(selected), dict(health))
    return _copy_settings(selected), dict(health)


def settings() -> dict:
    with LOCK:
        return _settings_state()[0]


def settings_health() -> dict:
    """Safe, local-only health; a durable review latch clears only on save."""
    with LOCK:
        return _settings_state()[1]


def save_settings(cfg: dict) -> None:
    global _SETTINGS_CACHE
    safe = validate_settings(cfg, allow_paper_start=True)
    with LOCK:
        _, fault, raw, _ = _read_settings_file(CONFIG_PATH)
        # Preserve readable corrupt originals before an explicitly approved save.
        # Refuse to overwrite an unreadable original whose evidence cannot be kept.
        if fault == 'unreadable':
            raise OSError('Settings cannot be replaced until the original file can be preserved.')
        evidence = _preserve_settings_evidence(raw) if fault in ('invalid_json', 'invalid_values') else None
        backup_path, recovery_path = _settings_paths()
        prior = _read_recovery_record()
        marker = {'version': 1, 'reason': 'save_in_progress', 'recorded_at': _now(),
                  'evidence_file': evidence or (prior or {}).get('evidence_file')}
        # If either replacement fails or the process stops, a persistent latch
        # remains. A successful settings write alone never silently clears it.
        _atomic_json(recovery_path, marker)
        _SETTINGS_CACHE = None
        _atomic_json(CONFIG_PATH, safe)
        _atomic_json(backup_path, safe)
        recovery_path.unlink()
        _SETTINGS_CACHE = None


@contextmanager
def connection():
    with LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(DB_PATH), timeout=15)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        con.execute('PRAGMA synchronous=FULL')
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()


def scope_key(name: str, exchange: str) -> str:
    if exchange not in EXCHANGES:
        raise ValueError('Unsupported exchange scope.')
    return f'{name}.{exchange}'


def _create_positions(db):
    db.execute('''CREATE TABLE positions (
        exchange TEXT NOT NULL, mode TEXT NOT NULL, symbol TEXT NOT NULL, qty REAL NOT NULL,
        entry_price REAL NOT NULL, cost REAL NOT NULL, highest REAL NOT NULL,
        stop REAL NOT NULL, target REAL NOT NULL, opened_at TEXT NOT NULL,
        PRIMARY KEY (exchange,mode,symbol))''')


def init_db():
    # Preserve old rows; live records without an exchange/account identity are
    # quarantined until manually reconciled, never guessed from current settings.
    cfg = settings()
    exchange, paper_start = cfg['exchange'], cfg['paper_start_usdt']
    with connection() as db:
        db.execute('PRAGMA journal_mode=WAL')
        # SQLite DDL is transactional only inside an explicit transaction. A
        # crash between rename and copy must not strand the old position table.
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        prior = db.execute("SELECT value FROM meta WHERE key='migration.legacy_paper_exchange'").fetchone()
        legacy_identity_preexisting = bool(prior and prior['value'] in EXCHANGES)
        if prior and prior['value'] in EXCHANGES:
            exchange = prior['value']
        else:
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('migration.legacy_paper_exchange',?)", (exchange,))
        db.execute('''CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
            level TEXT NOT NULL, message TEXT NOT NULL)''')
        columns = [r['name'] for r in db.execute('PRAGMA table_info(positions)')]
        if not columns:
            _create_positions(db)
        elif 'exchange' not in columns:
            db.execute('ALTER TABLE positions RENAME TO positions_v1')
            _create_positions(db)
            db.execute('''INSERT INTO positions SELECT CASE WHEN mode='paper' THEN ? ELSE 'legacy-unassigned' END,
                mode,symbol,qty,entry_price,cost,highest,stop,target,opened_at FROM positions_v1''', (exchange,))
            db.execute('DROP TABLE positions_v1')
        db.execute('''CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
            exchange TEXT NOT NULL, mode TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
            price REAL NOT NULL, quantity REAL NOT NULL, gross REAL NOT NULL,
            fee REAL NOT NULL, realized REAL, reason TEXT, order_id TEXT)''')
        if 'exchange' not in [r['name'] for r in db.execute('PRAGMA table_info(trades)')]:
            db.execute("ALTER TABLE trades ADD COLUMN exchange TEXT NOT NULL DEFAULT 'legacy-unassigned'")
            db.execute("UPDATE trades SET exchange=? WHERE mode='paper'", (exchange,))
        db.execute('CREATE INDEX IF NOT EXISTS trades_scope_recent ON trades(exchange,mode,id DESC)')
        # Copy legacy metadata exactly once. Retain original rows as evidence,
        # but never replay them after a deliberate reset of the scoped account.
        migrated = db.execute("SELECT value FROM meta WHERE key='migration.legacy_paper_metadata_v2'").fetchone()
        if not migrated:
            # The older v2 implementation already recorded its original scope
            # in this same transaction. On upgrade, missing scoped rows may be
            # deliberate resets, so do not resurrect originals a final time.
            if not legacy_identity_preexisting:
                for row in db.execute('SELECT key,value FROM meta').fetchall():
                    key = row['key']
                    if key == 'paper_cash' or key.startswith(('last_entry.paper.', 'baseline.paper.', 'breached.paper.')):
                        if not key.endswith(tuple('.' + x for x in EXCHANGES)):
                            db.execute('INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)', (scope_key(key, exchange), row['value']))
            db.execute("INSERT INTO meta(key,value) VALUES ('migration.legacy_paper_metadata_v2','1')")
        for name in EXCHANGES:
            db.execute('INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)', (scope_key('paper_cash', name), str(paper_start)))
        db.execute('''CREATE TABLE IF NOT EXISTS order_journal (
            intent_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            exchange TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
            quantity REAL, cost REAL, status TEXT NOT NULL, order_id TEXT)''')
        db.execute('CREATE INDEX IF NOT EXISTS orders_unresolved ON order_journal(status,exchange)')
        # Order state machine fields (additive; old rows are live orders).
        journal_columns = {r['name'] for r in db.execute('PRAGMA table_info(order_journal)')}
        for column, definition in JOURNAL_COLUMNS:
            if column not in journal_columns:
                db.execute(f'ALTER TABLE order_journal ADD COLUMN {column} {definition}')
        db.execute('CREATE INDEX IF NOT EXISTS orders_recent ON order_journal(exchange,mode,created_at)')
        db.execute('PRAGMA user_version=3')


JOURNAL_COLUMNS = (
    ('mode', "TEXT NOT NULL DEFAULT 'live'"), ('client_order_id', 'TEXT'),
    ('intended_price', 'REAL'), ('filled_price', 'REAL'), ('filled_quantity', 'REAL'),
    ('decision_id', 'TEXT'), ('submitted_at', 'TEXT'), ('acked_at', 'TEXT'), ('final_at', 'TEXT'),
    ('note', 'TEXT'),
)


def get_meta(name: str, default: str = '') -> str:
    with connection() as db:
        row = db.execute('SELECT value FROM meta WHERE key=?', (name,)).fetchone()
        return row['value'] if row else default


def set_meta(name: str, value: object) -> None:
    with connection() as db:
        db.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)', (name, str(value)))


def rows(sql: str, args: tuple = ()) -> list[dict]:
    with connection() as db:
        return [dict(row) for row in db.execute(sql, args).fetchall()]


def register_secrets(*values: str) -> None:
    with LOCK:
        _SECRETS.update(value for value in values if isinstance(value, str) and value)


def redact(message: object) -> str:
    text = str(message)
    with LOCK:
        for value in sorted(_SECRETS, key=len, reverse=True):
            text = text.replace(value, '[REDACTED]')
    # Quotes and backslashes end the value, so a redaction applied to a serialized JSON
    # line never eats a closing quote or the escape in front of one.
    text = re.sub(r'(?i)((?:api[_-]?(?:key|secret)|signature|authorization|password|token)\s*[:=]\s*["\']?)[^\s,;}&"\'\\]+', r'\1[REDACTED]', text)
    return text


def audit(level: str, message: str):
    level, message = str(level)[:20], redact(message)[:500]
    with connection() as db:
        # A condition reported every poll (a stale quote, a retained remainder)
        # refreshes its latest entry instead of flushing the 300-row history.
        latest = db.execute('SELECT id,level,message FROM audit ORDER BY id DESC LIMIT 1').fetchone()
        if latest and latest['level'] == level and latest['message'] == message:
            db.execute('UPDATE audit SET created_at=? WHERE id=?', (_now(), latest['id']))
            return
        db.execute('INSERT INTO audit(created_at,level,message) VALUES (?,?,?)', (_now(), level, message))
        db.execute('DELETE FROM audit WHERE id NOT IN (SELECT id FROM audit ORDER BY id DESC LIMIT 300)')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def create_order_intent(exchange: str, symbol: str, side: str, quantity: float | None = None, cost: float | None = None, *,
                        mode: str = 'live', intended_price: float | None = None, decision_id: str | None = None) -> str:
    """Durable intent; its id doubles as the exchange client order id."""
    scope_key('', exchange)
    if side not in ('BUY', 'SELL'):
        raise ValueError('Invalid order side.')
    if mode not in ('paper', 'live'):
        raise ValueError('Invalid order mode.')
    if any(value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0) for value in (quantity, cost, intended_price)):
        raise ValueError('Order quantities, costs and prices must be finite and positive.')
    intent_id, timestamp = uuid.uuid4().hex, _now()
    with connection() as db:
        db.execute('INSERT INTO order_journal(intent_id,created_at,updated_at,exchange,symbol,side,quantity,cost,status,order_id,'
                   'mode,client_order_id,intended_price,decision_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (intent_id, timestamp, timestamp, exchange, symbol, side, quantity, cost, 'pending', None,
                    mode, client_order_id(intent_id), intended_price, decision_id))
    return intent_id


def client_order_id(intent_id: str) -> str:
    """Exchange-visible idempotency key derived from the intent (32 hex chars)."""
    return str(intent_id)[:32]


RESOLVED_ORDER_STATUSES = ('accounted', 'rejected', 'reconciled')
ORDER_STATUSES = ('pending', 'submitted', 'acked', 'uncertain', 'filled', *RESOLVED_ORDER_STATUSES)
# Every legal order-journal transition. Resolved states are terminal except
# that the recovery tool may still mark an exchange's rows reconciled.
ORDER_TRANSITIONS = {
    'pending': {'submitted', 'acked', 'rejected', 'uncertain'},
    'submitted': {'acked', 'filled', 'rejected', 'uncertain'},
    'acked': {'filled', 'uncertain', 'rejected'},
    'filled': {'accounted', 'uncertain'},
    'uncertain': {'reconciled', 'filled', 'accounted'},
    'accounted': set(), 'rejected': set(), 'reconciled': set(),
}
_JOURNAL_FIELDS = {'filled_price', 'filled_quantity', 'submitted_at', 'acked_at', 'final_at', 'note'}


def update_order_intent(intent_id: str, status: str, order_id: str | None = None, db=None, **fields) -> None:
    # 'reconciled' is only ever written by the offline recovery tool after a
    # person has compared the journal with the exchange's own order history.
    if status not in ORDER_STATUSES:
        raise ValueError('Invalid order journal status.')
    if set(fields) - _JOURNAL_FIELDS:
        raise ValueError('Unknown order journal field.')
    stamps = {'submitted': 'submitted_at', 'acked': 'acked_at', 'filled': 'final_at', 'rejected': 'final_at',
              'uncertain': 'final_at'}
    if status in stamps and stamps[status] not in fields:
        fields[stamps[status]] = _now()
    extra_sql = ''.join(f',{name}=?' for name in fields)

    def update(connection):
        current = connection.execute('SELECT status FROM order_journal WHERE intent_id=?', (intent_id,)).fetchone()
        if current is None:
            raise ValueError('Order intent does not exist.')
        if status != current['status'] and status not in ORDER_TRANSITIONS[current['status']]:
            raise ValueError(f'Illegal order state transition {current["status"]} -> {status}.')
        connection.execute(f'UPDATE order_journal SET status=?,updated_at=?,order_id=COALESCE(?,order_id){extra_sql} WHERE intent_id=?',
                           (status, _now(), order_id, *fields.values(), intent_id))
    if db is not None:
        update(db)
    else:
        with connection() as con:
            update(con)


def order_intent(intent_id: str) -> dict | None:
    found = rows('SELECT * FROM order_journal WHERE intent_id=?', (intent_id,))
    return found[0] if found else None


def unresolved_orders(exchange: str | None = None) -> list[dict]:
    """Live intents without a final accounted/rejected/reconciled state.

    Paper intents never block live activation: a simulated order has no
    exchange-side effect to reconcile.
    """
    sql = "SELECT * FROM order_journal WHERE status NOT IN ('accounted','rejected','reconciled') AND mode='live'"
    return rows(sql + (' AND exchange=?' if exchange else '') + ' ORDER BY created_at', (exchange,) if exchange else ())


def abandon_stale_paper_intents(exchange: str) -> int:
    """A paper intent left open by a crash is rejected at startup (no side effects)."""
    with connection() as db:
        cursor = db.execute("UPDATE order_journal SET status='rejected',updated_at=?,final_at=?,note='abandoned at startup' "
                            "WHERE exchange=? AND mode='paper' AND status NOT IN ('accounted','rejected','reconciled')",
                            (_now(), _now(), exchange))
        return cursor.rowcount


def legacy_live_records() -> int:
    return sum(rows("SELECT COUNT(*) AS count FROM " + table + " WHERE mode='live' AND exchange='legacy-unassigned'")[0]['count'] for table in ('positions', 'trades'))


def reset_paper(exchange: str):
    scope_key('', exchange)
    paper_start = settings()['paper_start_usdt']
    with connection() as db:
        db.execute("DELETE FROM positions WHERE mode='paper' AND exchange=?", (exchange,))
        db.execute("DELETE FROM trades WHERE mode='paper' AND exchange=?", (exchange,))
        db.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)', (scope_key('paper_cash', exchange), str(paper_start)))
        db.execute("DELETE FROM meta WHERE (key LIKE 'last_entry.paper.%' OR key LIKE 'last_exit.paper.%' OR key LIKE 'manual_request.paper.%' OR key LIKE 'baseline.paper.%' OR key LIKE 'breached.paper.%' OR key LIKE 'peak_equity.paper.%' OR key LIKE 'drawdown_stop.paper.%' OR key LIKE 'realized_stop.paper.%' OR key LIKE 'realized_stop_cleared.paper.%') AND key LIKE ?", ('%.' + exchange,))
        db.execute("DELETE FROM order_journal WHERE mode='paper' AND exchange=?", (exchange,))
