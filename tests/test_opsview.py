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
    # The first paint needs no round trip: the snapshot ships inside the page,
    # so a stopped server degrades to "last known state, counters still moving".
    assert "let D = {" in html


# -- served, not written to disk ----------------------------------------------

def test_the_server_answers_html_and_json(dh, repo):
    """A real server on a real loopback port — the page has no other source."""
    import urllib.request
    from canopy import webserve

    tree = store.Tree.new("pay", "C1-1.0", "支付超时", "A君")
    tree.save(dh)
    httpd, base = webserve.serve_in_thread(dh, {"cron_interval_minutes": 5},
                                           root=repo, port=0)
    try:
        html = urllib.request.urlopen(base + "/").read().decode("utf-8")
        assert "支付超时" in html and "{{SNAPSHOT}}" not in html

        data = json.loads(urllib.request.urlopen(base + "/api/snapshot").read())
        assert data["trees"][0]["project"] == "pay"

        try:
            urllib.request.urlopen(base + "/etc/passwd")
            raise AssertionError("should 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_snapshot_is_rebuilt_per_request(dh, repo):
    """No file to go stale: track something and the next request shows it."""
    import urllib.request
    from canopy import webserve

    httpd, base = webserve.serve_in_thread(dh, {}, root=repo, port=0)
    try:
        first = json.loads(urllib.request.urlopen(base + "/api/snapshot").read())
        assert first["trees"] == []

        store.Tree.new("edd", "C1-9.0", "EDD 不准", "A君").save(dh)
        second = json.loads(urllib.request.urlopen(base + "/api/snapshot").read())
        assert second["trees"][0]["project"] == "edd"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_it_binds_loopback_only(dh, repo):
    """The snapshot carries channel names and local paths — not LAN material."""
    from canopy import webserve
    httpd, _ = webserve.serve_in_thread(dh, {}, root=repo, port=0)
    try:
        assert httpd.server_address[0] == "127.0.0.1"
    finally:
        httpd.shutdown()
        httpd.server_close()


def fake_child(dh, port=4300, pid=99):
    """Stands in for the spawned viewer: records the port it bound, as the real
    child does before the parent reports any URL."""
    from canopy import store, webserve

    def spawn(argv, _dh):
        store.write_json(webserve.state_path(dh), {"pid": pid, "port": port})
        return pid
    return spawn


def test_start_background_reuses_a_live_viewer(dh, monkeypatch):
    from canopy import webserve
    spawned = []
    child = fake_child(dh)

    def spawn(argv, d):
        spawned.append(argv)
        return child(argv, d)

    monkeypatch.setattr(webserve, "_spawn", spawn)
    monkeypatch.setattr(webserve, "_alive", lambda pid: True)
    monkeypatch.setattr(webserve, "_responds", lambda port: True)
    first = webserve.start_background(dh, {})
    second = webserve.start_background(dh, {})
    assert len(spawned) == 1                      # one viewer, not one per track
    assert second["port"] == first["port"]


def test_a_dead_viewer_is_replaced(dh, monkeypatch):
    from canopy import webserve
    spawned = []
    monkeypatch.setattr(webserve, "_spawn", lambda argv, dh: spawned.append(argv) or 99)
    monkeypatch.setattr(webserve, "_alive", lambda pid: False)
    monkeypatch.setattr(webserve, "_responds", lambda port: False)
    webserve.start_background(dh, {"serve_start_timeout": 0})
    webserve.start_background(dh, {"serve_start_timeout": 0})
    assert len(spawned) == 2


def test_the_viewer_exits_when_nobody_is_looking(dh, repo):
    """A viewer that outlives the person who opened it is a process leak."""
    import threading
    import time as _t
    from canopy import webserve

    seen = {"at": _t.time() - 3600}          # last request an hour ago
    httpd = webserve.make_server(dh, {}, root=repo, port=0, seen=seen)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    webserve._idle_watchdog(httpd, seen, timeout=1)

    thread.join(timeout=5)
    assert not thread.is_alive()


def test_a_request_keeps_it_alive(dh, repo):
    import threading
    import time as _t
    import urllib.request
    from canopy import webserve

    seen = {"at": _t.time()}
    httpd = webserve.make_server(dh, {}, root=repo, port=0, seen=seen)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/api/snapshot" % httpd.server_port).read()
        assert _t.time() - seen["at"] < 1     # the request refreshed the clock
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_thread_title_cannot_close_the_script_tag(dh, repo):
    """Every string in the snapshot came from Slack; a title is whatever the
    person who opened the thread typed."""
    tree = store.Tree.new("pay", "C1-1.0", "</script><img src=x onerror=alert(1)>",
                          "A君")
    tree.save(dh)
    html = opsview.render(snapshot_of(dh, run=lambda argv, stdin="": (0, "", "")),
                          root=repo)
    assert "</script><img" not in html
    assert "\\u003c/script\\u003e" in html


def test_embed_escapes_line_separators():
    # U+2028/9 are valid JSON but break a JS literal.
    assert "\\u2028" in opsview.embed({"x": "a\u2028b"})


def test_a_silent_connection_cannot_wedge_the_server(dh, repo):
    """Chrome's speculative preconnect opens a socket and says nothing. On a
    single-threaded server that blocked every later request and shutdown()."""
    import socket as _socket
    import urllib.request
    from canopy import webserve

    httpd, base = webserve.serve_in_thread(dh, {}, root=repo, port=0,
                                           run=lambda argv, stdin="": (0, "", ""))
    silent = _socket.create_connection(("127.0.0.1", httpd.server_port))
    try:
        got = urllib.request.urlopen(base + "/api/snapshot", timeout=5).read()
        assert json.loads(got.decode("utf-8"))["trees"] == []
    finally:
        silent.close()
        httpd.shutdown()
        httpd.server_close()


def test_the_page_does_not_fork_a_crontab_per_request(dh, repo):
    """It advertises itself as read-only; it was shelling out 720×/hour."""
    import urllib.request
    from canopy import webserve

    calls = []

    def counting(argv, stdin=""):
        calls.append(argv)
        return (0, "", "")

    httpd, base = webserve.serve_in_thread(
        dh, {}, root=repo, port=0, run=webserve._cached_crontab_from(counting))
    try:
        for _ in range(5):
            urllib.request.urlopen(base + "/api/snapshot", timeout=5).read()
        assert len(calls) == 1
    finally:
        httpd.shutdown()
        httpd.server_close()
        httpd.server_close()


def test_start_background_reports_the_port_the_child_actually_bound(dh, monkeypatch):
    """A stranger already on the wanted port used to be reported as ours — and
    it never self-corrected, because the stranger kept answering."""
    import socket as _socket
    from canopy import store, webserve

    squatter = _socket.socket()
    squatter.bind(("127.0.0.1", 0))
    squatter.listen(1)
    taken = squatter.getsockname()[1]

    def spawn(argv, _dh):
        # What the real child does: bind something else, then record it.
        store.write_json(webserve.state_path(dh), {"pid": 4242, "port": taken + 1})
        return 4242

    monkeypatch.setattr(webserve, "_alive", lambda pid: True)
    monkeypatch.setattr(webserve, "_responds", lambda port: port == taken + 1)
    try:
        info = webserve.start_background(dh, {"serve_port": taken}, spawn=spawn)
        assert info["port"] == taken + 1
    finally:
        squatter.close()
