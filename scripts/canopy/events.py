"""An append-only log of what the tick actually did, one JSON object per line.

`tick.log` answers "did it run"; this answers "what did it spend, on which node,
and how long did that take" — the two questions you have when a tree feels stuck
or a bill feels high. It is written by the tick and read by the ops page; nothing
else depends on it, so a corrupt line is skipped rather than raised.
"""

import json
import os
import tempfile
from pathlib import Path

FILE = "events.jsonl"
KEEP = 2000


def path(dh):
    return Path(dh) / FILE


def append(dh, event, keep=KEEP):
    """Write one event. Trims the file when it grows past `keep` lines."""
    p = path(dh)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, sort_keys=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    _trim(p, keep)
    return event


def _trim(p, keep):
    # Cheap gate first: reading the whole file on every append is what makes a
    # log expensive. 40 bytes is below any real event line, so this can only
    # skip the read when the file is definitely still short.
    try:
        if p.stat().st_size < keep * 40:
            return
        with p.open(encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return
    if len(lines) <= keep * 1.25:
        return
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tmp-events-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.writelines(lines[-keep:])
    os.replace(tmp, str(p))


def read(dh, limit=200):
    """-> the most recent events, oldest first. Bad lines are skipped."""
    p = path(dh)
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as fh:
        lines = fh.readlines()[-limit:]
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
