import json

import pytest

from canopy import locks, store
from canopy.errors import LockedError


def test_write_json_is_atomic(tmp_path):
    path = tmp_path / "tree.json"
    store.write_json(path, {"a": 1})
    store.write_json(path, {"a": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 2}
    # No temp files left behind for the next tick to trip over.
    assert [p.name for p in tmp_path.iterdir()] == ["tree.json"]


def test_node_id_roundtrip():
    nid = store.node_id("C0PAY", "1699.0042")
    assert store.split_node_id(nid) == ("C0PAY", "1699.0042")


def test_slugify_makes_a_human_project_id():
    assert store.slugify("Pay timeout / EDD!!") == "pay-timeout-edd"
    assert store.slugify("支付超时") == "支付超时"      # unicode survives
    assert store.slugify("///") == "tree"              # never empty


def test_tree_edges_and_walks():
    tree = store.Tree.new("p", "r", "root", "A君")
    tree.add_child("r", "c1", "child one")
    tree.add_child("c1", "g1", "grandchild")
    assert tree.children("r") == ["c1"]
    assert tree.ancestors("g1") == ["r", "c1"]
    assert tree.descendants("r") == ["c1", "g1"]


def test_add_child_twice_is_refused():
    tree = store.Tree.new("p", "r", "root", "A君")
    tree.add_child("r", "c1", "child")
    with pytest.raises(ValueError):
        tree.add_child("r", "c1", "child again")


def test_unknown_status_is_refused():
    tree = store.Tree.new("p", "r", "root", "A君")
    with pytest.raises(ValueError):
        tree.set_status("r", "sleeping")


def test_lock_blocks_a_second_worker(tmp_path):
    locks.acquire(tmp_path, pid=1234, now=100, alive=lambda pid: True)
    with pytest.raises(LockedError):
        locks.acquire(tmp_path, pid=5678, now=101, alive=lambda pid: True)


def test_a_live_worker_keeps_its_lock_for_as_long_as_it_plausibly_runs(tmp_path):
    """`recalibrate` can legitimately run for hours, so a 30-minute staleness
    rule must not break its lock — but pids get recycled, so there is still an
    upper bound, or a SIGKILLed worker parks the node forever."""
    locks.acquire(tmp_path, pid=1234, now=100, alive=lambda pid: True)
    assert locks.is_held(tmp_path, now=100 + 3 * 3600, stale_after=1800,
                         alive=lambda pid: True)
    assert not locks.is_held(tmp_path, now=100 + 7 * 3600, stale_after=1800,
                             alive=lambda pid: True)


def test_eperm_means_the_process_exists(tmp_path):
    """`os.kill(pid, 0)` on someone else's process raises PermissionError;
    reading that as "dead" let two workers into one node."""
    import errno
    import os as _os

    def kill(pid, sig):
        raise PermissionError(errno.EPERM, "not yours")

    real = _os.kill
    _os.kill = kill
    try:
        assert locks._alive(4242) is True
    finally:
        _os.kill = real


def test_a_pidless_lock_is_broken_once_stale(tmp_path):
    locks.lock_path(tmp_path).write_text('{"started": 100}', encoding="utf-8")
    assert locks.is_held(tmp_path, now=200, stale_after=1800)
    assert not locks.is_held(tmp_path, now=100 + 3600, stale_after=1800)


def test_release_never_removes_someone_elses_lock(tmp_path):
    locks.acquire(tmp_path, pid=1234, now=100, alive=lambda pid: True)
    assert locks.release(tmp_path, pid=9999) is False
    assert locks.lock_path(tmp_path).exists()
    assert locks.release(tmp_path, pid=1234) is True


def test_dead_process_lock_is_broken(tmp_path):
    locks.acquire(tmp_path, pid=1234, now=100, alive=lambda pid: True)
    assert locks.acquire(tmp_path, pid=5678, now=101, alive=lambda pid: False)


def test_held_context_releases_on_exception(tmp_path):
    with pytest.raises(RuntimeError):
        with locks.held(tmp_path, pid=1, now=100, alive=lambda pid: True):
            raise RuntimeError("worker blew up")
    assert not locks.lock_path(tmp_path).exists()


def test_truncated_lock_holds_until_it_goes_stale(tmp_path):
    """Unreadable lock: wait it out. Two workers in one thread is the worse bug."""
    locks.lock_path(tmp_path).write_text("{oh no", encoding="utf-8")
    assert locks.is_held(tmp_path, now=100, stale_after=1800)
    assert not locks.is_held(tmp_path, now=3600, stale_after=1800)
