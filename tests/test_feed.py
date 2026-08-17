from canopy import feed as feed_mod
from canopy import ops, store


def entries_of(ctx, proj_id, nid):
    return feed_mod.load_segments(ctx.node_dir(proj_id, nid))


def test_append_updates_the_live_segment_in_place(ctx, slack, tracked):
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    ops.append_checkpoint(ctx, proj_id, nid, "决定先加索引", author="E君")
    ops.append_checkpoint(ctx, proj_id, nid, "DBA 说周四能上", author="F君")

    segments = entries_of(ctx, proj_id, nid)
    assert len(segments) == 1
    assert len(segments[0]["entries"]) == 2
    # Both checkpoints live in the ONE feed message an observer pinned.
    feed_updates = [u for u in slack.updates if u["ts"] == tracked["feed_ts"]]
    assert len(feed_updates) == 2
    assert "决定先加索引" in slack.text_of(tracked["feed_ts"])


def test_an_entry_is_one_line_of_content(ctx, tracked):
    """No author, no date, no per-entry link: the header links the thread, and
    three pieces of provenance per line bury the one piece of content."""
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    ops.append_checkpoint(ctx, proj_id, nid, "决定先加索引", author="E君",
                          raw_permalink="https://x/p1")
    text = entries_of(ctx, proj_id, nid)[0]["entries"][0]
    assert text == "• 决定先加索引"


def test_full_segment_is_sealed_and_a_new_one_opens(ctx, slack, tracked):
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    ctx.cfg["feed_segment_max_chars"] = 150

    for i in range(12):
        ops.append_checkpoint(ctx, proj_id, nid, "checkpoint 编号 %02d" % i)

    segments = entries_of(ctx, proj_id, nid)
    assert len(segments) >= 2
    assert segments[0]["sealed"] is True
    assert segments[-1]["sealed"] is False

    sealed_text = slack.text_of(segments[0]["ts"])
    assert "第 2 段" in sealed_text          # pointer stamped onto the sealed one

    state = store.load_state(ctx.dh, proj_id, nid)
    assert state["feed_ts"] == [s["ts"] for s in segments]


def test_a_sealed_segment_is_never_edited_again(ctx, slack, tracked):
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    ctx.cfg["feed_segment_max_chars"] = 150
    for i in range(12):
        ops.append_checkpoint(ctx, proj_id, nid, "checkpoint 编号 %02d" % i)

    segments = entries_of(ctx, proj_id, nid)
    sealed_ts = segments[0]["ts"]
    edits_after_seal = [u for u in slack.updates if u["ts"] == sealed_ts]
    last_seal_index = max(i for i, u in enumerate(slack.updates)
                          if u["ts"] == sealed_ts)
    later = [u for u in slack.updates[last_seal_index + 1:] if u["ts"] == sealed_ts]
    assert edits_after_seal and not later


def test_one_oversized_entry_still_gets_posted(ctx, tracked):
    """An entry longer than the cap must not loop forever opening segments."""
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    ctx.cfg["feed_segment_max_chars"] = 50
    ops.append_checkpoint(ctx, proj_id, nid, "长" * 200)
    segments = entries_of(ctx, proj_id, nid)
    assert len(segments) == 1 and len(segments[0]["entries"]) == 1


def test_recalibrate_rewrites_every_segment(ctx, slack, tracked):
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    ctx.cfg["feed_segment_max_chars"] = 150
    for i in range(12):
        ops.append_checkpoint(ctx, proj_id, nid, "旧 checkpoint %02d" % i)

    tree = ctx.tree(proj_id)
    state = store.load_state(ctx.dh, proj_id, nid)
    feed = ctx.feed(proj_id, state, tree)
    rebuilt = feed.rebuild([feed.render_entry("重建后的 %d" % i) for i in range(4)])

    assert sum(len(s["entries"]) for s in rebuilt) == 4
    assert "旧 checkpoint" not in slack.text_of(rebuilt[0]["ts"])
    assert "重建后的 0" in slack.text_of(rebuilt[0]["ts"])
