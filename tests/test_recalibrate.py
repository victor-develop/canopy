"""Loop C: the heavy escape hatch, and the reason the cheap path stays cheap."""

from canopy import feed as feed_mod
from canopy import ops, store, worker


def seed_history(slack, state, count):
    for i in range(count):
        slack.add(state["channel"], state["thread_ts"],
                  "17000%05d.000100" % i, "U%d" % (i % 3), "消息 %d" % i)


def test_recalibrate_reads_history_in_chunks(ctx, slack, tracked):
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    state = store.load_state(ctx.dh, proj_id, nid)
    seed_history(slack, state, 25)

    chunks = []

    def fake_run(cfg, prompt, node_dir, out_file=None, **kw):
        chunks.append(prompt)
        return "第 %d 段结论" % len(chunks)

    result = worker.recalibrate(ctx, proj_id, nid, chunk_size=10, run=fake_run)
    # 26 messages (the tracked root plus 25) at 10 per chunk, then one last
    # call that boils the checkpoints down to the digest.
    assert len(chunks) == 3 + 1
    assert "Every checkpoint recorded" in chunks[-1]
    assert result["checkpoints"] == 3
    segments = feed_mod.load_segments(ctx.node_dir(proj_id, nid))
    assert sum(len(s["entries"]) for s in segments) == 3


def test_recalibrate_carries_earlier_notes_into_later_chunks(ctx, slack, tracked):
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    state = store.load_state(ctx.dh, proj_id, nid)
    seed_history(slack, state, 25)
    prompts = []

    def fake_run(cfg, prompt, node_dir, out_file=None, **kw):
        prompts.append(prompt)
        return "结论 %d" % len(prompts)

    worker.recalibrate(ctx, proj_id, nid, chunk_size=10, run=fake_run)
    assert "结论 1" in prompts[1]        # compressed forward, not re-read
    assert "结论 2" in prompts[2]


def test_recalibrate_overwrites_what_the_cheap_path_wrote(ctx, slack, tracked):
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    ops.append_checkpoint(ctx, proj_id, nid, "错的 checkpoint")
    state = store.load_state(ctx.dh, proj_id, nid)
    seed_history(slack, state, 3)

    worker.recalibrate(ctx, proj_id, nid, chunk_size=10,
                       run=lambda *a, **k: "对的 checkpoint")
    text = slack.text_of(tracked["feed_ts"])
    assert "对的 checkpoint" in text and "错的 checkpoint" not in text


def test_a_chunk_can_add_nothing(ctx, slack, tracked):
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    state = store.load_state(ctx.dh, proj_id, nid)
    seed_history(slack, state, 5)
    result = worker.recalibrate(ctx, proj_id, nid, chunk_size=2,
                                run=lambda *a, **k: "SKIP")
    assert result["checkpoints"] == 0


def test_return_with_no_text_draws_on_the_feed(ctx, tracked):
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    ops.append_checkpoint(ctx, proj_id, nid, "决定加索引")
    summary = worker.summarize_for_return(ctx, proj_id, nid)
    assert "决定加索引" in summary


def test_return_with_an_empty_feed_says_so(ctx, tracked):
    summary = worker.summarize_for_return(ctx, tracked["proj_id"],
                                          tracked["node_id"])
    assert "no checkpoints" in summary


def test_recalibrate_also_rewrites_the_digest(ctx, slack, tracked):
    """The per-tick summarizer keeps the digest current as it goes; without
    this the heavy path left it exactly as stale as it found it."""
    from canopy import prompts
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    state = store.load_state(ctx.dh, proj_id, nid)
    seed_history(slack, state, 5)
    prompts.write_digest(ctx.node_dir(proj_id, nid), "过时的现状")

    def fake_run(cfg, prompt, node_dir, out_file=None, **kw):
        if "Every checkpoint recorded" in prompt:
            return "支付超时在查慢查询,等 DBA 确认索引方案。"
        return "定位到订单表"

    result = worker.recalibrate(ctx, proj_id, nid, chunk_size=10, run=fake_run)
    assert result["digest"] is True
    digest = prompts.read_digest(ctx.node_dir(proj_id, nid))
    assert "等 DBA 确认索引方案" in digest and "过时" not in digest


def test_recalibrate_leaves_the_digest_alone_when_there_is_nothing_to_say(
        ctx, slack, tracked):
    from canopy import prompts
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    state = store.load_state(ctx.dh, proj_id, nid)
    seed_history(slack, state, 3)
    prompts.write_digest(ctx.node_dir(proj_id, nid), "先前写好的现状")

    result = worker.recalibrate(ctx, proj_id, nid, chunk_size=10,
                                run=lambda *a, **k: "SKIP")
    # Every chunk skipped: no checkpoints, so no digest call and no clobbering.
    assert result["checkpoints"] == 0 and result["digest"] is False
    assert prompts.read_digest(ctx.node_dir(proj_id, nid)) == "先前写好的现状"


def test_the_digest_is_told_how_the_thread_opened(ctx, slack, tracked):
    """Checkpoints record progress, so the message stating the problem earns
    none — and a digest built from checkpoints alone came out as "now in the
    feedback-validation phase", which says nothing about what is being
    validated."""
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    state = store.load_state(ctx.dh, proj_id, nid)
    seed_history(slack, state, 3)
    seen = []

    def fake_run(cfg, prompt, node_dir, out_file=None, **kw):
        seen.append(prompt)
        return "结论一句"

    worker.recalibrate(ctx, proj_id, nid, chunk_size=10, run=fake_run)
    digest_call = [p for p in seen if "Every checkpoint recorded" in p][0]
    assert "How this thread opened" in digest_call
    assert "支付超时" in digest_call        # the tracked root's opening message
