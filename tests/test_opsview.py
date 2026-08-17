"""The ops page: is it alive, what is it running, and how long has it been."""

import json

from canopy import events, locks, opsview, paths, store


def snapshot_of(dh, cfg=None, **kw):
    return opsview.snapshot(dh, cfg or {"cron_interval_minutes": 5}, **kw)


def test_a_missing_cron_entry_is_visible_while_nodes_are_active(dh):
    tree = store.Tree.new("pay", "C1-1.0", "支付超时", "A君")
    tree.save(dh)
    snap = snapshot_of(dh, run=lambda argv, stdin="": (0, "", ""))
    assert snap["cron"]["should_run"] is True
    assert snap["cron"]["installed"] is False


def test_overdue_is_expressed_as_data_not_as_a_verdict(dh):
    """The page decides 'overdue' in the browser, so the counter keeps moving
    even when nothing rewrites the file."""
    snap = snapshot_of(dh, cfg={"cron_interval_minutes": 5},
                       run=lambda argv, stdin="": (0, "", ""))
    assert snap["cron"]["overdue_after"] == 600
    assert "now" in snap


def test_a_held_lock_shows_up_as_a_running_worker(dh):
    tree = store.Tree.new("pay", "C1-1.0", "支付超时", "A君")
    tree.save(dh)
    node_dir = paths.node_dir(dh, "pay", "C1-1.0")
    locks.acquire(node_dir, pid=4242, now=1000.0)
    snap = snapshot_of(dh, now=1100.0, alive=lambda pid: True,
                       run=lambda argv, stdin="": (0, "", ""))
    running = snap["running"][0]
    assert running["pid"] == 4242 and running["held"] is True
    assert running["alias"] == "1" and running["started"] == 1000.0


def test_a_dead_lock_is_reported_but_not_counted_as_running(dh):
    tree = store.Tree.new("pay", "C1-1.0", "支付超时", "A君")
    tree.save(dh)
    locks.acquire(paths.node_dir(dh, "pay", "C1-1.0"), pid=1, now=1000.0)
    snap = snapshot_of(dh, alive=lambda pid: False,
                       run=lambda argv, stdin="": (0, "", ""))
    assert snap["running"][0]["held"] is False


def test_events_carry_what_each_worker_cost(dh):
    events.append(dh, {"kind": "worker", "ts": 10, "duration": 12.5,
                       "node": "C1-1.0", "mode": "light", "outcome": "checkpoint"})
    events.append(dh, {"kind": "tick", "ts": 11, "verdicts": {"work": 1}})
    events.append(dh, {"kind": "worker", "ts": 12, "error": "runner exited 1"})
    snap = snapshot_of(dh, run=lambda argv, stdin="": (0, "", ""))
    assert snap["last_tick"]["verdicts"] == {"work": 1}
    assert snap["recent_workers"][0]["error"] == "runner exited 1"
    assert snap["recent_errors"][0]["error"] == "runner exited 1"


def test_events_survive_a_corrupt_line(dh):
    events.append(dh, {"kind": "tick", "ts": 1})
    with events.path(dh).open("a", encoding="utf-8") as fh:
        fh.write("{ this is not json\n")
    events.append(dh, {"kind": "tick", "ts": 2})
    assert [e["ts"] for e in events.read(dh)] == [1, 2]


def test_events_are_trimmed(dh):
    for i in range(60):
        events.append(dh, {"kind": "tick", "ts": i}, keep=20)
    kept = events.read(dh, limit=1000)
    assert len(kept) <= 26 and kept[-1]["ts"] == 59


def test_render_inlines_the_snapshot(dh, repo):
    tree = store.Tree.new("pay", "C1-1.0", "支付超时", "A君")
    tree.save(dh)
    html = opsview.render(snapshot_of(dh, run=lambda argv, stdin="": (0, "", "")),
                          root=repo)
    assert "{{SNAPSHOT}}" not in html
    assert "支付超时" in html
    # No server, no fetch: the data is in the page.
    assert "fetch(" not in html
