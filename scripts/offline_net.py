"""Refuse remote networking in this process: used by the offline tests and the fixture soak.

``install()`` replaces the socket entry points that can reach another host
(connect, connect_ex, sendto, sendmsg, the name and address lookups including
``getnameinfo``'s reverse lookup, and on
Windows the proactor connect and sendto that asyncio uses in place of
``socket.connect``) with versions that allow only loopback addresses and local
sockets. Anything else raises ``RemoteNetworkRefused`` before a packet leaves.

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


_LOCAL_NAMES = {'localhost', 'localhost.localdomain', 'ip6-localhost', 'ip6-loopback'}


def local_host(host) -> bool:
    if host is None:
        return True        # getaddrinfo(None, port) means a local wildcard
    if isinstance(host, bytes):
        host = host.decode('ascii', 'replace')
    host = str(host).strip('[]').split('%')[0]
    if host == '' or host.lower() in _LOCAL_NAMES:
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


def install() -> None:
    global _installed
    if _installed:
        return
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

    def getaddrinfo(host, *args, **kwargs):
        if not local_host(host):
            _refuse('a name lookup for', host)
        return real_getaddrinfo(host, *args, **kwargs)

    def gethostbyname(host):
        if not local_host(host):
            _refuse('a name lookup for', host)
        return real_gethostbyname(host)

    def gethostbyname_ex(host):
        if not local_host(host):
            _refuse('a name lookup for', host)
        return real_gethostbyname_ex(host)

    def gethostbyaddr(host):
        if not local_host(host):
            _refuse('an address lookup for', host)
        return real_gethostbyaddr(host)

    def getnameinfo(sockaddr, flags):
        if not (flags & socket.NI_NUMERICHOST) and not allowed(sockaddr):
            _refuse('an address lookup for', sockaddr)       # asks the resolver for the address's name
        return real_getnameinfo(sockaddr, flags)

    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    socket.socket.sendto = sendto
    if real_sendmsg is not None:
        socket.socket.sendmsg = sendmsg
    socket.getaddrinfo, socket.gethostbyname = getaddrinfo, gethostbyname
    socket.gethostbyname_ex, socket.gethostbyaddr = gethostbyname_ex, gethostbyaddr
    socket.getnameinfo = getnameinfo

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
