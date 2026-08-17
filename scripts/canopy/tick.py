"""The cron tick: a cheap gate, and only then a worker.

Nodes with no new messages never touch an LLM — one `conversations read` per
active node and out. That is what makes an idle tree nearly free, so the gate
runs before anything else and short-circuits hard.
"""

import time

from . import events, locks, mentions, store, worker
from .errors import CanopyError, LockedError


def gate(ctx, proj_id, nid, state, now=None, alive=None, agents=()):
    """-> (verdict, payload). Verdicts: 'no-new', 'locked', 'self-only', 'work'."""
    cursor = state.get("cursor") or state["thread_ts"]
    # One read, not two: `latest_ts` used to pull the whole thread and then
    # `new_messages` pulled it again, doubling the cost of the cheap gate.
    fetched = ctx.slack.thread(state["channel"], state["thread_ts"],
                               oldest=cursor)
    messages = [m for m in fetched if float(m["ts"]) > float(cursor)]
    latest = messages[-1]["ts"] if messages else cursor
    if not messages:
        return "no-new", {"latest": latest}

    node_dir = ctx.node_dir(proj_id, nid)
    stale = int(ctx.cfg.get("lock_stale_seconds", 1800))
    if locks.is_held(node_dir, now=now, stale_after=stale, alive=alive):
        return "locked", {"latest": latest}

    theirs = [m for m in messages if not mentions.is_own_post(m["text"], agents)]
    if not theirs:
        # Only Canopy's own replies arrived. Advance past them, spend nothing.
        return "self-only", {"cursor": messages[-1]["ts"]}
    return "work", {"messages": theirs, "cursor": messages[-1]["ts"]}


MAX_RETRIES = 3


def _advance(ctx, proj_id, state, cursor, outcome, batch_start=None):
    """Move the cursor — unless this batch failed, and not forever if it keeps.

    A soft failure (Slack down, runner exited non-zero) used to advance anyway,
    so those messages were never answered and nothing said so. Holding the
    cursor fixes that, but holding it unconditionally replays the same poison
    message every tick for the life of the tree. So: retry a few times, then
    step over it and record that it was given up on.
    """
    failed = bool((outcome or {}).get("error")) or any(
        step.get("error") for step in (outcome or {}).get("steps", []))
    retries = dict(state.get("retries") or {})

    if not failed:
        state.pop("retries", None)
        state["cursor"] = cursor
        store.save_state(ctx.dh, proj_id, state)
        return "advanced"

    # Key on the OLDEST message in the failing batch, not the newest. The newest
    # ts changes every time anybody says anything, so on a busy thread the
    # counter reset every tick: the cursor froze, the batch grew without bound,
    # and the prompt (and the bill) grew with it. The oldest ts is stable until
    # the poison message is finally stepped over.
    key = batch_start or cursor
    count = retries.get("count", 0) + 1 if retries.get("batch_start") == key else 1
    if count >= MAX_RETRIES:
        state.pop("retries", None)
        state["cursor"] = cursor
        store.save_state(ctx.dh, proj_id, state)
        events.append(ctx.dh, {
            "kind": "worker", "ts": time.time(), "node": state["node_id"],
            "project": proj_id, "mode": "give-up", "outcome": "skipped",
            "error": "gave up after %d attempts: %s"
                     % (count, (outcome or {}).get("error")),
        })
        return "gave-up"

    state["retries"] = {"batch_start": key, "count": count}
    store.save_state(ctx.dh, proj_id, state)
    return "retrying"


def _outcome_word(outcome):
    """One word for the ops page: what did this worker actually do."""
    outcome = outcome or {}
    if outcome.get("error"):
        return "error"
    if outcome.get("posted"):
        return "posted"
    if outcome.get("appended"):
        return "checkpoint"
    if outcome.get("command"):
        return outcome["command"]
    return "skip"


def tick(ctx, now=None, alive=None, handle=None, out_file=None, run=None):
    """Walk every tracked project once. -> one result dict per active node.

    One tick at a time, machine-wide. A tick can legitimately outrun the cron
    interval — a node can hold a worker for its whole timeout, and the project
    lock waits — so without this, ticks stack up and each one pays for the same
    work. The lock lives in the data home and is broken if its holder dies.
    """
    handle = handle or worker.handle
    results = []
    agents = worker.agent_names(ctx.dh)
    try:
        gate_lock = locks.acquire(ctx.dh, stale_after=int(
            ctx.cfg.get("lock_stale_seconds", 1800)), alive=alive)
    except LockedError:
        return [{"verdict": "tick-already-running"}]

    try:
        results = _walk(ctx, agents, now=now, alive=alive, handle=handle,
                        out_file=out_file, run=run)
    finally:
        locks.release(ctx.dh)

    verdicts = {}
    for row in results:
        verdicts[row["verdict"]] = verdicts.get(row["verdict"], 0) + 1
    events.append(ctx.dh, {"kind": "tick", "ts": time.time(), "verdicts": verdicts})
    return results


def _walk(ctx, agents, now=None, alive=None, handle=None, out_file=None, run=None):
    results = []
    for proj_id, tree in sorted(ctx.trees().items()):
        for nid in sorted(tree.nodes):
            if tree.node(nid).get("status") != "active":
                continue
            try:
                state = store.load_state(ctx.dh, proj_id, nid)
            except FileNotFoundError:
                results.append({"project": proj_id, "node": nid,
                                "verdict": "no-state"})
                continue

            try:
                verdict, payload = gate(ctx, proj_id, nid, state, now=now,
                                        alive=alive, agents=agents)
            except CanopyError as exc:
                results.append({"project": proj_id, "node": nid,
                                "verdict": "error", "error": str(exc)})
                continue

            if verdict == "self-only":
                state["cursor"] = payload["cursor"]
                store.save_state(ctx.dh, proj_id, state)
                results.append({"project": proj_id, "node": nid,
                                "verdict": verdict, "cursor": state["cursor"]})
                continue

            if verdict != "work":
                results.append({"project": proj_id, "node": nid,
                                "verdict": verdict})
                continue

            messages = payload["messages"]
            node_dir = ctx.node_dir(proj_id, nid)
            stale = int(ctx.cfg.get("lock_stale_seconds", 1800))
            try:
                with locks.held(node_dir, now=now, stale_after=stale, alive=alive):
                    started = time.time()
                    # Without this the runner's own stdout — banner, token
                    # counts, thinking — is what gets posted to Slack, and the
                    # SKIP contract can never match.
                    answer_file = out_file or (node_dir / "last-message.txt")
                    outcome = handle(ctx, proj_id, nid, messages, agents=agents,
                                     out_file=answer_file, run=run)
                    events.append(ctx.dh, {
                        "kind": "worker",
                        "ts": time.time(),
                        "duration": round(time.time() - started, 2),
                        "project": proj_id,
                        "node": nid,
                        "mode": (outcome or {}).get("kind")
                                or (outcome or {}).get("command") or "?",
                        "outcome": _outcome_word(outcome),
                        "error": (outcome or {}).get("error"),
                    })
                    # Re-read: a worker (or a fork) may have rewritten state.
                    state = store.load_state(ctx.dh, proj_id, nid)
                    _advance(ctx, proj_id, state, payload["cursor"], outcome,
                             batch_start=messages[0]["ts"])
            except LockedError:
                results.append({"project": proj_id, "node": nid,
                                "verdict": "locked"})
                continue

            results.append({"project": proj_id, "node": nid, "verdict": "work",
                            "messages": len(messages), "outcome": outcome,
                            "cursor": state["cursor"]})
    return results
