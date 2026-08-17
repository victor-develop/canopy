import pytest

from canopy import treemap as treemap_mod
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

    # Three posts: the tree message, the feed message, the in-thread announce.
    assert len(slack.posted) == 3
    tree_msg, feed_post, announce = slack.posted
    assert ctx.tree(proj_id).data["tree_msgs"][0]["ts"] == tree_msg["ts"]
    assert feed_post["thread_ts"] is None
    assert announce["thread_ts"] == state["thread_ts"]
    # One line, two links: the trace tree and the digest. Anything longer gets
    # skimmed past in a thread people are already arguing in.
    assert announce["text"].startswith("[canopy]:")
    assert "|跟踪>" in announce["text"] and "|智能摘要>" in announce["text"]


def test_track_seeds_profiles_and_messages(ctx, tracked):
    assert (paths.profiles_dir(ctx.dh) / "canopy.md").exists()
    assert (paths.messages_dir(ctx.dh, "zh") / "feed-root.md").exists()


def test_track_keeps_messages_posted_while_it_was_announcing(ctx, slack, tracked):
    """The cursor lands on what was in the thread before Canopy spoke, not on
    Canopy's own announce — anything said during those round-trips used to be
    skipped for good. The announce is filtered by its identity prefix instead."""
    from canopy import mentions
    state = store.load_state(ctx.dh, tracked["proj_id"], tracked["node_id"])
    assert state["cursor"] != tracked["announce_ts"]
    announce = [p for p in slack.posted if p["ts"] == tracked["announce_ts"]][0]
    assert mentions.is_own_post(announce["text"], ["canopy"])


def test_track_refuses_to_track_the_same_project_twice(ctx, slack, tracked):
    link = "https://example.slack.com/archives/C0PAY/p1699000001000100"
    with pytest.raises(ValueError):
        ops.track(ctx, link, title=tracked["title"], proj_id=tracked["proj_id"])


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

    kickoff, feed_post, announce = slack.posted[3:]
    assert kickoff["thread_ts"] is None          # kickoff starts a NEW thread
    assert feed_post["thread_ts"] is None        # the child's feed sits in the channel
    assert announce["thread_ts"] == tracked["node_id"].split("-")[1]


def test_fork_of_a_fork_nests(ctx, tracked):
    proj_id, root = tracked["proj_id"], tracked["node_id"]
    child = ops.fork(ctx, proj_id, root, "慢查询定位")
    grand = ops.fork(ctx, proj_id, child["node_id"], "索引方案")
    assert grand["alias"] == "1.a.i"


def test_tree_message_marks_status_and_links(ctx, slack, tracked):
    proj_id = tracked["proj_id"]
    child = ops.fork(ctx, proj_id, tracked["node_id"], "慢查询定位")
    ops.set_status(ctx, proj_id, child["node_id"], "untracked", reason="先放着")

    tree = ctx.tree(proj_id)
    map_ts = tree.data["tree_msgs"][0]["ts"]
    text = slack.text_of(map_ts)
    assert "× `1.a` 慢查询定位" in text
    assert "|智能摘要>" in text and "|全文>" in text


def test_status_change_posts_into_the_feed_thread(ctx, slack, tracked):
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    ops.set_status(ctx, proj_id, nid, "untracked", reason="等 DBA")
    last = slack.posted[-1]
    assert last["thread_ts"] == tracked["feed_ts"]
    assert "不再跟踪" in last["text"] and "等 DBA" in last["text"]


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


def test_chinese_title_keeps_a_usable_proj_id():
    assert store.slugify("设计师不用 Figma 的方案") == "设计师不用-figma-的方案"
    assert store.slugify("!!!") == "tree"


def test_second_tree_with_a_colliding_slug_gets_a_suffix(ctx, slack, tracked):
    channel, ts = "C0EDD", "1699000500.000100"
    slack.add(channel, ts, ts, "U1", tracked["title"])   # same title, other thread
    link = "https://example.slack.com/archives/%s/p%s" % (channel, ts.replace(".", ""))
    second = ops.track(ctx, link, namer=lambda *a, **k: "pay-timeout")
    assert second["proj_id"] == tracked["proj_id"] + "-2"


