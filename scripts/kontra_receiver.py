"""Run the KONTRA local demonstration receiver on loopback.

    uv run python scripts/kontra_receiver.py [--host 127.0.0.1] [--port 8765] [--store DIR]

The HYDRA API sends packages to HYDRA_KONTRA_RECEIVER (default http://127.0.0.1:8765/receive). Any non-loopback
bind address is refused. Default store: $HYDRA_STATE_DIR/kontra-receiver or /tmp/hydra-kontra-receiver.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hydra.kontra_receiver import create_app  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--store", type=Path, default=None)
    a = ap.parse_args()
    if a.host != "localhost" and not ipaddress.ip_address(a.host).is_loopback:
        sys.exit("refused: the demonstration receiver binds to loopback only")
    if a.port in (8000, 8100):
        sys.exit("refused: ports 8000/8100 belong to the HYDRA API / live demo")
    state = os.environ.get("HYDRA_STATE_DIR")
    store = a.store or (Path(state) / "kontra-receiver" if state else Path("/tmp/hydra-kontra-receiver"))
    import uvicorn

    uvicorn.run(create_app(store), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
