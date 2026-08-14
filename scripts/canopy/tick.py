"""The cron tick: a cheap gate, and only then a worker.

Nodes with no new messages never touch an LLM — one `conversations read` per
active node and out. That is what makes an idle tree nearly free, so the gate
runs before anything else and short-circuits hard.
"""

from . import locks, store, worker
from .errors import CanopyError, LockedError


def gate(ctx, proj_id, nid, state, now=None, alive=None):
    """-> (verdict, payload). Verdicts: 'no-new', 'locked', 'work'."""
    latest = ctx.slack.latest_ts(state["channel"], state["thread_ts"],
                                 after_ts=state.get("cursor"))
    if float(latest) <= float(state.get("cursor") or state["thread_ts"]):
        return "no-new", {"latest": latest}

    node_dir = ctx.node_dir(proj_id, nid)
    stale = int(ctx.cfg.get("lock_stale_seconds", 1800))
    if locks.is_held(node_dir, now=now, stale_after=stale, alive=alive):
        return "locked", {"latest": latest}

    messages = ctx.slack.new_messages(state["channel"], state["thread_ts"],
                                      state.get("cursor") or state["thread_ts"])
    if not messages:
        return "no-new", {"latest": latest}
    return "work", {"messages": messages}


def tick(ctx, now=None, alive=None, handle=None, out_file=None, run=None):
    """Walk every tracked project once. -> one result dict per active node."""
    handle = handle or worker.handle
    results = []
    agents = worker.agent_names(ctx.dh)

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
                                        alive=alive)
            except CanopyError as exc:
                results.append({"project": proj_id, "node": nid,
                                "verdict": "error", "error": str(exc)})
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
                    outcome = handle(ctx, proj_id, nid, messages, agents=agents,
                                     out_file=out_file, run=run)
                    # Re-read: a worker (or a fork) may have rewritten state.
                    state = store.load_state(ctx.dh, proj_id, nid)
                    state["cursor"] = messages[-1]["ts"]
                    store.save_state(ctx.dh, proj_id, state)
            except LockedError:
                results.append({"project": proj_id, "node": nid,
                                "verdict": "locked"})
                continue

            results.append({"project": proj_id, "node": nid, "verdict": "work",
                            "messages": len(messages), "outcome": outcome,
                            "cursor": state["cursor"]})
    return results
