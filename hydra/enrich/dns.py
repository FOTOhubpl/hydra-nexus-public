"""DNS lookups (A, AAAA, CNAME, NS, MX, TXT) through public resolvers.

dnspython is not in uv.lock (docs/CONTRACT_REQUESTS.md), so live queries use the system `dig` binary
(`dig +short @1.1.1.1 <name> <TYPE>`), falling back to `socket.getaddrinfo` for A/AAAA when dig is missing.
Cache records are compatible with the analyst's dnspython output: `{answers: [...], ttl}`.
"""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
from typing import Any

from hydra.enrich.client import Enricher, Response

# Strict hostname: labels of letters/digits/underscore/hyphen, never starting or ending with '-', so a hostile
# hostname can never be read as a dig option (argument injection). The name is also passed with `-q`.
NAME_OK = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?\.)*"
    r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?$"
)
TYPES = ("A", "AAAA", "CNAME", "NS", "MX", "TXT", "SOA")
RESOLVERS = ("1.1.1.1", "8.8.8.8")


def _dig(name: str, rtype: str) -> tuple[str | None, str, Any]:
    if not NAME_OK.match(name) or rtype not in TYPES:
        return f"dns://{rtype}/{name}", "error:invalid", None
    url = f"dns://{rtype}/{name}"
    dig = shutil.which("dig")
    if not dig:
        if rtype not in ("A", "AAAA"):
            return url, "error:no_dig", None
        fam = socket.AF_INET if rtype == "A" else socket.AF_INET6
        try:
            ans = sorted({i[4][0] for i in socket.getaddrinfo(name, None, fam)})
            return url, "ok", {"answers": ans, "ttl": None}
        except socket.gaierror:
            return url, "NXDOMAIN", {"answers": []}
    last = "error:dig"
    for res in RESOLVERS:
        p = subprocess.run(
            [dig, "+noall", "+answer", "+comments", "+time=4", "+tries=1", f"@{res}", "-t", rtype, "-q", name],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        out = p.stdout
        m = re.search(r"status: ([A-Z]+)", out)
        rcode = m.group(1) if m else None
        if rcode == "NXDOMAIN":
            return url, "NXDOMAIN", {"answers": []}
        if rcode != "NOERROR":
            last = f"error:{rcode or 'noresp'}"
            continue
        ans, ttl = [], None
        for line in out.splitlines():
            if not line or line.startswith(";"):
                continue
            parts = line.split(None, 4)
            if len(parts) == 5 and parts[3] == rtype:
                ans.append(parts[4].strip())
                ttl = int(parts[1]) if parts[1].isdigit() else ttl
        return url, "ok", {"answers": sorted(ans), "ttl": ttl}
    return url, last, None


def query(enr: Enricher, name: str, rtype: str) -> Response:
    name = name.lower().rstrip(".")
    if not NAME_OK.match(name) or rtype not in TYPES:
        return Response("dns", f"{name}_{rtype}", "error:invalid", None, None, None)
    return enr.get("dns", f"{name}_{rtype}", lambda: _dig(name, rtype))


def answers(r: Response) -> list[str]:
    return list((r.data or {}).get("answers") or []) if r.ok else []
