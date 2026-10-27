"""Refuse remote networking in this process: used by the offline tests and the fixture soak.

``install()`` replaces the socket entry points that can reach another host
(connect, connect_ex, sendto, sendmsg, the name and address lookups including
``getnameinfo``'s reverse lookup, and on
Windows the proactor connect and sendto that asyncio uses in place of
``socket.connect``) with versions that allow only loopback addresses and local
sockets. Anything else raises ``RemoteNetworkRefused`` before a packet leaves.

A lookup that stays local is answered here, not by the resolver: ``localhost``
is 127.0.0.1 (``::1`` when IPv6 is asked for), and a loopback or unspecified
address is named ``localhost``. The resolver reads the hosts file first and
asks a DNS server only for what that file lacks, which on many machines
includes ``localhost`` over IPv6 and every loopback address but 127.0.0.1.
Other names (``localhost.localdomain``, ``ip6-localhost``) are refused.

uvloop and winloop are blocked from import, so uvicorn's default loop is
asyncio's. Their libuv loops resolve and connect in C, where these checks do
not reach; ``install()`` refuses to run if one is already imported.

``clear_proxies()`` removes every ``*_proxy`` environment variable and sets
``NO_PROXY`` to ``*``. A proxy on loopback would otherwise carry a request to
any host: the guard sees only the local hop, and the remote name travels
inside the proxy's CONNECT line. ``*`` also stops urllib and requests from
reading a proxy out of the Windows registry.

Only the standard library is imported, so this can run before any
application module.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import sys


class RemoteNetworkRefused(ConnectionRefusedError):
    """Raised when this process tries to reach a host that is not local."""


_LOCAL_NAME = 'localhost'
_LIBUV_LOOPS = ('uvloop', 'winloop')


def local_host(host) -> bool:
    if host is None:
        return True        # getaddrinfo(None, port) means a local wildcard
    if isinstance(host, bytes):
        host = host.decode('ascii', 'replace')
    host = str(host).strip('[]').split('%')[0]
    if host == '' or host.lower() == _LOCAL_NAME:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def allowed(address) -> bool:
    if isinstance(address, (str, bytes)):
        return True        # AF_UNIX path
    if isinstance(address, tuple) and address:
        return local_host(address[0])
    return False


def _refuse(what: str, target) -> None:
    raise RemoteNetworkRefused(f'Offline run refused {what} {target!r}')


def clear_proxies(environ=os.environ) -> None:
    for name in [name for name in environ if name.lower().endswith('_proxy')]:
        del environ[name]
    environ['NO_PROXY'] = '*'
    environ['no_proxy'] = '*'


_installed = False
_replaced: list = []        # (owner, attribute, replacement) for active()


def active() -> bool:
    """True when install() ran in this process and every function it replaced is still its replacement."""
    return _installed and bool(_replaced) and all(getattr(owner, name, None) is new
                                                    for owner, name, new in _replaced)


def _is_name(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode('ascii', 'replace')
    return isinstance(host, str) and host.lower() == _LOCAL_NAME


def install() -> None:
    global _installed
    if _installed:
        return
    loaded = [name for name in _LIBUV_LOOPS if sys.modules.get(name) is not None]
    if loaded:
        raise RuntimeError(f'{", ".join(loaded)} was imported before the offline guard; '
                           'its event loop resolves and connects outside these checks')
    for name in _LIBUV_LOOPS:
        sys.modules[name] = None        # import now raises ImportError; uvicorn's auto loop falls back to asyncio
    _installed = True
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_sendto = socket.socket.sendto
    real_sendmsg = getattr(socket.socket, 'sendmsg', None)        # not on Windows
    real_getaddrinfo, real_gethostbyname = socket.getaddrinfo, socket.gethostbyname
    real_gethostbyname_ex, real_gethostbyaddr = socket.gethostbyname_ex, socket.gethostbyaddr
    real_getnameinfo = socket.getnameinfo

    def connect(self, address):
        if not allowed(address):
            _refuse('a connection to', address)
        return real_connect(self, address)

    def connect_ex(self, address):
        if not allowed(address):
            _refuse('a connection to', address)
        return real_connect_ex(self, address)

    def sendto(self, data, *args):
        if args and not allowed(args[-1]):        # sendto(data, address) or sendto(data, flags, address)
            _refuse('a datagram to', args[-1])
        return real_sendto(self, data, *args)

    def sendmsg(self, buffers, *args):
        if len(args) >= 3 and args[2] is not None and not allowed(args[2]):
            _refuse('a message to', args[2])
        return real_sendmsg(self, buffers, *args)

    def getaddrinfo(host, port=None, family=0, type=0, proto=0, flags=0):
        if not local_host(host):
            _refuse('a name lookup for', host)
        if not _is_name(host):
            return real_getaddrinfo(host, port, family, type, proto, flags)    # an address or the wildcard
        address = '::1' if family == socket.AF_INET6 else '127.0.0.1'
        found = real_getaddrinfo(address, port, family, type, proto, flags | socket.AI_NUMERICHOST)
        named = bool(flags & socket.AI_CANONNAME)            # the canonical name goes on the first entry only
        return [(f, t, p, _LOCAL_NAME if named and i == 0 else '', a) for i, (f, t, p, _, a) in enumerate(found)]

    def gethostbyname(host):
        if not local_host(host):
            _refuse('a name lookup for', host)
        return '127.0.0.1' if _is_name(host) else real_gethostbyname(host)

    def gethostbyname_ex(host):
        if not local_host(host):
            _refuse('a name lookup for', host)
        return (_LOCAL_NAME, [], ['127.0.0.1']) if _is_name(host) else real_gethostbyname_ex(host)

    def gethostbyaddr(host):
        if not local_host(host):
            _refuse('an address lookup for', host)
        if host is None or host == '':
            return real_gethostbyaddr(host)        # refused by the socket module itself, before any lookup
        return (_LOCAL_NAME, [], ['127.0.0.1' if _is_name(host) else str(host).strip('[]')])

    def getnameinfo(sockaddr, flags):
        if flags & socket.NI_NUMERICHOST:
            return real_getnameinfo(sockaddr, flags)
        if not allowed(sockaddr):
            _refuse('an address lookup for', sockaddr)       # asks the resolver for the address's name
        numeric = (flags | socket.NI_NUMERICHOST) & ~socket.NI_NAMEREQD        # the name is given below
        _, service = real_getnameinfo(sockaddr, numeric)                       # also checks the address
        return _LOCAL_NAME, service

    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    socket.socket.sendto = sendto
    if real_sendmsg is not None:
        socket.socket.sendmsg = sendmsg
    socket.getaddrinfo, socket.gethostbyname = getaddrinfo, gethostbyname
    socket.gethostbyname_ex, socket.gethostbyaddr = gethostbyname_ex, gethostbyaddr
    socket.getnameinfo = getnameinfo
    _replaced.extend([(socket.socket, 'connect', connect), (socket.socket, 'connect_ex', connect_ex),
                      (socket.socket, 'sendto', sendto), (socket, 'getaddrinfo', getaddrinfo),
                      (socket, 'gethostbyname', gethostbyname), (socket, 'gethostbyname_ex', gethostbyname_ex),
                      (socket, 'gethostbyaddr', gethostbyaddr), (socket, 'getnameinfo', getnameinfo)])
    if real_sendmsg is not None:
        _replaced.append((socket.socket, 'sendmsg', sendmsg))

    if sys.platform == 'win32':
        # The proactor event loop connects through ConnectEx, not socket.connect, and an IP
        # literal skips getaddrinfo, so asyncio needs its own check here.
        from asyncio import windows_events
        real_proactor_connect = windows_events.IocpProactor.connect
        real_proactor_sendto = windows_events.IocpProactor.sendto

        def proactor_connect(self, conn, address):
            if not allowed(address):
                _refuse('a connection to', address)
            return real_proactor_connect(self, conn, address)

        def proactor_sendto(self, conn, buf, flags=0, addr=None):
            if addr is not None and not allowed(addr):
                _refuse('a datagram to', addr)
            return real_proactor_sendto(self, conn, buf, flags, addr)

        windows_events.IocpProactor.connect = proactor_connect
        windows_events.IocpProactor.sendto = proactor_sendto
        _replaced.extend([(windows_events.IocpProactor, 'connect', proactor_connect),
                          (windows_events.IocpProactor, 'sendto', proactor_sendto)])
