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
    assert store.slugify("支付超时") == "tree"          # falls back, never empty


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


def test_stale_lock_is_broken(tmp_path):
    locks.acquire(tmp_path, pid=1234, now=100, alive=lambda pid: True)
    # Older than the staleness timeout: a worker that died without releasing
    # must not park a node forever.
    assert locks.acquire(tmp_path, pid=5678, now=100 + 3600, stale_after=1800,
                         alive=lambda pid: True)


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
