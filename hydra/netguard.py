"""Network policy shared by every outbound path (CR-ASTRA-SEC-8, -9; audit A09).

1. `HYDRA_OFFLINE=1` is the kill switch. It dominates every explicit override (`force=True`, `live=True`,
   an enabled LLM config, `HYDRA_MODEL_DOWNLOAD=1`). `offline()` reads the current value; `require_online()`
   raises `OfflineBlocked` at the top of each remote call site (enrichment, registry, ad library, scheduler,
   watch recorder, model download, remote LLM).
2. `install_offline_guard()` adds a process-level Python socket guard for the server and CLI when offline:
   `connect`/`connect_ex`/`sendto` to non-loopback addresses and DNS lookups of non-loopback names raise.
   Loopback stays usable (the API itself, the optional local LLM server). It covers every Python library in the
   process; it does not cover subprocesses (`dig`, `ffmpeg`), which only run network-free here or behind the
   live-enrichment switch. It is not an OS firewall.
3. `check_url()` is the SSRF policy for a URL we did not choose (page fetch hops, registry redirects): scheme
   http/https only, no local names (`localhost`, `*.local`, `*.internal`, metadata hosts), literal IPs in every
   form (`2130706433`, `0177.0.0.1`, `0x7f000001`, IPv6, IPv4-mapped IPv6) must be global unicast, and with
   `resolve=True` every resolved address must be public.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import threading
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")
LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "metadata",
               "metadata.google.internal", "instance-data", "instance-data.ec2.internal"}
LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".lan", ".intranet", ".corp")
_LOOPBACK_NAMES = {"localhost", "ip6-localhost", "ip6-loopback", "localhost.localdomain"}


class OfflineBlocked(OSError):
    """A remote network operation was attempted while HYDRA_OFFLINE=1."""


class UnsafeDestination(ValueError):
    """A URL points at a non-public destination or uses a scheme other than http/https."""


def offline(settings: object | None = None) -> bool:
    """Kill-switch state. An explicit Settings object wins; otherwise the environment (then Settings)."""
    if settings is not None and hasattr(settings, "offline"):
        return bool(settings.offline)
    env = os.environ.get("HYDRA_OFFLINE")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    from hydra.config import get_settings

    return bool(get_settings().offline)


def require_online(what: str, settings: object | None = None) -> None:
    if offline(settings):
        raise OfflineBlocked(f"HYDRA_OFFLINE=1: {what} is blocked (tryb offline, brak połączeń sieciowych)")


def model_download_allowed(settings: object | None = None) -> bool:
    """Model downloads need HYDRA_MODEL_DOWNLOAD=1 and no offline kill switch."""
    return os.environ.get("HYDRA_MODEL_DOWNLOAD") == "1" and not offline(settings)


# ------------------------------------------------------------------------------------- address checks


def literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """IP literal in any form a resolver accepts, without DNS: dotted, IPv6 (bracketed or not), and the
    inet_aton shorthands (decimal `2130706433`, octal `0177.0.0.1`, hex `0x7f000001`, `127.1`)."""
    h = host.strip().strip("[]").rstrip(".")
    if not h:
        return None
    try:
        return ipaddress.ip_address(h.split("%", 1)[0])
    except ValueError:
        pass
    if all(part and (part.isdigit() or part.lower().startswith("0x")) for part in h.split(".")):
        try:
            return ipaddress.IPv4Address(socket.inet_aton(h))
        except OSError:
            return None
    return None


def is_public_ip(ip: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Global unicast only: no loopback, private, link-local (cloud metadata), CGNAT, multicast, reserved,
    unspecified; IPv4-mapped / 6to4 / Teredo IPv6 addresses are judged by the embedded IPv4 address."""
    try:
        addr = ipaddress.ip_address(str(ip).split("%", 1)[0]) if not isinstance(
            ip, ipaddress.IPv4Address | ipaddress.IPv6Address) else ip
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        embedded = addr.ipv4_mapped or addr.sixtofour or (addr.teredo[1] if addr.teredo else None)
        if embedded is not None and not is_public_ip(embedded):
            return False
    return bool(addr.is_global) and not addr.is_multicast and not addr.is_reserved and not addr.is_unspecified


def is_local_name(host: str) -> bool:
    h = host.strip().strip("[]").rstrip(".").lower()
    return h in LOCAL_NAMES or h.endswith(LOCAL_SUFFIXES) or "." not in h and literal_ip(h) is None


