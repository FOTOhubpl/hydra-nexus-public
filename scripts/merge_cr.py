"""Resolve add/add conflicts in docs/CONTRACT_REQUESTS.md: union of '## ' sections, later version wins."""
import re
import subprocess
import sys

path = "docs/CONTRACT_REQUESTS.md"


def stage(n: int) -> str:
    r = subprocess.run(["git", "show", f":{n}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def sections(text: str) -> tuple[str, dict[str, str]]:
    parts = re.split(r"(?m)^(?=## )", text)
    head = parts[0] if parts and not parts[0].startswith("## ") else ""
    secs = {}
    for p in parts:
        if p.startswith("## "):
            secs[p.splitlines()[0].split("—")[0].split(":")[0].strip()] = p.rstrip() + "\n"
    return head, secs


if not stage(2) or not stage(3):
    sys.exit("merge_cr: no add/add conflict staged for docs/CONTRACT_REQUESTS.md; nothing to do")
ours_head, ours = sections(stage(2))
_, theirs = sections(stage(3))
merged = dict(ours)
merged.update(theirs)  # theirs (incoming branch) wins for the same id: it is the newer status
out = (ours_head.rstrip() + "\n\n" if ours_head else "") + "\n".join(merged.values())
open(path, "w").write(out)
print(f"{len(merged)} sections", file=sys.stderr)
