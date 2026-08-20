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

from .errors import LockedError  # re-exported for callers

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
    """Does this pid exist? EPERM means it does — it just isn't ours."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return True


MAX_AGE = 6 * 3600


def is_held(node_dir, now=None, stale_after=1800, alive=None, max_age=MAX_AGE):
    info = read(node_dir)
    if info is None:
        return False
    now = time.time() if now is None else now
    age = now - float(info.get("started") or 0)
    pid = info.get("pid")
    if pid:
        # A live process holds its lock as long as it needs — `recalibrate` can
        # legitimately run for hours, and breaking its lock on a 30-minute rule
        # put a second worker into the same node.
        #
        # But not forever: pids get recycled (macOS wraps at 99999), so a lock
        # left behind by a SIGKILLed worker eventually names an unrelated live
        # process, and without an upper bound that node was never watched again.
        return (alive or _alive)(pid) and age <= max_age
    # No readable pid: fall back to the staleness window rather than breaking it
    # outright. Two workers in one thread is worse than one node waiting.
    return age <= stale_after


def acquire(node_dir, pid=None, now=None, stale_after=1800, alive=None,
            max_age=MAX_AGE):
    node_dir = Path(node_dir)
    node_dir.mkdir(parents=True, exist_ok=True)
    if is_held(node_dir, now=now, stale_after=stale_after, alive=alive,
               max_age=max_age):
        raise LockedError("node is locked: %s" % (node_dir,))
    now = time.time() if now is None else now
    payload = {"pid": pid if pid is not None else os.getpid(), "started": now}
    lock_path(node_dir).write_text(json.dumps(payload), encoding="utf-8")
    return payload


def refresh(node_dir, pid=None, now=None):
    """Restamp a lock you already hold. -> the payload written.

    Only the holder calls this. Releasing and re-acquiring would work too,
    except for the window in between — which is exactly when a second holder
    slips in, and a long-lived holder (`canopy loop`) would open that window on
    every iteration.
    """
    node_dir = Path(node_dir)
    node_dir.mkdir(parents=True, exist_ok=True)
    payload = {"pid": pid if pid is not None else os.getpid(),
               "started": time.time() if now is None else now}
    lock_path(node_dir).write_text(json.dumps(payload), encoding="utf-8")
    return payload


def release(node_dir, pid=None):
    """Only ever remove your own lock.

    A worker that overran and got its lock broken used to delete the *new*
    holder's lock on the way out, leaving the node unlocked with a worker still
    inside it.
    """
    path = lock_path(node_dir)
    if not path.exists():
        return False
    info = read(node_dir) or {}
    mine = pid if pid is not None else os.getpid()
    if info.get("pid") not in (None, mine):
        return False
    path.unlink()
    return True


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
        release(self.node_dir, pid=self.pid if self.pid is not None else None)
        return False
