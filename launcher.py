"""Single-instance loopback launcher for source and the Windows console build."""
from __future__ import annotations

import json
import http.client
import os
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


def data_directory() -> Path:
    override = os.environ.get('CRYPTOPILOT_DATA_DIR')
    if override:
        return Path(override).expanduser().resolve()
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.cryptopilot'))) / 'CryptoPilot'


class InstanceLock:
    """OS lock released automatically on process exit, including a crash.

    Retain the lock file: deleting it can let processes lock different files at
    the same path. The PID is diagnostic; only the operating system lock counts.
    """
    def __init__(self, folder: Path):
        self.path = folder / 'instance.lock'
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = os.fdopen(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600), 'r+b')
        self.handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise AlreadyRunning('CryptoPilot is already running for this data folder. Use its existing console and dashboard.') from exc
        # Windows permits byte-range locks beyond EOF. Initialize only after
        # locking, so simultaneous first launches never write a rival's lock.
        self.handle.seek(0)
        self.handle.write(b'\0' + json.dumps({'pid': os.getpid()}).encode('utf8'))
        self.handle.truncate()
        self.handle.flush()
        return self

    def __exit__(self, *_):
        if self.handle is not None:
            self.handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def configured_port() -> int:
    try:
        port = int(os.environ.get('CRYPTOPILOT_PORT', '8765'))
    except ValueError as exc:
        raise ValueError('CRYPTOPILOT_PORT must be an integer from 1 to 65535.') from exc
    if not 1 <= port <= 65535:
        raise ValueError('CRYPTOPILOT_PORT must be an integer from 1 to 65535.')
    return port


def bind_loopback(port: int) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(('127.0.0.1', port))
        return listener
    except OSError:
        listener.close()
        raise


def open_when_ready(url: str, token: str, stop: threading.Event) -> None:
    # This readiness probe is HTTP-only. urllib's default opener constructs an
    # HTTPS handler even for loopback HTTP, making browser startup unnecessarily
    # depend on the packaged TLS trust store. HTTPConnection uses no proxies.
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1':
        print('Cannot open the dashboard automatically. Use the local dashboard address printed above.')
        return
    try:
        port = parsed.port or 80
    except ValueError:
        print('Cannot open the dashboard automatically. Use the local dashboard address printed above.')
        return
    deadline = time.monotonic() + 30
    while not stop.is_set() and time.monotonic() < deadline:
        connection = None
        try:
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
            connection.request('GET', parsed.path or '/')
            response = connection.getresponse()
            if response.status == 200 and token in response.read(1024 * 1024).decode('utf8', errors='replace'):
                try:
                    if webbrowser.open(url):
                        return
                except (OSError, webbrowser.Error):
                    pass
                print('Open the dashboard in your browser: ' + url)
                return
        except (OSError, http.client.HTTPException, ValueError):
            pass
        finally:
            if connection is not None:
                connection.close()
        stop.wait(.25)
    if not stop.is_set():
        print('The dashboard did not open automatically. Open it in your browser: ' + url)


def report_startup_failure(exc: BaseException) -> None:
    """Leave one usable line on the console and in logs/startup.log when start-up fails.

    The installed app runs in a console that closes with the process, so a bare
    traceback is lost. The console gets the failure class and the data folder;
    the redacted traceback goes to the data folder, never to the console.
    """
    folder = None
    try:
        folder = data_directory()
    except Exception:
        pass
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    line = f'{stamp} CryptoPilot could not start: {type(exc).__name__}'
    print(line + (f' (data folder: {folder}).' if folder else '.')
          + ' Check that the data folder is writable and the portfolio database is intact; '
            'the redacted traceback is in logs/startup.log inside the data folder.')
    if folder is None:
        return
    try:
        import traceback
        try:
            from storage import redact
        except Exception:
            def redact(text):
                return str(text)
        log = Path(folder) / 'logs' / 'startup.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        if log.exists() and log.stat().st_size > 1_000_000:
            log.unlink()
        with log.open('a', encoding='utf-8') as handle:
            handle.write(line + '\n' + redact(''.join(traceback.format_exception(exc))) + '\n')
    except Exception:
        pass


def begin_engine_stop(engine, announce=print) -> threading.Thread:
    """Stop scanning and new orders now, on a helper thread (never inside a signal handler)."""
    def run():
        announce('Stop requested: the bot stops scanning and placing orders now; open dashboard requests '
                 'get up to 30 s to finish, then in-flight exchange operations get up to 30 s.')
        engine.stop()
    thread = threading.Thread(target=run, daemon=True, name='StopOnExit')
    thread.start()
    return thread


def engine_first_server(server_class, engine, announce=print):
    """A uvicorn Server that stops the engine the moment an exit is requested.

    uvicorn first drains open connections (up to ``timeout_graceful_shutdown``)
    and only then runs the app's shutdown, which used to be the first place
    the engine was told to stop. Stopping here keeps the worker from starting
    another scan or entry during that drain. A second Ctrl+C still forces exit.
    """
    class EngineFirstServer(server_class):
        def handle_exit(self, sig, frame):
            first = not self.should_exit
            super().handle_exit(sig, frame)
            if first:
                begin_engine_stop(engine, announce)
    return EngineFirstServer


def main() -> int:
    listener = None
    stop_browser = threading.Event()
    try:
        port = configured_port()
        with InstanceLock(data_directory()):
            try:
                listener = bind_loopback(port)
            except OSError:
                print(f'Cannot start: local port {port} is already in use. Close the other application or set CRYPTOPILOT_PORT to an unused port.')
                return 1
            # Initialize application state only after owning the data folder and port.
            import uvicorn
            from app import CSRF, app, engine
            url = f'http://127.0.0.1:{port}/'
            print('=' * 58)
            print(' CryptoPilot Local - SPOT trading workstation')
            print(' Dashboard: ' + url)
            print(' Data: ' + str(data_directory()))
            print(' Keep this console open. Ctrl+C requests a safe stop.')
            print(' Default: PAPER trading. Live trading must be authorised.')
            print('=' * 58)
            if os.environ.get('CRYPTOPILOT_NO_BROWSER') != '1':
                threading.Thread(target=open_when_ready, args=(url, CSRF, stop_browser), daemon=True).start()
            # Nothing proxies this loopback server; forwarded-for headers from
            # a local client must not rewrite the peer address it is judged by.
            # The engine stops as soon as an exit is requested; the connection
            # drain that follows is bounded so a slow request cannot hold the
            # shutdown open, and the engine's own 30 s in-flight wait runs after
            # it in app.lifespan.
            config = uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning',
                                    access_log=False, proxy_headers=False, timeout_graceful_shutdown=30)
            server = engine_first_server(uvicorn.Server, engine)(config)

            def request_exit():
                if not server.should_exit:
                    begin_engine_stop(engine)
                server.should_exit = True
            app.state.request_exit = request_exit
            server.run(sockets=[listener])
            print('Stopped.')
        return 0
    except (AlreadyRunning, ValueError) as exc:
        print(str(exc))
        return 1
    except KeyboardInterrupt:
        # Ctrl+C before the server owns the signal handler is a normal exit.
        print('Stopped.')
        return 0
    except Exception as exc:
        report_startup_failure(exc)
        return 1
    finally:
        stop_browser.set()
        if listener is not None:
            listener.close()


if __name__ == '__main__':
    sys.exit(main())
