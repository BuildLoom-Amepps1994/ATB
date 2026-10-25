"""Loopback-only dashboard. Never return credentials or service exception text."""
from __future__ import annotations

import ipaddress
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr

import credentials
import storage
from market_data import DataUnavailable
from trader import TradingEngine

engine = TradingEngine()
CSRF = secrets.token_urlsafe(32)
# __file__ follows the editable installed module as well as the standalone bundle.
STATIC = Path(__file__).resolve().parent / 'static'


@asynccontextmanager
async def lifespan(application):
    yield
    engine.stop()


app = FastAPI(title='CryptoPilot Local', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount('/static', StaticFiles(directory=str(STATIC)), name='static')


def _origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
            return None
        if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            return None
        if any(c.isspace() for c in value) or '%' in parsed.netloc or any(c in value for c in ('\\', '?', '#')):
            return None
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == 'https' else 80)
        if not 1 <= port <= 65535:
            return None
        return parsed.scheme, parsed.hostname, port
    except ValueError:
        return None


def _security_headers(response, nonce: str):
    response.headers.update({
        'Cache-Control': 'no-store',
        'Pragma': 'no-cache',
        'X-Content-Type-Options': 'nosniff',
        'X-Frame-Options': 'DENY',
        'Referrer-Policy': 'no-referrer',
        'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
        'Cross-Origin-Resource-Policy': 'same-origin',
        'Content-Security-Policy': "default-src 'none'; script-src 'self' 'nonce-" + nonce + "'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'",
    })
    return response


@app.middleware('http')
async def local_security(request: Request, call_next):
    nonce = secrets.token_urlsafe(24)
    request.state.csp_nonce = nonce
    def reject(message):
        return _security_headers(JSONResponse({'error': message}, status_code=403), nonce)
    hosts = request.headers.getlist('host')
    if len(hosts) != 1:
        return reject('Local access only.')
    # Read the trusted ASGI scheme directly: building Request.url first can
    # raise on a malformed IPv6 Host before our validation gets to reject it.
    local_origin = _origin(request.scope.get('scheme', 'http') + '://' + hosts[0])
    if local_origin is None:
        return reject('Local access only.')
    try:
        if request.client is None or not ipaddress.ip_address(request.client.host).is_loopback:
            return reject('Local access only.')
    except ValueError:
        return reject('Local access only.')
    origins = request.headers.getlist('origin')
    if len(origins) > 1 or (origins and _origin(origins[0]) != local_origin):
        return reject('Cross-site action denied.')
    if request.headers.get('sec-fetch-site') == 'cross-site':
        return reject('Cross-site action denied.')
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        tokens = request.headers.getlist('x-cryptopilot-token')
        if len(tokens) != 1 or not secrets.compare_digest(tokens[0], CSRF):
            return reject('Invalid local action token.')
    try:
        response = await call_next(request)
    except Exception as exc:
        _record_failure(exc)
        response = JSONResponse({'detail': 'Local service request failed. Check local activity.'}, status_code=500)
    return _security_headers(response, nonce)


@app.exception_handler(RequestValidationError)
async def invalid_body(request: Request, exc: RequestValidationError):
    # Pydantic's default error includes submitted values, including API secrets.
    return JSONResponse({'detail': 'Invalid request data. Check the required fields and value types.'}, status_code=422)


@app.get('/', response_class=HTMLResponse)
def homepage(request: Request):
    doc = (STATIC / 'index.html').read_text(encoding='utf8')
    return HTMLResponse(doc.replace('__CRYPTOPILOT_CSRF__', json.dumps(CSRF)).replace('__CRYPTOPILOT_NONCE__', request.state.csp_nonce))


@app.get('/api/state')
def state():
    return ensure_ok(engine.snapshot)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Credentials(StrictModel):
    api_key: SecretStr = Field(min_length=8, max_length=250)
    api_secret: SecretStr = Field(min_length=8, max_length=250)
    remember: bool = False


class Mode(StrictModel):
    mode: str
    confirmation: str = ''
    eligible: bool = False


class ConfigData(StrictModel):
    settings: dict


class ChartQuery(StrictModel):
    symbol: str = Field(default='BTC/USDT', max_length=30)
    timeframe: str = Field(default='1h', max_length=3)


