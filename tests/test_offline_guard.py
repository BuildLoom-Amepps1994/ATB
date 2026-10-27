"""The offline guard (scripts/offline_net.py, installed by conftest) refuses every way out.

Each case aims at 203.0.113.10 (TEST-NET-3, never routed) or a public name, so a
guard that let one through would fail the test without reaching a real service.
"""
import asyncio
import importlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import pytest

import offline_net          # registered by conftest before any test module is imported

REMOTE = ('203.0.113.10', 443)


@pytest.fixture
def fake_proxy():
    """A loopback listener that records whatever a client sends it, as a proxy would receive it."""
    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    server.listen()
    server.settimeout(0.2)
    received, stop = [], threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(1)
                try:
                    received.append(conn.recv(4096))
                except OSError:
                    pass
    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    yield f'http://127.0.0.1:{server.getsockname()[1]}', received
    stop.set()
    worker.join(timeout=2)
    server.close()


def test_no_proxy_variable_survives_the_session_setup():
    leftovers = {k: v for k, v in os.environ.items() if k.lower().endswith('_proxy') and k.lower() != 'no_proxy'}
    assert leftovers == {}
    assert os.environ.get('NO_PROXY') == '*' or os.environ.get('no_proxy') == '*'


def test_clear_proxies_removes_every_case_and_scheme():
    env = {'HTTPS_PROXY': 'http://127.0.0.1:3128', 'http_proxy': 'x', 'ALL_PROXY': 'x', 'wss_proxy': 'x',
           'NO_PROXY': 'internal', 'PATH': '/bin'}
    offline_net.clear_proxies(env)
    assert env == {'PATH': '/bin', 'NO_PROXY': '*', 'no_proxy': '*'}


@pytest.mark.parametrize('variable', ['HTTPS_PROXY', 'https_proxy', 'ALL_PROXY'])
def test_a_loopback_proxy_cannot_carry_urllib_to_a_remote_host(monkeypatch, fake_proxy, variable):
    url, received = fake_proxy
    monkeypatch.setenv(variable, url)
    with pytest.raises(urllib.error.URLError) as caught:
        urllib.request.urlopen('https://api.mexc.com/api/v3/ping', timeout=5)
    assert isinstance(caught.value.reason, offline_net.RemoteNetworkRefused)
    assert received == []                                       # nothing reached the proxy


def test_a_loopback_proxy_cannot_carry_requests_to_a_remote_host(monkeypatch, fake_proxy):
    requests = pytest.importorskip('requests')
    url, received = fake_proxy
    monkeypatch.setenv('HTTPS_PROXY', url)
    with pytest.raises(requests.exceptions.ConnectionError):
        requests.get('https://api.github.com/', timeout=5)
    assert received == []


def test_asyncio_cannot_connect_to_a_remote_address():
    async def attempt():
        await asyncio.wait_for(asyncio.open_connection(*REMOTE), timeout=5)
    with pytest.raises(offline_net.RemoteNetworkRefused):
        asyncio.run(attempt())


def test_name_and_address_lookups_are_refused_but_localhost_resolves():
    for lookup in (socket.gethostbyname, socket.gethostbyname_ex):
        with pytest.raises(offline_net.RemoteNetworkRefused):
            lookup('api.mexc.com')
    with pytest.raises(offline_net.RemoteNetworkRefused):
        socket.gethostbyaddr(REMOTE[0])
    with pytest.raises(offline_net.RemoteNetworkRefused):
        socket.getnameinfo(REMOTE, 0)                           # a reverse lookup, sent to the resolver

    async def reverse_lookup():
        return await asyncio.get_running_loop().getnameinfo(REMOTE)
    with pytest.raises(offline_net.RemoteNetworkRefused):
        asyncio.run(reverse_lookup())
    assert socket.getnameinfo(REMOTE, socket.NI_NUMERICHOST | socket.NI_NUMERICSERV) == ('203.0.113.10', '443')
    assert socket.gethostbyname('localhost').startswith('127.')


@pytest.mark.parametrize('name', ['ip6-localhost', 'localhost.localdomain', 'localhost.', '[localhost]',
                                  'localhost%lo'])
