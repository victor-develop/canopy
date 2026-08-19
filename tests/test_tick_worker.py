from canopy import feed as feed_mod
from canopy import locks, ops, store, tick as tick_mod, worker


def add_msg(slack, state, ts, user, text):
    slack.add(state["channel"], state["thread_ts"], ts, user, text)


def state_of(ctx, tracked):
    return store.load_state(ctx.dh, tracked["proj_id"], tracked["node_id"])


def no_llm(*args, **kwargs):
    raise AssertionError("the gate let a node through to an LLM")


def test_idle_node_never_reaches_a_worker(ctx, tracked):
    """`self-only` here means the only thing since the cursor is Canopy's own
    announce — still zero tokens, still no worker."""
    results = tick_mod.tick(ctx, handle=no_llm)
    assert [r["verdict"] for r in results] == ["self-only"]


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


def test_canopy_does_not_wake_the_parent_with_its_own_fork_announce(ctx, slack,
                                                                   tracked):
    """The announce lands in the parent's thread, before the parent's cursor.
    If Canopy cannot recognise it, the parent wakes on it every tick forever."""
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "@canopy fork 慢查询定位")
    tick_mod.tick(ctx, run=no_llm)          # the fork, plus its announce

    results = tick_mod.tick(ctx, handle=no_llm)   # would raise if it woke a worker

    parent = [r for r in results if r["node"] == nid][0]
    assert parent["verdict"] in ("self-only", "no-new")


def test_canopy_does_not_answer_its_own_reply(ctx, slack, tracked):
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "@canopy 这个怎么办")
    tick_mod.tick(ctx, run=lambda *a, **k: "我的答复")

    results = tick_mod.tick(ctx, handle=no_llm)
    assert results[0]["verdict"] == "self-only"


def test_a_question_is_answered_once_even_when_the_batch_retries(ctx, slack,
                                                                tracked):
    """One poison command plus one question posted the same answer three times
    and paid for three full workers."""
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "@canopy ack return")
    add_msg(slack, state, "1700001001.000100", "U3", "@canopy 你怎么看")

    for _ in range(4):
        tick_mod.tick(ctx, run=lambda *a, **k: "我的答复")

    replies = [p for p in slack.posted if "我的答复" in (p["text"] or "")]
    assert len(replies) == 1


# -- the node digest: short, current, and what a child node inherits -----------

TWO_PART = "CHECKPOINT: 决定先加复合索引\nDIGEST: 支付超时在查慢查询,已定位到订单表,等 DBA 确认索引方案。"


def digest_of(ctx, proj_id, nid):
    from canopy import prompts
    return prompts.read_digest(ctx.node_dir(proj_id, nid))


def test_a_question_also_reaches_the_feed_and_the_digest(ctx, slack, tracked):
    """The summarizer used to be the `else` of "did anything else happen", so a
    message carrying an `@agent` question never reached the feed at all — and
    the cursor moved past it, so it was gone. Every child node lives on exactly
    that kind of traffic."""
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "@canopy 这个能今天出结论吗")

    def fake_run(cfg, prompt, node_dir, out_file=None, **kw):
        return TWO_PART if "CHECKPOINT" in prompt else "今天出不了,缺 DBA 确认。"

    tick_mod.tick(ctx, run=fake_run)

    assert any("今天出不了" in (p["text"] or "") for p in slack.posted)
    segments = feed_mod.load_segments(ctx.node_dir(proj_id, nid))
    assert "决定先加复合索引" in segments[0]["entries"][0]
    assert "等 DBA 确认索引方案" in digest_of(ctx, proj_id, nid)


def test_a_pure_command_batch_still_costs_no_llm(ctx, slack, tracked):
    """`fork` / `guide:` / `untrack` are executed as code and carry no
    conversation, so there is nothing to summarize and nothing to pay for."""
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "@canopy fork 慢查询定位")
    tick_mod.tick(ctx, run=no_llm)
    assert digest_of(ctx, proj_id, nid) == ""


def test_the_digest_is_rewritten_not_appended(ctx, slack, tracked):
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "先看看索引")
    tick_mod.tick(ctx, run=lambda *a, **k: "CHECKPOINT: SKIP\nDIGEST: 第一版现状")
    add_msg(slack, state, "1700001001.000100", "U2", "DBA 回了")
    tick_mod.tick(ctx, run=lambda *a, **k: "CHECKPOINT: SKIP\nDIGEST: 第二版现状")

    digest = digest_of(ctx, proj_id, nid)
    assert "第二版现状" in digest and "第一版" not in digest


def test_a_skipped_checkpoint_still_updates_the_digest(ctx, slack, tracked):
    """"still arguing, nothing settled" is exactly what a child needs, and it
    is the state that goes stale fastest."""
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "还在吵")
    results = tick_mod.tick(ctx, run=lambda *a, **k:
                            "CHECKPOINT: SKIP\nDIGEST: 还在吵方案,没有结论")
    assert results[0]["outcome"]["appended"] is False
    assert "还在吵方案" in digest_of(ctx, proj_id, nid)


def test_the_digest_is_capped(ctx, slack, tracked):
    from canopy import prompts
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "聊了很久")
    tick_mod.tick(ctx, run=lambda *a, **k:
                  "CHECKPOINT: SKIP\nDIGEST: " + "很长的现状描述 " * 200)
    assert len(digest_of(ctx, proj_id, nid)) <= prompts.DIGEST_MAX + 1


