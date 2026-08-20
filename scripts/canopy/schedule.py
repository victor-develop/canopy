"""One invariant: the cron entry exists exactly when something is being watched.

Nobody should have to remember to install or remove it. `track` makes a node
active, `untrack` makes the last one inactive, and the entry follows — otherwise
you get the two failure modes that actually happen: a tree that looks watched
but has no cron behind it, and a cron that wakes every five minutes to walk a
tree where every node is untracked.
"""

import sys
from pathlib import Path

from . import cron, store

INSTALLED = "installed"
REMOVED = "removed"
UNCHANGED = "unchanged"


def tick_command():
    """The exact argv cron gets: absolute python, absolute script."""
    main = Path(__file__).resolve().parents[1] / "canopy_main.py"
    return "%s %s tick" % (sys.executable, main)


def has_active(dh):
    for proj_id in store.list_projects(dh):
        tree = store.Tree.load(dh, proj_id)
        for nid in tree.nodes:
            if tree.node(nid).get("status", "active") == "active":
                return True
    return False


def sync(dh, cfg, run=None, tick_cmd=None):
    """-> INSTALLED | REMOVED | UNCHANGED.

    `schedule_backend: none` means somebody else owns the waking. Every tick
    calls this (an `@canopy untrack` in Slack can retire the last node, and the
    entry that woke us should go with it) — so without this early return, the
    first tick of an external scheduler reinstalls the crontab line it was
    started to replace, and the tree gets woken twice a minute by two
    schedulers.
    """
    if (cfg.get("schedule_backend") or "cron") != "cron":
        return UNCHANGED
    wanted = has_active(dh)
    present = cron.installed(run=run)
    if wanted and not present:
        cron.install(tick_cmd or tick_command(),
                     cfg.get("cron_interval_minutes", 5),
                     data_home=str(dh),
                     login_shell=cfg.get("cron_login_shell"), run=run)
        return INSTALLED
    if present and not wanted:
        cron.uninstall(run=run)
        return REMOVED
    return UNCHANGED
