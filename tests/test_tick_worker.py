from canopy import feed as feed_mod
from canopy import locks, ops, store, tick as tick_mod, worker


def add_msg(slack, state, ts, user, text):
    slack.add(state["channel"], state["thread_ts"], ts, user, text)


def state_of(ctx, tracked):
    return store.load_state(ctx.dh, tracked["proj_id"], tracked["node_id"])


def no_llm(*args, **kwargs):
    raise AssertionError("the gate let a node through to an LLM")


def test_idle_node_never_reaches_a_worker(ctx, tracked):
    results = tick_mod.tick(ctx, handle=no_llm)
    assert [r["verdict"] for r in results] == ["no-new"]


def test_untracked_node_is_skipped_entirely(ctx, slack, tracked):
    ops.set_status(ctx, tracked["proj_id"], tracked["node_id"], "untracked")
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "还有人在吗")
    assert tick_mod.tick(ctx, handle=no_llm) == []


def test_locked_node_waits_for_the_next_tick(ctx, slack, tracked):
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "新消息")
    locks.acquire(ctx.node_dir(tracked["proj_id"], tracked["node_id"]),
                  pid=999, now=1700000000.0)
    results = tick_mod.tick(ctx, now=1700000001.0, alive=lambda pid: True,
                            handle=no_llm)
    assert results[0]["verdict"] == "locked"
    # Cursor untouched: the messages get picked up next tick.
    assert state_of(ctx, tracked)["cursor"] == state["cursor"]


def test_chatter_goes_to_the_light_summarizer(ctx, slack, tracked):
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "我觉得是索引问题")
    calls = []

    def fake_run(cfg, prompt, node_dir, out_file=None, **kw):
        calls.append(prompt)
        return "决定先加复合索引"

    results = tick_mod.tick(ctx, run=fake_run)
    assert results[0]["outcome"]["kind"] == "light"
    assert results[0]["outcome"]["appended"] is True
    segments = feed_mod.load_segments(ctx.node_dir(tracked["proj_id"],
                                                   tracked["node_id"]))
    assert "决定先加复合索引" in segments[0]["entries"][0]
    assert "checkpoint" in calls[0].lower()


def test_summarizer_can_decide_nothing_is_worth_recording(ctx, slack, tracked):
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "收到")
    results = tick_mod.tick(ctx, run=lambda *a, **k: "SKIP")
    assert results[0]["outcome"]["appended"] is False
    assert results[0]["cursor"] == "1700001000.000100"   # still advances


def test_mention_wakes_a_full_worker_and_posts_its_reply(ctx, slack, tracked):
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "@canopy 这个能今天出结论吗")
    prompts = []

    def fake_run(cfg, prompt, node_dir, out_file=None, **kw):
        prompts.append(prompt)
        return "今天出不了,缺 DBA 的确认。"

    results = tick_mod.tick(ctx, run=fake_run)
    assert results[0]["outcome"]["posted"] is True
    assert slack.posted[-1]["text"].startswith("*[canopy]*")
    assert slack.posted[-1]["thread_ts"] == state["thread_ts"]
    # The worker sees its node and the increment, not the whole tree.
    assert "@canopy 这个能今天出结论吗" in prompts[0]
    assert "支付超时" in prompts[0]


def test_worker_can_stay_silent(ctx, slack, tracked):
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "@canopy 收到")
    before = len(slack.posted)
    results = tick_mod.tick(ctx, run=lambda *a, **k: "SKIP")
    assert results[0]["outcome"]["posted"] is False
    assert len(slack.posted) == before


def test_fork_typed_in_the_thread_runs_as_code_not_as_the_model(ctx, slack, tracked):
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "@canopy fork 慢查询定位")

    results = tick_mod.tick(ctx, run=no_llm)      # a fork must not need an LLM
    outcome = results[0]["outcome"]
    assert outcome["command"] == "fork" and outcome["alias"] == "1.a"
    tree = ctx.tree(tracked["proj_id"])
    assert tree.children(tracked["node_id"]) == [outcome["node_id"]]