def test_a_child_worker_is_given_the_parents_digest(ctx, slack, tracked):
    from canopy import prompts
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "@canopy fork 慢查询定位")
    fork = tick_mod.tick(ctx, run=no_llm)[0]["outcome"]
    child = fork["node_id"]
    prompts.write_digest(ctx.node_dir(proj_id, nid),
                         "支付超时在查慢查询,等 DBA 确认索引方案。")

    child_state = store.load_state(ctx.dh, proj_id, child)
    add_msg(slack, child_state, "1700002000.000100", "U5",
            "@canopy 你从 DBA 角度看呢")
    seen = []

    def fake_run(cfg, prompt, node_dir, out_file=None, **kw):
        seen.append(prompt)
        return "先看执行计划。"

    tick_mod.tick(ctx, run=fake_run)
    worker_prompt = [p for p in seen if "Upstream" in p][0]
    assert "等 DBA 确认索引方案" in worker_prompt
    # And the link, so the owner can send it to read the whole parent thread.
    assert state["raw_permalink"] in worker_prompt


def test_no_upstream_block_when_the_parent_has_no_digest_yet(ctx, slack, tracked):
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "@canopy fork 慢查询定位")
    child = tick_mod.tick(ctx, run=no_llm)[0]["outcome"]["node_id"]
    child_state = store.load_state(ctx.dh, proj_id, child)
    add_msg(slack, child_state, "1700002000.000100", "U5", "@canopy 看法?")
    seen = []
    tick_mod.tick(ctx, run=lambda cfg, prompt, *a, **k: (seen.append(prompt), "ok")[1])
    # No invented context, no stale context: the worker says it lacks it.
    assert not any("Upstream" in p for p in seen)


def test_a_failed_summarizer_holds_the_cursor(ctx, slack, tracked):
    """It rides along with the reply now, so its failure has to count as one."""
    from canopy.errors import RunnerError
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "@canopy 这个怎么办")

    def fake_run(cfg, prompt, node_dir, out_file=None, **kw):
        if "CHECKPOINT" in prompt:
            raise RunnerError("runner exited 1")
        return "我的答复"

    tick_mod.tick(ctx, run=fake_run)
    assert state_of(ctx, tracked)["cursor"] == state["cursor"]


def test_the_summarizer_sees_the_digest_it_is_rewriting(ctx, slack, tracked):
    """Rewriting "from scratch" while seeing only the increment would throw away
    everything still true and leave a digest describing three messages."""
    from canopy import prompts
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    prompts.write_digest(ctx.node_dir(proj_id, nid), "支付超时,正在查慢查询")
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "DBA 说索引没问题")
    seen = []

    def fake_run(cfg, prompt, node_dir, out_file=None, **kw):
        seen.append(prompt)
        return "CHECKPOINT: SKIP\nDIGEST: 支付超时,索引已排除,继续查"

    tick_mod.tick(ctx, run=fake_run)
    assert "正在查慢查询" in seen[0]
    assert "rewrite it, do not append" in seen[0]


# -- what a woken worker remembers, and how it looks the rest up ---------------

def test_a_worker_is_given_its_own_digest(ctx, slack, tracked):
    """The digest flowed *down* to children and nowhere else, so a woken worker
    knew the parent problem and nothing about what it had itself concluded
    twenty minutes earlier."""
    from canopy import prompts
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    prompts.write_digest(ctx.node_dir(proj_id, nid),
                         "在把原型流程做成 skill,repo 是 victor-develop/llm-ready")
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "@canopy 那你开个 PR 吧")
    seen = []

    def fake_run(cfg, prompt, node_dir, out_file=None, **kw):
        seen.append(prompt)
        return "开好了"

    tick_mod.tick(ctx, run=fake_run)
    worker_prompt = [p for p in seen if "This node so far" in p][0]
    assert "victor-develop/llm-ready" in worker_prompt


def test_a_worker_is_told_the_command_that_reads_the_thread(ctx, slack, tracked):
    """"the history is in Slack" is not actionable: the first version of that
    instruction cost a round trip in which the agent asked for a repo URL the
    owner had pasted eleven messages earlier."""
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "@canopy 那个 repo 你看了吗")
    seen = []
    tick_mod.tick(ctx, run=lambda cfg, prompt, *a, **k:
                  (seen.append(prompt), "看了")[1])
    worker_prompt = [p for p in seen if "How to read this thread" in p][0]
    assert "conversations read %s --thread-ts %s" % (state["channel"],
                                                    state["thread_ts"]) \
        in worker_prompt


def test_the_worker_is_told_to_look_rather_than_ask_again(ctx, slack, tracked):
    proj_id, nid = tracked["proj_id"], tracked["node_id"]
    state = state_of(ctx, tracked)
    add_msg(slack, state, "1700001000.000100", "U2", "@canopy 就用那个 repo")
    seen = []
    tick_mod.tick(ctx, run=lambda cfg, prompt, *a, **k:
                  (seen.append(prompt), "好")[1])
    focus = seen[0]
    assert "read this thread's history yourself" in focus
    # Cross-node reads keep the old rule: ask the owner first.
    assert "ask this node's owner first" in focus
