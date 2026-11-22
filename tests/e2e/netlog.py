"""Recording socket layer for the e2e API subprocess (HYDRA_E2E_NETLOG=<file>).

Installed before `hydra.cli` runs, so the product's own offline guard (hydra.netguard, HYDRA_OFFLINE=1) wraps it:
every non-loopback connect / sendto / DNS lookup that gets PAST the product guard is appended to the log file
(and still refused). An empty log after a run means the kill switch held; loopback stays usable.
"""

from __future__ import annotations

import json
import os
import socket
import time

_LOCAL = {"localhost", "127.0.0.1", "::1", "0.0.0.0", None, ""}


def _local(sock: socket.socket, address: object) -> bool:
    if sock.family == getattr(socket, "AF_UNIX", object()):
        return True
    return isinstance(address, tuple) and bool(address) and address[0] in _LOCAL


def install(path: str | None = None) -> None:
    path = path or os.environ.get("HYDRA_E2E_NETLOG")
    if not path:
        return
    orig_connect, orig_connect_ex = socket.socket.connect, socket.socket.connect_ex
    orig_sendto, orig_gai = socket.socket.sendto, socket.getaddrinfo

    def record(what: str) -> OSError:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": time.time(), "what": what}) + "\n")
        return OSError(f"e2e netlog: blocked {what}")

    def connect(self, address):  # type: ignore[no-untyped-def]
        if not _local(self, address):
            raise record(f"connect {address!r}")
        return orig_connect(self, address)

    def connect_ex(self, address):  # type: ignore[no-untyped-def]
        if not _local(self, address):
            raise record(f"connect_ex {address!r}")
        return orig_connect_ex(self, address)

    def sendto(self, data, *args):  # type: ignore[no-untyped-def]
        address = args[-1] if args else None
        if address is not None and not _local(self, address):
            raise record(f"sendto {address!r}")
        return orig_sendto(self, data, *args)

    def getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        h = host.decode() if isinstance(host, bytes) else host
        if h not in _LOCAL:
            raise socket.gaierror(str(record(f"getaddrinfo {h!r}")))
        return orig_gai(host, *args, **kwargs)

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.socket.sendto = sendto  # type: ignore[method-assign]
    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
