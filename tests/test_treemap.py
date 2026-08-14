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


def fake_render(repo):
    """Render the real shipped zh templates, no data home involved."""
    from canopy import templates

    def render(name, values):
        path = repo / "templates" / "messages" / "zh" / name
        return templates.render(path.read_text(encoding="utf-8"), values)
    return render


def test_pointer_row_carries_the_link_to_the_next_message(repo):
    tree = deep_tree(6)
    segs = treemap.segments(tree)
    body = treemap.render_body(tree, segs[0], fake_render(repo),
                               segment_link=lambda nid: "https://x/seg2",
                               permalink=lambda ch, ts: "https://x/feed")
    assert "↳ `1.a.i.1.a` 层 4  [<https://x/seg2|接着看>]" in body


def test_a_row_carries_the_alias_the_digest_and_the_thread(repo):
    tree = store.Tree.new("pay", "C1-0", "支付超时", "A君")
    tree.add_child("C1-0", "C1-1", "慢查询", owner="E君")
    body = treemap.render_body(tree, treemap.segments(tree)[0], fake_render(repo),
                               states={"C1-1": {"channel": "C1",
                                                "raw_permalink": "https://x/p1",
                                                "feed_ts": ["1.0"]}},
                               permalink=lambda ch, ts: "https://x/feed")
    row = [l for l in body.splitlines() if "慢查询" in l][0]
    assert row.startswith("    ◦ `1.a` 慢查询")
    assert "[<https://x/feed|智能总结>] [<https://x/p1|全文>]" in row
    # Owner, counts and lock state are deliberately absent: this message is only
    # re-rendered when the tree changes shape, so live fields would go stale.
    assert "E君" not in row


def test_an_untracked_node_is_marked_but_kept(repo):
    tree = store.Tree.new("pay", "C1-0", "支付超时", "A君")
    tree.add_child("C1-0", "C1-1", "旧方向")
    tree.set_status("C1-1", "untracked")
    body = treemap.render_body(tree, treemap.segments(tree)[0], fake_render(repo),
                               states={"C1-1": {"channel": "C1",
                                                "raw_permalink": "https://x/p1",
                                                "feed_ts": ["1.0"]}},
                               permalink=lambda ch, ts: "https://x/feed")
    row = [l for l in body.splitlines() if "旧方向" in l][0]
    assert row.strip().startswith("× `1.a`")