def test_guide_typed_in_the_thread_only_reacts(ctx, slack, tracked):
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2",
            "@canopy guide: 只记 DB 侧结论")
    before = len(slack.posted)
    tick_mod.tick(ctx, run=no_llm)
    assert len(slack.posted) == before
    assert slack.reactions[-1]["ts"] == "1700001000.000100"


def test_untrack_typed_in_the_thread_stops_the_watching(ctx, slack, tracked):
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "@canopy untrack 聊完了")
    tick_mod.tick(ctx, run=no_llm)
    assert ctx.tree(tracked["proj_id"]).node(tracked["node_id"])["status"] == \
        "untracked"


def test_track_typed_in_the_thread_reopens_it(ctx, slack, tracked):
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    ops.set_status(ctx, proj_id, nid, "untracked")
    # An untracked node is skipped by the gate, so the command is executed the
    # way a human would: through the CLI. (In Slack you re-open with
    # `canopy track <ref>`; the thread itself is no longer watched.)
    result = ops.set_status(ctx, proj_id, nid, "active")
    assert result["status"] == "active"


def test_cursor_advances_so_the_next_tick_is_free(ctx, slack, tracked):
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "随便聊聊")
    tick_mod.tick(ctx, run=lambda *a, **k: "SKIP")
    assert state_of(ctx, tracked)["cursor"] == "1700001000.000100"
    assert [r["verdict"] for r in tick_mod.tick(ctx, handle=no_llm)] == ["no-new"]


def test_lock_is_released_even_when_the_worker_blows_up(ctx, slack, tracked):
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "@canopy 看下")

    def boom(*a, **k):
        raise RuntimeError("runner died")

    try:
        tick_mod.tick(ctx, handle=boom)
    except RuntimeError:
        pass
    node_dir = ctx.node_dir(tracked["proj_id"], tracked["node_id"])
    assert not locks.lock_path(node_dir).exists()


def test_classify_keeps_the_command_and_the_question():
    messages = [{"ts": "1", "user": "U", "text": "@canopy 这个怎么办"},
                {"ts": "2", "user": "U", "text": "@canopy fork 慢查询"}]
    plan = worker.classify(messages, ["canopy"])
    assert plan["kind"] == "command"
    assert [c["command"] for c in plan["commands"]] == ["fork"]
    assert plan["question"]["message"]["ts"] == "1"


def test_classify_keeps_every_command_in_order():
    """Two people forking inside one tick window must produce two children."""
    messages = [{"ts": "1", "user": "U", "text": "@canopy fork 甲"},
                {"ts": "2", "user": "V", "text": "@canopy fork 乙"}]
    plan = worker.classify(messages, ["canopy"])
    assert [c["arg"] for c in plan["commands"]] == ["甲", "乙"]


def test_classify_falls_through_to_chatter():
    plan = worker.classify([{"ts": "1", "user": "U", "text": "hi"}], ["canopy"])
    assert plan["kind"] == "chatter" and not plan["commands"]


def test_canopys_own_reply_does_not_wake_a_worker(ctx, slack, tracked):
    """Otherwise every reply costs a summarizer reading Canopy's own words."""
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "UBOT", "*[canopy]* 我查了下日志")
    results = tick_mod.tick(ctx, handle=no_llm)
    assert results[0]["verdict"] == "self-only"
    assert state_of(ctx, tracked)["cursor"] == "1700001000.000100"


def test_a_real_message_after_canopys_own_still_gets_handled(ctx, slack, tracked):
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "UBOT", "*[canopy]* 我查了下日志")
    add_msg(slack, state, "1700001001.000100", "U2", "那就先加索引")
    seen = {}

    def fake_run(cfg, prompt, node_dir, out_file=None, **kw):
        seen["prompt"] = prompt
        return "决定先加索引"

    results = tick_mod.tick(ctx, run=fake_run)
    assert results[0]["verdict"] == "work"
    assert "那就先加索引" in seen["prompt"]
    assert "我查了下日志" not in seen["prompt"]      # its own post is filtered out
    assert state_of(ctx, tracked)["cursor"] == "1700001001.000100"


