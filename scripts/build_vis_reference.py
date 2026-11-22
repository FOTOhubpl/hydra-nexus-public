"""Build the small face reference base (and optionally logo templates) from material-named faces (task §5
allows a small base of frequent persons). Thin wrapper over hydra.reference_build (same as
`hydra reference build --from DIR`). Output OUTSIDE git: --out, else $HYDRA_VIS_REFERENCE_DIR, else
<state>/reference.

    . scripts/env.sh && uv run python scripts/build_vis_reference.py [--sample DIR] [--logos] [--out DIR]
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from hydra import reference_build as rb
from hydra.config import get_settings


def main() -> None:
    s = get_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", default=str(s.data_dir / "nask" / "sample"),
                    help="dataset folder; 'state' = the imported incidents of the state")
    ap.add_argument("--logos", action="store_true", help="also rebuild logo templates (recurring brand marks)")
    ap.add_argument("--out", default=os.environ.get("HYDRA_VIS_REFERENCE_DIR") or None)
    args = ap.parse_args()
    source = None if args.sample == "state" else Path(args.sample)
    res = rb.build(s, source, faces=True, logos=args.logos, out=Path(args.out) if args.out else None,
                   progress=lambda i, n, what: print(f"[{i + 1}/{n}] {what}"))
    print(res["message_pl"])


if __name__ == "__main__":
    main()
