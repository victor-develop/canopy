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


def test_paused_node_is_skipped_entirely(ctx, slack, tracked):
    ops.set_status(ctx, tracked["proj_id"], tracked["node_id"], "paused")
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

    def fake_run(cfg, prompt, node_dir, out_file=None):
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

    def fake_run(cfg, prompt, node_dir, out_file=None):
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


def test_done_typed_in_the_thread_updates_status(ctx, slack, tracked):
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "@canopy done 上线了")
    tick_mod.tick(ctx, run=no_llm)
    assert ctx.tree(tracked["proj_id"]).node(tracked["node_id"])["status"] == "done"


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


def test_classify_prefers_a_command_over_a_question():
    messages = [{"ts": "1", "user": "U", "text": "@canopy 这个怎么办"},
                {"ts": "2", "user": "U", "text": "@canopy fork 慢查询"}]
    kind, detail = worker.classify(messages, ["canopy"])
    assert kind == "command" and detail["command"] == "fork"


def test_classify_falls_through_to_chatter():
    kind, _ = worker.classify([{"ts": "1", "user": "U", "text": "hi"}], ["canopy"])
    assert kind == "chatter"