def test_a_local_looking_name_the_guard_does_not_answer_is_refused(name):
    # Most hosts files lack these, so the resolver would ask a DNS server for them.
    with pytest.raises(offline_net.RemoteNetworkRefused):
        socket.getaddrinfo(name, 443)
    with pytest.raises(offline_net.RemoteNetworkRefused):
        socket.gethostbyaddr(name)


def test_binding_to_a_remote_name_is_refused_and_localhost_binds_to_loopback():
    with socket.socket() as sock:
        with pytest.raises(offline_net.RemoteNetworkRefused):
            sock.bind(('api.mexc.com', 0))                      # bind resolves a name, so DNS would be asked
        with pytest.raises(offline_net.RemoteNetworkRefused):
            sock.bind(('203.0.113.10', 0))
        sock.bind(('localhost', 0))
        assert sock.getsockname()[0] == '127.0.0.1'


def test_a_reverse_lookup_answers_only_a_plain_local_literal():
    assert socket.gethostbyaddr(b'127.0.0.1') == ('localhost', [], ['127.0.0.1'])
    assert socket.gethostbyaddr('0:0:0:0:0:0:0:1') == ('localhost', [], ['::1'])
    for spelling in ('[::1]', '::1%lo', '[127.0.0.1]'):
        with pytest.raises(socket.gaierror):
            socket.gethostbyaddr(spelling)                     # the real function has no answer for these either
        with pytest.raises(socket.gaierror):
            socket.gethostbyname(spelling)