class ManualOrder(StrictModel):
    symbol: str = Field(min_length=3, max_length=30)
    side: str = Field(pattern='^(buy|sell)$')
    quote_amount: float | None = Field(default=None, gt=0, le=1_000_000, allow_inf_nan=False)
    confirmation: str = Field(default='', max_length=30)
    request_id: str = Field(min_length=36, max_length=36)
    expected_context: str = Field(min_length=36, max_length=36)


def _record_failure(exc):
    try:
        storage.audit('error', 'Local service request failed: ' + type(exc).__name__)
    except Exception:
        # A failed disk must not turn error reporting into a secret-bearing
        # traceback in the browser or server console.
        pass


def ensure_ok(action):
    try:
        return action()
    except HTTPException:
        raise
    except storage.ConfigValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except DataUnavailable:
        raise HTTPException(status_code=503, detail='Market data is temporarily unavailable. Try again after the cooldown.', headers={'Retry-After': '30'}) from None
    except credentials.CredentialStoreUnavailable:
        raise HTTPException(status_code=400, detail='Saved credential access or removal could not be verified. Check the operating-system credential manager.') from None
    except (ValueError, TypeError, KeyError):
        raise HTTPException(status_code=400, detail='Action unavailable. Check settings, stop the bot if necessary, and review local activity.') from None
    except Exception as exc:
        # Log the exception type only. Exchange text can contain signed requests.
        _record_failure(exc)
        raise HTTPException(status_code=502, detail='Exchange or local service request failed. Check local activity.') from None


@app.post('/api/connect')
def connect(req: Credentials):
    key, secret = req.api_key.get_secret_value().strip(), req.api_secret.get_secret_value().strip()
    storage.register_secrets(key, secret)
    return ensure_ok(lambda: (engine.connect(key, secret, req.remember), {'ok': True})[1])


@app.post('/api/disconnect')
def disconnect():
    return ensure_ok(lambda: (engine.disconnect(), {'ok': True})[1])


@app.post('/api/mode')
def change_mode(req: Mode):
    return ensure_ok(lambda: (engine.set_mode(req.mode, req.confirmation, req.eligible), {'mode': engine.mode})[1])


@app.post('/api/config')
def configure(req: ConfigData):
    return ensure_ok(lambda: (engine.update_config(req.settings), {'ok': True})[1])


@app.post('/api/start')
def start():
    return ensure_ok(lambda: (engine.start(), {'ok': True})[1])


@app.post('/api/stop')
def stop():
    return ensure_ok(lambda: (engine.stop(), {'ok': True})[1])


@app.post('/api/order')
def manual_order(req: ManualOrder):
    # A deliberate, individual action; never retry submissions at the HTTP layer.
    # The engine checks mode, live confirmation and portfolio limits atomically.
    def submit():
        try:
            UUID(req.request_id)
            UUID(req.expected_context)
        except ValueError:
            raise storage.ConfigValidationError('Invalid order request or trading context identifier.') from None
        return engine.manual_trade(req.symbol.upper(), req.side,
                                   quote_amount=req.quote_amount,
                                   confirmation=req.confirmation,
                                   request_id=req.request_id,
                                   expected_context=req.expected_context)
    return ensure_ok(submit)


@app.post('/api/reset-paper')
def reset_paper():
    return ensure_ok(lambda: (engine.reset_paper_portfolio(), {'ok': True})[1])


@app.get('/api/history')
def history(symbol: str = 'BTC/USDT', timeframe: str = '1h'):
    return ensure_ok(lambda: {'candles': engine.history(symbol.upper(), timeframe)})


@app.post('/api/scan')
def scan():
    if engine.running:
        raise HTTPException(status_code=400, detail='Automatic scanning is already active.')
    return ensure_ok(lambda: {'markets': engine.scan_once(allow_orders=False)})


@app.post('/api/backtest')
def run_backtest(req: ChartQuery):
    def work():
        if req.timeframe not in ('1m', '5m', '1h', '4h', '1d'):
            raise storage.ConfigValidationError('Backtest timeframe must be 1m, 5m, 1h, 4h or 1d.')
        return engine.research_backtest(req.symbol.upper(), req.timeframe)
    return ensure_ok(work)