def test_tracking_the_same_thread_twice_is_refused(ctx, tracked):
    link = "https://example.slack.com/archives/C0PAY/p1699000001000100"
    with pytest.raises(ValueError) as exc:
        ops.track(ctx, link, proj_id="another-name")
    assert "already tracked" in str(exc.value)


def test_rename_reaches_the_tree_the_state_and_the_feed(ctx, slack, tracked):
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    ops.rename(ctx, proj_id, nid, "支付超时:重试风暴")

    assert ctx.tree(proj_id).node(nid)["title"] == "支付超时:重试风暴"
    assert store.load_state(ctx.dh, proj_id, nid)["title"] == "支付超时:重试风暴"
    assert "支付超时:重试风暴" in slack.text_of(tracked["feed_ts"])
    map_ts = ctx.tree(proj_id).data["tree_msgs"][0]["ts"]
    assert "支付超时:重试风暴" in slack.text_of(map_ts)


def test_rename_keeps_the_checkpoints_already_recorded(ctx, slack, tracked):
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    ops.append_checkpoint(ctx, proj_id, nid, "决定加索引")
    ops.rename(ctx, proj_id, nid, "新标题")
    assert "决定加索引" in slack.text_of(tracked["feed_ts"])


def test_the_child_thread_gets_a_link_to_its_own_feed(ctx, slack, tracked):
    """The kickoff is posted before the feed exists, so it is edited afterwards.

    Without the edit, the child thread is the one place in the tree with no way
    to reach its own digest.
    """
    result = ops.fork(ctx, tracked["proj_id"], tracked["node_id"], "慢查询定位")
    kickoff = slack.text_of(result["thread_ts"])
    feed_url = ctx.permalink("C0PAY", result["feed_ts"])
    assert "|智能摘要>" in kickoff and feed_url in kickoff
    # And it still points up and out — every canopy message uses the same
    # skeleton: 正在[跟踪]对话并进行 [智能摘要].
    assert "|上游>" in kickoff and "|跟踪>" in kickoff


def test_the_kickoff_is_readable_before_the_feed_exists(ctx, slack, tracked):
    posted = []
    original_post = slack.post

    def capture(channel, text, thread_ts=None):
        posted.append(text)
        return original_post(channel, text, thread_ts=thread_ts)

    slack.post = capture
    ops.fork(ctx, tracked["proj_id"], tracked["node_id"], "慢查询定位")
    kickoff_as_posted = posted[0]
    # Empty link degrades to its label rather than posting `<|智能摘要>`.
    assert "智能摘要" in kickoff_as_posted and "<|" not in kickoff_as_posted


def test_the_child_thread_says_what_it_is_about(ctx, slack, tracked):
    """Someone landing in the child thread should not have to go back up to
    learn what it is for."""
    result = ops.fork(ctx, tracked["proj_id"], tracked["node_id"], "慢查询定位")
    assert "慢查询定位" in slack.text_of(result["thread_ts"])


def test_a_template_that_hides_the_identity_prefix_is_refused(ctx, tracked):
    """Canopy skips its own messages by the `[agent]` prefix. Edit that away and
    it answers itself every tick, burning a worker each time — so fail here, on
    the machine of whoever just edited the template."""
    user_template = paths.messages_dir(ctx.dh, "zh") / "reply.md"
    user_template.write_text("---\nmoment: reply\nvars: [agent, body]\n---\n"
                             "{{body}} —— {{agent}}\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        ops.reply(ctx, tracked["proj_id"], tracked["node_id"], "查到了")
    assert "reply to itself" in str(exc.value)


def test_fork_without_a_title_answers_through_a_template(ctx, slack, tracked):
    from canopy import worker
    state = store.load_state(ctx.dh, tracked["proj_id"], tracked["node_id"])
    slack.add(state["channel"], state["thread_ts"], "1700001000.000100", "U2",
              "@canopy fork")
    worker.handle(ctx, tracked["proj_id"], tracked["node_id"],
                  [{"ts": "1700001000.000100", "user": "U2", "text": "@canopy fork"}])
    assert "fork" in slack.posted[-1]["text"] and "标题" in slack.posted[-1]["text"]
