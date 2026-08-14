"""One lock file per node: present == in use.

If a worker overruns a tick, the next tick sees the lock and skips that node
this round; the new messages just wait and get picked up next tick, because the
cursor re-pull covers them. A dead lock (process gone, or older than the
staleness timeout) is broken rather than blocking a node forever.
"""

import json
import os
import time
from pathlib import Path

from .errors import LockedError

LOCK_NAME = "lock"


def lock_path(node_dir):
    return Path(node_dir) / LOCK_NAME


def read(node_dir):
    path = lock_path(node_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        # A truncated lock from a killed worker is a dead lock, not a puzzle.
        return {"pid": None, "started": 0}


def _alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    except (TypeError, ValueError):
        return False
    return True


def is_held(node_dir, now=None, stale_after=1800, alive=None):
    info = read(node_dir)
    if info is None:
        return False
    now = time.time() if now is None else now
    if now - float(info.get("started") or 0) > stale_after:
        return False
    if not info.get("pid"):
        # Unreadable lock: fall back to the staleness window rather than
        # breaking it. Two workers posting into one thread is worse than one
        # node waiting a few ticks.
        return True
    alive_fn = alive or _alive
    return alive_fn(info.get("pid"))


def acquire(node_dir, pid=None, now=None, stale_after=1800, alive=None):
    node_dir = Path(node_dir)
    node_dir.mkdir(parents=True, exist_ok=True)
    if is_held(node_dir, now=now, stale_after=stale_after, alive=alive):
        raise LockedError("node is locked: %s" % (node_dir,))
    now = time.time() if now is None else now
    payload = {"pid": pid if pid is not None else os.getpid(), "started": now}
    lock_path(node_dir).write_text(json.dumps(payload), encoding="utf-8")
    return payload


def release(node_dir):
    path = lock_path(node_dir)
    if path.exists():
        path.unlink()
        return True
    return False


class held(object):
    """`with held(node_dir):` — release even when the worker raises."""

    def __init__(self, node_dir, pid=None, now=None, stale_after=1800, alive=None):
        self.node_dir = node_dir
        self.pid = pid
        self.now = now
        self.stale_after = stale_after
        self.alive = alive

    def __enter__(self):
        return acquire(self.node_dir, pid=self.pid, now=self.now,
                       stale_after=self.stale_after, alive=self.alive)

    def __exit__(self, *exc):
        release(self.node_dir)
        return False