def test_two_forks_in_one_batch_both_land(ctx, slack, tracked):
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "@canopy fork 慢查询定位")
    add_msg(slack, state, "1700001001.000100", "U3", "@canopy fork 重试风暴")

    tick_mod.tick(ctx, run=no_llm)

    tree = ctx.tree(proj_id)
    titles = [tree.node(c)["title"] for c in tree.children(nid)]
    assert titles == ["慢查询定位", "重试风暴"]


def test_a_bad_command_does_not_kill_the_tick(ctx, slack, tracked):
    """`ack return` with no draft raises ValueError; it must not escape."""
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "@canopy ack return")

    results = tick_mod.tick(ctx, run=no_llm)
    assert results[0]["outcome"]["error"].startswith("ValueError")


def test_a_failing_batch_is_retried_then_skipped(ctx, slack, tracked):
    """Retry, but not forever: the same message must not replay every 5 minutes
    for the rest of the tree's life."""
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    state = state_of(ctx, tracked)
    before = state["cursor"]
    add_msg(slack, state, "1700001000.000100", "U2", "@canopy ack return")

    for _ in range(2):
        tick_mod.tick(ctx, run=no_llm)
        assert state_of(ctx, tracked)["cursor"] == before      # still retrying

    tick_mod.tick(ctx, run=no_llm)
    assert state_of(ctx, tracked)["cursor"] == "1700001000.000100"   # gave up


def test_a_command_is_not_re_executed_when_the_batch_retries(ctx, slack, tracked):
    """`fork` is not idempotent. One fork plus one bad command in the same batch
    used to grow a duplicate child — and three Slack messages — every tick."""
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "@canopy fork 慢查询定位")
    add_msg(slack, state, "1700001001.000100", "U3", "@canopy ack return")

    for _ in range(3):
        tick_mod.tick(ctx, run=no_llm)

    tree = ctx.tree(proj_id)
    titles = [tree.node(c)["title"] for c in tree.children(nid)]
    assert titles == ["慢查询定位"]          # exactly one, after three ticks


def test_a_busy_thread_still_gives_up_on_a_poison_message(ctx, slack, tracked):
    """The retry counter keys on the oldest message in the failing batch. Keyed
    on the newest, a thread where people keep talking never gave up: the cursor
    froze and every tick re-read a larger batch."""
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "@canopy ack return")

    for i in range(4):
        add_msg(slack, state, "17000010%02d.000100" % (10 + i), "U3", "继续聊")
        tick_mod.tick(ctx, run=lambda *a, **k: "SKIP")

    cursor = state_of(ctx, tracked)["cursor"]
    assert float(cursor) > 1700001000.000100      # stepped over the poison


def test_only_one_tick_runs_at_a_time(ctx, slack, tracked):
    """A tick can outrun the cron interval (a worker holds a node for its whole
    timeout), so without this ticks stack and each pays for the same work."""
    from canopy import locks
    # Real wall clock: the lock has an upper age bound, so a lock stamped with
    # the fixture's frozen 2023 timestamp would read as ancient and be broken.
    locks.acquire(ctx.dh, pid=999, alive=lambda pid: True)
    try:
        results = tick_mod.tick(ctx, alive=lambda pid: True, handle=no_llm)
        assert results == [{"verdict": "tick-already-running"}]
    finally:
        locks.release(ctx.dh, pid=999)


def test_the_tick_lock_is_released_afterwards(ctx, tracked):
    from canopy import locks
    tick_mod.tick(ctx, handle=no_llm)
    assert not locks.lock_path(ctx.dh).exists()