def check_url(url: str, *, resolve: bool = False) -> str:
    """Validate a URL we did not choose. Returns the hostname; raises UnsafeDestination."""
    try:
        sp = urlsplit(url)
        host = sp.hostname
        _ = sp.port  # raises ValueError on a malformed port
    except ValueError as exc:
        raise UnsafeDestination(f"malformed URL: {exc}") from exc
    if sp.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeDestination(f"unsupported URL scheme {sp.scheme!r}")
    if not host:
        raise UnsafeDestination("URL without host")
    ip = literal_ip(host)
    if ip is not None:
        if not is_public_ip(ip):
            raise UnsafeDestination(f"non-public address {ip}")
        return host
    if is_local_name(host):
        raise UnsafeDestination(f"local host name {host!r}")
    if resolve:
        port = sp.port or (443 if sp.scheme.lower() == "https" else 80)
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise UnsafeDestination(f"unresolvable host {host!r}") from exc
        for *_rest, addr in infos:
            if not is_public_ip(addr[0]):
                raise UnsafeDestination(f"non-public address {addr[0]}")
    return host


# ------------------------------------------------------------------------------------- process socket guard

_GUARD_LOCK = threading.Lock()
_ORIGINALS: dict[str, object] = {}


def _loopback_host(host: object) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    h = str(host).strip().strip("[]").lower()
    if h in ("", *_LOOPBACK_NAMES) or h.endswith(".localhost"):
        return True
    ip = literal_ip(h)
    return ip is not None and ip.is_loopback


def _address_ok(sock: socket.socket, address: object) -> bool:
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        return True  # AF_UNIX and friends are local by construction
    return isinstance(address, tuple) and bool(address) and _loopback_host(address[0])


def install_offline_guard() -> bool:
    """Install the Python-level socket kill switch (idempotent). Returns True when installed by this call."""
    with _GUARD_LOCK:
        if _ORIGINALS:
            return False
        connect, connect_ex, sendto = socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto
        getaddrinfo = socket.getaddrinfo
        _ORIGINALS.update(connect=connect, connect_ex=connect_ex, sendto=sendto, getaddrinfo=getaddrinfo)

        def guarded_connect(self, address):  # type: ignore[no-untyped-def]
            if not _address_ok(self, address):
                raise OfflineBlocked(f"HYDRA_OFFLINE=1: connection to {address!r} blocked")
            return connect(self, address)

        def guarded_connect_ex(self, address):  # type: ignore[no-untyped-def]
            if not _address_ok(self, address):
                raise OfflineBlocked(f"HYDRA_OFFLINE=1: connection to {address!r} blocked")
            return connect_ex(self, address)

        def guarded_sendto(self, data, *args):  # type: ignore[no-untyped-def]
            if args and not _address_ok(self, args[-1]):
                raise OfflineBlocked(f"HYDRA_OFFLINE=1: datagram to {args[-1]!r} blocked")
            return sendto(self, data, *args)

        def guarded_getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
            if not _loopback_host(host):
                raise OfflineBlocked(f"HYDRA_OFFLINE=1: DNS lookup of {host!r} blocked")
            return getaddrinfo(host, *args, **kwargs)

        socket.socket.connect = guarded_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
        socket.socket.sendto = guarded_sendto  # type: ignore[method-assign]
        socket.getaddrinfo = guarded_getaddrinfo  # type: ignore[assignment]
        return True


def uninstall_offline_guard() -> None:
    with _GUARD_LOCK:
        if not _ORIGINALS:
            return
        socket.socket.connect = _ORIGINALS["connect"]  # type: ignore[method-assign]
        socket.socket.connect_ex = _ORIGINALS["connect_ex"]  # type: ignore[method-assign]
        socket.socket.sendto = _ORIGINALS["sendto"]  # type: ignore[method-assign]
        socket.getaddrinfo = _ORIGINALS["getaddrinfo"]  # type: ignore[assignment]
        _ORIGINALS.clear()


def guard_if_offline(settings: object | None = None) -> bool:
    """Entry-point hook (server, CLI): install the socket guard when offline unless HYDRA_SOCKET_GUARD=0."""
    if os.environ.get("HYDRA_SOCKET_GUARD", "1") == "0" or not offline(settings):
        return False
    return install_offline_guard()
