"""The tree message: one map, segmented every 4 levels, cross-linked."""

from canopy import ops, store, treemap


def deep_tree(levels):
    tree = store.Tree.new("pay", "C1-0", "支付超时", "A君")
    parent = "C1-0"
    for i in range(1, levels):
        nid = "C1-%d" % i
        tree.add_child(parent, nid, "层 %d" % i)
        parent = nid
    return tree


def test_a_shallow_tree_is_one_message():
    segs = treemap.segments(deep_tree(4))
    assert len(segs) == 1
    assert [nid for nid, _d, ptr in segs[0]["rows"] if not ptr] == \
        ["C1-0", "C1-1", "C1-2", "C1-3"]


def test_the_fifth_level_opens_a_second_message():
    segs = treemap.segments(deep_tree(6))
    assert len(segs) == 2
    # The cut node stays in message 1 as a pointer, and owns message 2.
    assert segs[0]["rows"][-1] == ("C1-4", 4, True)
    assert segs[1]["root"] == "C1-4"
    assert [nid for nid, _d, ptr in segs[1]["rows"] if not ptr] == ["C1-4", "C1-5"]


def test_every_four_levels_cuts_again():
    segs = treemap.segments(deep_tree(13))
    assert [s["root"] for s in segs] == ["C1-0", "C1-4", "C1-8", "C1-12"]


def test_segment_of_finds_where_a_node_is_drawn():
    tree = deep_tree(6)
    assert treemap.segment_of(tree, "C1-3") == 1
    assert treemap.segment_of(tree, "C1-4") == 2      # its row, not its pointer


def test_pointer_row_carries_the_link_to_the_next_message():
    tree = deep_tree(6)
    segs = treemap.segments(tree)
    body = treemap.render_body(tree, segs[0],
                               segment_link=lambda nid: "https://x/seg2")
    assert "↳ `1.a.i.1.a` 层 4 — <https://x/seg2|接着看>" in body


def test_rows_show_status_owner_and_links():
    tree = store.Tree.new("pay", "C1-0", "支付超时", "A君")
    tree.add_child("C1-0", "C1-1", "慢查询", owner="E君")
    tree.set_status("C1-1", "done")
    body = treemap.render_body(tree, treemap.segments(tree)[0], states={
        "C1-1": {"channel": "C1", "raw_permalink": "https://x/p1",
                 "feed_ts": ["1.0"]},
    }, permalink=lambda ch, ts: "https://x/feed")
    assert "✔ `1.a` 慢查询" in body
    assert "<https://x/p1|thread>" in body and "<https://x/feed|feed>" in body
    assert "E君" in body and "done" in body


# -- posting side ------------------------------------------------------------

def test_track_posts_one_tree_message(ctx, slack, tracked):
    tree = ctx.tree(tracked["proj_id"])
    msgs = tree.data["tree_msgs"]
    assert len(msgs) == 1
    text = slack.text_of(msgs[0]["ts"])
    assert "整棵树" in text and "`1`" in text
    assert "1 个节点" in text


def test_the_tree_message_updates_in_place_on_fork(ctx, slack, tracked):
    proj_id = tracked["proj_id"]
    before = ctx.tree(proj_id).data["tree_msgs"][0]["ts"]
    ops.fork(ctx, proj_id, tracked["node_id"], "慢查询定位")
    tree = ctx.tree(proj_id)
    assert [m["ts"] for m in tree.data["tree_msgs"]] == [before]  # no new message
    assert "慢查询定位" in slack.text_of(before)


def test_a_deep_fork_chain_opens_a_second_tree_message(ctx, slack, tracked):
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    for title in ("层1", "层2", "层3", "层4"):
        nid = ops.fork(ctx, proj_id, nid, title)["node_id"]

    tree = ctx.tree(proj_id)
    msgs = tree.data["tree_msgs"]
    assert len(msgs) == 2
    first, second = slack.text_of(msgs[0]["ts"]), slack.text_of(msgs[1]["ts"])
    # Message 1 points down, message 2 points back up: the tree stays walkable.
    assert "接着看" in first
    assert "接 <" in second and "上一段" in second


def test_messages_link_to_the_segment_that_holds_the_node(ctx, tracked):
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    for title in ("层1", "层2", "层3", "层4"):
        nid = ops.fork(ctx, proj_id, nid, title)["node_id"]
    tree = ctx.tree(proj_id)
    deep = treemap.permalink(tree, ctx.cfg, nid)
    shallow = treemap.permalink(tree, ctx.cfg, tree.root)
    assert deep != shallow
