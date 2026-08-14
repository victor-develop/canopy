import pytest

from canopy import canvas as canvas_mod
from canopy import noderef, ops, paths, store
from canopy.slack import parse_thread_link


def test_parse_thread_link_top_level():
    assert parse_thread_link(
        "https://x.slack.com/archives/C0PAY/p1699000001000100") == \
        ("C0PAY", "1699000001.000100")


def test_parse_thread_link_reply_uses_thread_ts():
    channel, ts = parse_thread_link(
        "https://x.slack.com/archives/C0PAY/p1699000500000200?thread_ts=1699000001.000100")
    assert (channel, ts) == ("C0PAY", "1699000001.000100")


def test_track_creates_tree_state_feed_and_announce(ctx, slack, tracked):
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    tree = ctx.tree(proj_id)
    assert tree.root == nid
    state = store.load_state(ctx.dh, proj_id, nid)
    assert state["status"] == "active"
    assert state["feed_ts"] == [tracked["feed_ts"]]

    # Two posts: the feed message in the channel, the announce in the thread.
    assert len(slack.posted) == 2
    feed_post, announce = slack.posted
    assert feed_post["thread_ts"] is None
    assert announce["thread_ts"] == state["thread_ts"]
    assert "@canopy" in announce["text"]          # how anyone but A君 finds fork/guide


def test_track_seeds_profiles_and_messages(ctx, tracked):
    assert (paths.profiles_dir(ctx.dh) / "canopy.md").exists()
    assert (paths.messages_dir(ctx.dh, "zh") / "feed-root.md").exists()


def test_track_cursor_skips_its_own_announce(ctx, slack, tracked):
    """Otherwise the very next tick would wake a worker on Canopy's own message."""
    state = store.load_state(ctx.dh, tracked["proj_id"], tracked["node_id"])
    assert state["cursor"] == tracked["announce_ts"]


def test_track_refuses_to_track_the_same_project_twice(ctx, slack, tracked):
    link = "https://example.slack.com/archives/C0PAY/p1699000001000100"
    with pytest.raises(ValueError):
        ops.track(ctx, link, title=tracked["title"])


def test_fork_writes_the_edge_and_opens_a_child_thread(ctx, slack, tracked):
    proj_id, root = tracked["proj_id"], tracked["node_id"]
    result = ops.fork(ctx, proj_id, root, "慢查询定位")

    tree = ctx.tree(proj_id)
    assert tree.children(root) == [result["node_id"]]
    assert tree.parent(result["node_id"]) == root
    assert result["alias"] == "1.a"

    child_state = store.load_state(ctx.dh, proj_id, result["node_id"])
    assert child_state["parent"] == root
    assert child_state["feed_ts"] == [result["feed_ts"]]

    kinds = [(p["thread_ts"] is None, p["text"][:12]) for p in slack.posted[2:]]
    assert kinds[0][0] is True        # kickoff starts a NEW thread
    assert slack.posted[-1]["thread_ts"] == tracked["node_id"].split("-")[1]


def test_fork_of_a_fork_nests(ctx, tracked):
    proj_id, root = tracked["proj_id"], tracked["node_id"]
    child = ops.fork(ctx, proj_id, root, "慢查询定位")
    grand = ops.fork(ctx, proj_id, child["node_id"], "索引方案")
    assert grand["alias"] == "1.a.i"


def test_canvas_marks_status_and_links(ctx, tracked):
    proj_id = tracked["proj_id"]
    child = ops.fork(ctx, proj_id, tracked["node_id"], "慢查询定位")
    ops.set_status(ctx, proj_id, child["node_id"], "done", reason="上线了")

    text = canvas_mod.canvas_path(ctx.dh, proj_id).read_text(encoding="utf-8")
    assert "✔ `1.a` 慢查询定位" in text
    assert "[thread](https://example.slack.com/archives/" in text


def test_status_change_posts_into_the_feed_thread(ctx, slack, tracked):
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    ops.set_status(ctx, proj_id, nid, "paused", reason="等 DBA")
    last = slack.posted[-1]
    assert last["thread_ts"] == tracked["feed_ts"]
    assert "paused" in last["text"] and "等 DBA" in last["text"]


def test_guide_appends_and_only_reacts(ctx, slack, tracked):
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    posts_before = len(slack.posted)
    ops.guide(ctx, proj_id, nid, "只记 DB 侧结论", message_ts="1699000900.000100")

    guide = (ctx.node_dir(proj_id, nid) / "guide.md").read_text(encoding="utf-8")
    assert "只记 DB 侧结论" in guide
    assert len(slack.posted) == posts_before        # no message, ever
    assert slack.reactions[-1]["emoji"] == "white_check_mark"


def test_return_then_ack_posts_into_the_parent_thread(ctx, slack, tracked):
    proj_id, root = tracked["proj_id"], tracked["node_id"]
    child = ops.fork(ctx, proj_id, root, "慢查询定位")
    nid = child["node_id"]

    draft = ops.return_draft(ctx, proj_id, nid, "结论:加复合索引")
    assert slack.posted[-1]["thread_ts"] is None     # a NEW message, for review
    state = store.load_state(ctx.dh, proj_id, nid)
    assert state["return_draft"]["ts"] == draft["ts"]

    ops.ack_return(ctx, proj_id, nid)
    posted = slack.posted[-1]
    root_ts = store.load_state(ctx.dh, proj_id, root)["thread_ts"]
    assert posted["thread_ts"] == root_ts
    assert "加复合索引" in posted["text"]
    assert "return_draft" not in store.load_state(ctx.dh, proj_id, nid)


def test_ack_without_a_draft_refuses(ctx, tracked):
    proj_id, root = tracked["proj_id"], tracked["node_id"]
    child = ops.fork(ctx, proj_id, root, "慢查询定位")
    with pytest.raises(ValueError):
        ops.ack_return(ctx, proj_id, child["node_id"])


def test_root_cannot_return(ctx, tracked):
    proj_id, root = tracked["proj_id"], tracked["node_id"]
    ops.return_draft(ctx, proj_id, root, "x")
    with pytest.raises(ValueError):
        ops.ack_return(ctx, proj_id, root)


def test_reply_is_identity_prefixed(ctx, slack, tracked):
    ops.reply(ctx, tracked["proj_id"], tracked["node_id"], "查到了,是索引缺失")
    assert slack.posted[-1]["text"].startswith("*[canopy]*")
