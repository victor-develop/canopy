"""The ops page: what canopy is running, and how long since it last ran.

Deliberately a **static file**, rewritten by every tick. A served page would need
a process, and "no daemon" is the one rule this design does not bend. Elapsed
times are computed in the browser from embedded timestamps, so the page stays
useful between ticks: the "last tick 3m ago" counter keeps climbing on its own,
and turns red once the tick is overdue. That is the failure this page exists for
— a cron entry that fires and crashes every time reports nothing, and nobody
notices for half an hour.
"""

import json
import time
from pathlib import Path

from . import cron, events, locks, noderef, paths, schedule, store

FILE = "ops.html"
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
                "checkpoints": len(
                    (state.get("feed_ts") or [])) and _checkpoints(dh, proj_id, nid),
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
        "recent_errors": errors[-10:][::-1],
        "ticks": ticks[-60:],
    }


def render(data, root=None):
    root = Path(root) if root else paths.skill_root()
    template = (root / "templates" / TEMPLATE).read_text(encoding="utf-8")
    return template.replace(
        "{{SNAPSHOT}}", json.dumps(data, ensure_ascii=False, sort_keys=True))


def write(dh, cfg, root=None, now=None, alive=None, run=None):
    """-> the path of the page. Called by every tick, and by `canopy ops`."""
    data = snapshot(dh, cfg, now=now, alive=alive, run=run)
    out = Path(dh) / FILE
    out.write_text(render(data, root=root), encoding="utf-8")
    return out
