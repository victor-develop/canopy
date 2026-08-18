"""What the ops page shows, as plain data plus a render of it.

Served by `canopy serve` (see webserve.py). The snapshot is rebuilt per request,
so the page is live; elapsed times are still computed in the *browser* from the
embedded timestamps, which is what keeps it honest when the thing being watched
has stopped: a cron entry that fires and crashes every time produces no new
data, and a counter that only moves when data arrives would look calm. Here it
keeps climbing and goes red.
"""

import json
import time
from pathlib import Path

from . import cron, events, locks, noderef, paths, schedule, store

TEMPLATE = "ops.html"


def _worker_processes(dh, cfg, now=None, alive=None):
    """Canopy's own CLI processes, read from the per-node lock files."""
    out = []
    stale = int(cfg.get("lock_stale_seconds", 1800))
    for proj_id in store.list_projects(dh):
        try:
            tree = store.Tree.load(dh, proj_id)
        except (OSError, ValueError):
            continue
        alias_map = noderef.aliases(tree)
        for nid in tree.nodes:
            node_dir = paths.node_dir(dh, proj_id, nid)
            info = locks.read(node_dir)
            if not info:
                continue
            out.append({
                "project": proj_id,
                "node": nid,
                "alias": alias_map.get(nid, ""),
                "title": tree.node(nid).get("title") or nid,
                "pid": info.get("pid"),
                "started": info.get("started"),
                "held": locks.is_held(node_dir, now=now, stale_after=stale,
                                      alive=alive),
                "runner": cfg.get("runner_path") or cfg.get("runner"),
            })
    return out


def _trees(dh):
    out = []
    for proj_id in store.list_projects(dh):
        try:
            tree = store.Tree.load(dh, proj_id)
        except (OSError, ValueError):
            continue
        alias_map = noderef.aliases(tree)
        nodes = []
        for nid in sorted(tree.nodes, key=lambda n: alias_map.get(n, "")):
            try:
                state = store.load_state(dh, proj_id, nid)
            except FileNotFoundError:
                state = {}
            nodes.append({
                "node": nid,
                "alias": alias_map.get(nid, ""),
                "title": tree.node(nid).get("title") or nid,
                "status": tree.node(nid).get("status", "active"),
                "cursor": state.get("cursor"),
                "checkpoints": (_checkpoints(dh, proj_id, nid)
                                if state.get("feed_ts") else 0),
                "raw_permalink": state.get("raw_permalink"),
            })
        out.append({"project": proj_id, "nodes": nodes})
    return out


def _checkpoints(dh, proj_id, nid):
    from . import feed as feed_mod
    segments = feed_mod.load_segments(paths.node_dir(dh, proj_id, nid))
    return sum(len(s.get("entries") or []) for s in segments)


def snapshot(dh, cfg, now=None, alive=None, run=None):
    """Everything the page shows, as plain data."""
    now = time.time() if now is None else now
    log = events.read(dh, limit=200)
    ticks = [e for e in log if e.get("kind") == "tick"]
    workers = [e for e in log if e.get("kind") == "worker"]
    errors = [e for e in log if e.get("error")]
    interval = int(cfg.get("cron_interval_minutes", 5))
    return {
        "now": now,
        "data_home": str(dh),
        "runner": cfg.get("runner"),
        "runner_path": cfg.get("runner_path"),
        "slack_cli": cfg.get("slack_cli_path") or cfg.get("slack_cli"),
        "cron": {
            "installed": cron.installed(run=run),
            "interval_minutes": interval,
            # Two intervals of silence is not a blip, it is a broken tick.
            "overdue_after": interval * 60 * 2,
            "should_run": schedule.has_active(dh),
        },
        "last_tick": ticks[-1] if ticks else None,
        "running": _worker_processes(dh, cfg, now=now, alive=alive),
        "trees": _trees(dh),
        "recent_workers": workers[-25:][::-1],
        "recent_status": [e for e in log if e.get("kind") == "status"][-10:][::-1],
        "recent_errors": errors[-10:][::-1],
        "ticks": ticks[-60:],
    }


def render(data, root=None):
    root = Path(root) if root else paths.skill_root()
    template = (root / "templates" / TEMPLATE).read_text(encoding="utf-8")
    return template.replace("{{SNAPSHOT}}", embed(data))


def embed(data):
    """JSON for a `<script>` literal.

    `json.dumps` does not escape `<`, `>` or `&`, and everything in this
    snapshot came from Slack: a thread title of `</script><img src=x onerror=…>`
    would close the script tag and run. The title is whatever the person who
    opened the thread typed, so that is any workspace member.
    """
    return (json.dumps(data, ensure_ascii=False, sort_keys=True)
            .replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026").replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))