def test_local_lookups_are_answered_without_the_resolver():
    """In a fresh process whose resolver only records what it is asked, every local lookup is numeric or none."""
    code = r'''
import importlib.util, ipaddress, json, socket, sys
real_getaddrinfo, real_getnameinfo, asked = socket.getaddrinfo, socket.getnameinfo, []

def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    shown = host.decode('ascii', 'replace') if isinstance(host, bytes) else host
    asked.append(['getaddrinfo', shown, bool(flags & socket.AI_NUMERICHOST)])
    return real_getaddrinfo(host, port, family, type, proto, flags | socket.AI_NUMERICHOST)

def getnameinfo(sockaddr, flags):
    asked.append(['getnameinfo', sockaddr[0], bool(flags & socket.NI_NUMERICHOST)])
    return real_getnameinfo(sockaddr, flags | socket.NI_NUMERICHOST)

def literal(host):
    try:
        return bool(ipaddress.ip_address(host))
    except ValueError:
        return False

def method(name):                         # a socket method resolves a host name in C, so it must get a literal
    real = getattr(socket.socket, name)
    def call(self, *args):
        asked.append([name, args[-1][0], literal(args[-1][0])])
        return real(self, *args)
    setattr(socket.socket, name, call)

def resolver(function):
    def lookup(host):
        asked.append([function, host.decode('ascii', 'replace') if isinstance(host, bytes) else host, False])
        raise socket.herror(1, 'the resolver was asked')
    return lookup

socket.getaddrinfo, socket.getnameinfo = getaddrinfo, getnameinfo
for name in ('bind', 'connect', 'connect_ex', 'sendto', 'sendmsg'):
    if hasattr(socket.socket, name):          # no sendmsg on Windows
        method(name)
socket.gethostbyname, socket.gethostbyname_ex, socket.gethostbyaddr = (
    resolver(f) for f in ('gethostbyname', 'gethostbyname_ex', 'gethostbyaddr'))
spec = importlib.util.spec_from_file_location('offline_net', sys.argv[1])
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
guard.install()
serv = socket.NI_NUMERICSERV
with socket.socket() as server:
    server.bind(('localhost', 0))
    server.listen()
    bound, port = server.getsockname()
    with socket.socket() as client:
        client.connect(('LOCALHOST', port))
    with socket.socket() as client:
        connected = client.connect_ex(('localhost', port))
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as datagram:
    datagram.sendto(b'local', ('localhost', 9))
    if hasattr(datagram, 'sendmsg'):
        datagram.sendmsg([b'local'], [], 0, ('localhost', 9))
literals = [socket.getaddrinfo(host, 443, 0, socket.SOCK_STREAM)[0][4][0] for host in ('127.0.0.1', '::1')]
for spelling in ('[127.0.0.1]', '127.0.0.2%lo'):        # C would resolve these: they must not reach it
    with socket.socket() as sock:
        try:
            sock.connect_ex((spelling, 1))
        except socket.gaierror:
            pass

def unanswered(lookup, *args):
    try:
        lookup(*args)
    except socket.gaierror as error:
        return ['gaierror', error.errno]
    except OSError as error:
        return [type(error).__name__]
    return ['answered']

# glibc hands an empty name to the resolver, which can ask a DNS server for the root name.
empty = [unanswered(socket.getaddrinfo, '', 443), unanswered(socket.getaddrinfo, b'', 443),
         unanswered(socket.getaddrinfo, '', 443, 0, socket.SOCK_STREAM, 0, socket.AI_PASSIVE),
         unanswered(socket.gethostbyname_ex, ''), unanswered(socket.gethostbyname_ex, b'')]
print(json.dumps({'answers': {
    'bound': bound, 'connect_ex': connected, 'literals': literals,
    'ipv6': socket.getaddrinfo('localhost', 443, socket.AF_INET6, socket.SOCK_STREAM)[0][4][0],
    'ipv4': socket.getaddrinfo('LOCALHOST', 443, socket.AF_INET, socket.SOCK_STREAM)[0][4][0],
    'canonical': socket.getaddrinfo('localhost', 443, 0, socket.SOCK_STREAM, 0, socket.AI_CANONNAME)[0][3],
    'name': socket.gethostbyname('localhost'),
    'name_ex': socket.gethostbyname_ex('localhost'),
    'reverse': [socket.gethostbyaddr(a) for a in ('127.0.0.2', '::1', '0.0.0.0', 'localhost')],
    'nameinfo': [socket.getnameinfo(s, serv) for s in (('127.0.0.2', 443), ('::1', 443, 0, 0), ('0.0.0.0', 443))],
    'name_required': socket.getnameinfo(('127.0.0.1', 443), serv | socket.NI_NAMEREQD),
    'empty': empty,
}, 'asked': asked}))
'''
    guard = os.path.abspath(offline_net.__file__)
    done = subprocess.run([sys.executable, '-I', '-c', code, guard], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    assert report['answers'] == {
        'bound': '127.0.0.1', 'connect_ex': 0, 'literals': ['127.0.0.1', '::1'],
        'ipv6': '::1', 'ipv4': '127.0.0.1', 'canonical': 'localhost',
        'name': '127.0.0.1', 'name_ex': ['localhost', [], ['127.0.0.1']],
        'reverse': [['localhost', [], ['127.0.0.2']], ['localhost', [], ['::1']],
                    ['localhost', [], ['0.0.0.0']], ['localhost', [], ['127.0.0.1']]],
        'nameinfo': [['localhost', '443']] * 3,
        'name_required': ['localhost', '443'],
        'empty': [['gaierror', socket.EAI_NONAME]] * 5,
    }
    assert not [entry for entry in report['asked'] if entry[1] == ''], report['asked']
    assert report['asked'] and all(numeric for _, _, numeric in report['asked']), report['asked']
    called = [(function, host) for function, host, _ in report['asked']]
    assert {'bind', 'connect', 'connect_ex', 'sendto'} <= {function for function, _ in called}
    assert ('sendmsg' in {function for function, _ in called}) == hasattr(socket.socket, 'sendmsg')
    assert ('getaddrinfo', '127.0.0.1') in called and ('getaddrinfo', '::1') in called


def test_an_empty_host_gets_no_lookup_but_keeps_its_wildcard_meaning():
    # getaddrinfo and gethostbyname_ex would hand '' to the resolver; the C socket functions read it as the wildcard.
    for host in ('', b''):
        with pytest.raises(socket.gaierror) as refused:
            socket.getaddrinfo(host, 443)
        assert refused.value.errno == socket.EAI_NONAME
        with pytest.raises(socket.gaierror) as refused:
            socket.gethostbyname_ex(host)
        assert refused.value.errno == socket.EAI_NONAME
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo('', 443, 0, socket.SOCK_STREAM, 0, socket.AI_PASSIVE)
    assert socket.gethostbyname('') == '0.0.0.0'
    assert {entry[4][0] for entry in socket.getaddrinfo(None, 443, socket.AF_INET)} == {'127.0.0.1'}
    assert socket.getaddrinfo('127.0.0.1', 443, socket.AF_INET)[0][4][0] == '127.0.0.1'
    assert socket.gethostbyname_ex('127.0.0.1')[2] == ['127.0.0.1']
    with socket.socket() as sock:
        sock.bind(('', 0))
        assert sock.getsockname()[0] == '0.0.0.0'


def test_the_socket_methods_get_the_loopback_literal_of_their_family():
    assert offline_net._numeric(('localhost', 80), socket.AF_INET) == ('127.0.0.1', 80)
    assert offline_net._numeric(('LOCALHOST', 80, 0, 0), socket.AF_INET6) == ('::1', 80, 0, 0)
    assert offline_net._numeric(('127.0.0.2', 80), socket.AF_INET) == ('127.0.0.2', 80)
    assert offline_net._numeric('/tmp/socket', socket.AF_INET) == '/tmp/socket'


@pytest.mark.parametrize('spelling', ['[127.0.0.1]', '127.0.0.2%lo', '[::1]'])
def test_a_bracketed_or_zoned_loopback_spelling_is_not_handed_to_the_resolver(spelling):
    # The C socket functions would resolve a host they cannot parse, so the guard gives it no answer.
    with socket.socket() as sock:
        with pytest.raises(socket.gaierror):
            sock.connect_ex((spelling, 1))
        with pytest.raises(socket.gaierror):
            sock.bind((spelling, 0))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as datagram:
        with pytest.raises(socket.gaierror):
            datagram.sendto(b'x', (spelling, 9))


def test_a_bind_of_a_socket_that_is_not_ip_is_not_judged_by_host():
    family = getattr(socket, 'AF_NETLINK', None)
    if family is None:
        pytest.skip('no AF_NETLINK on this platform')
    try:
        sock = socket.socket(family, socket.SOCK_RAW, 0)          # NETLINK_ROUTE
    except OSError as exc:
        pytest.skip(f'netlink sockets unavailable: {exc}')
    with sock:
        sock.bind((0, 0))                                         # (pid, groups), not a host


@pytest.mark.parametrize('module', ['uvloop', 'winloop'])
def test_libuv_event_loops_cannot_be_imported(module):
    # Their loops resolve and connect in C, outside the socket functions the guard replaces.
    with pytest.raises(ImportError):
        importlib.import_module(module)


def test_uvicorns_default_loop_is_asyncio_under_the_guard():
    auto = pytest.importorskip('uvicorn.loops.auto')
    loop = auto.auto_loop_factory(use_subprocess=False)()
    try:
        assert type(loop).__module__.startswith('asyncio')
    finally:
        loop.close()


def test_active_says_whether_the_replacements_are_still_in_place(monkeypatch):
    assert offline_net.active()
    monkeypatch.setattr(socket, 'getnameinfo', lambda sockaddr, flags: ('remote', '443'))
    assert not offline_net.active()


@pytest.mark.parametrize('module', ['uvloop', 'winloop'])
def test_active_says_whether_the_libuv_loops_are_still_blocked(monkeypatch, module):
    assert offline_net.active()
    monkeypatch.delitem(sys.modules, module)                   # it could be imported again
    assert not offline_net.active()


def test_install_refuses_when_a_libuv_loop_is_already_imported(monkeypatch):
    spec = importlib.util.spec_from_file_location('offline_net_fresh', offline_net.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    monkeypatch.setitem(sys.modules, 'uvloop', type(sys)('uvloop'))
    with pytest.raises(RuntimeError, match='uvloop was imported before the offline guard'):
        fresh.install()                      # raises before it replaces anything


def test_a_datagram_to_a_remote_address_is_refused_and_one_to_loopback_is_sent():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver, \
         socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        receiver.bind(('127.0.0.1', 0))
        receiver.settimeout(2)
        with pytest.raises(offline_net.RemoteNetworkRefused):
            sender.sendto(b'query', ('203.0.113.10', 53))
        with pytest.raises(offline_net.RemoteNetworkRefused):
            sender.sendto(b'query', 0, ('203.0.113.10', 53))
        sender.sendto(b'local', receiver.getsockname())
        assert receiver.recv(16) == b'local'


def test_a_python_child_process_gets_the_same_guard():
    code = ('import socket\n'
            'try:\n'
            '    socket.create_connection(("203.0.113.10", 443), timeout=5)\n'
            'except ConnectionRefusedError as exc:\n'
            '    print(type(exc).__name__)\n')
    done = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == 'RemoteNetworkRefused'
