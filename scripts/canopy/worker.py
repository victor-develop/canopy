"""What actually happens once a node has new messages.

Three paths, cheapest first: a structural command runs as code, chatter goes to
the light summarizer, and only a genuine `@agent` question spends a full worker.
Every path ends the same way — advance the cursor, release the lock, exit — so a
worker that dies just gets re-run from the cursor on the next tick.
"""

from . import mentions, ops, prompts, runner as runner_mod, store
from .errors import CanopyError
from .prompts import SKIP


def agent_names(dh):
    from . import paths
    directory = paths.profiles_dir(dh)
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.md"))


def classify(messages, agents):
    """-> ("command"|"agent"|"chatter", details).

    Commands win over a free-form question in the same batch: if someone typed
    `@canopy fork X`, the fork is the point.
    """
    for msg in messages:
        for agent in mentions.mentioned_agents(msg.get("text") or "", agents):
            cmd, arg = mentions.parse(msg.get("text") or "", agent)
            if cmd:
                return "command", {"message": msg, "agent": agent,
                                   "command": cmd, "arg": arg}
    for msg in messages:
        found = mentions.mentioned_agents(msg.get("text") or "", agents)
        if found:
            return "agent", {"message": msg, "agent": found[0]}
    return "chatter", {}


def run_command(ctx, proj_id, nid, detail, out_file=None):
    """Structural commands: executed by code, never by the model."""
    cmd = detail["command"]
    arg = detail.get("arg")
    agent = detail.get("agent")
    msg = detail.get("message") or {}

    if cmd == "fork":
        if not arg:
            return {"command": cmd, "skipped": "fork needs a title"}
        return dict(ops.fork(ctx, proj_id, nid, arg, agent=agent), command=cmd)
    if cmd == "guide":
        ops.guide(ctx, proj_id, nid, arg or "", message_ts=msg.get("ts"))
        return {"command": cmd, "guide": arg}
    if cmd == "done":
        return dict(ops.set_status(ctx, proj_id, nid, "done", reason=arg or "",
                                   agent=agent), command=cmd)
    if cmd == "ack return":
        return dict(ops.ack_return(ctx, proj_id, nid, agent=agent), command=cmd)
    if cmd == "return":
        summary = arg or summarize_for_return(ctx, proj_id, nid, out_file=out_file)
        return dict(ops.return_draft(ctx, proj_id, nid, summary, agent=agent),
                    command=cmd)
    if cmd == "recalibrate":
        return dict(recalibrate(ctx, proj_id, nid, out_file=out_file), command=cmd)
    return {"command": cmd, "skipped": "unknown command"}


def run_full(ctx, proj_id, nid, messages, agent, out_file=None, run=None):
    """A woken agent: profile + this node + the increment, then one reply."""
    state = store.load_state(ctx.dh, proj_id, nid)
    node_dir = ctx.node_dir(proj_id, nid)
    prompt = prompts.worker_prompt(
        state,
        prompts.read_profile(ctx.dh, agent),
        messages,
        guide_text=prompts.read_guide(node_dir),
        agent=agent,
    )
    answer = (run or runner_mod.run)(ctx.cfg, prompt, node_dir, out_file=out_file)
    answer = (answer or "").strip()
    if not answer or answer == SKIP:
        return {"kind": "full", "posted": False}
    ts = ops.reply(ctx, proj_id, nid, answer, agent=agent)
    return {"kind": "full", "posted": True, "ts": ts, "body": answer}


def run_light(ctx, proj_id, nid, messages, out_file=None, run=None):
    """Chatter path: update the feed, never wake a full agent."""
    state = store.load_state(ctx.dh, proj_id, nid)
    node_dir = ctx.node_dir(proj_id, nid)
    from . import feed as feed_mod
    segments = feed_mod.load_segments(node_dir)
    recent = segments[-1]["entries"] if segments else []
    prompt = prompts.summarizer_prompt(
        state,
        prompts.read_summarizer(ctx.dh, root=ctx.root),
        messages,
        guide_text=prompts.read_guide(node_dir),
        recent_entries=recent,
    )
    answer = ((run or runner_mod.run)(ctx.cfg, prompt, node_dir,
                                      out_file=out_file) or "").strip()
    if not answer or answer == SKIP:
        return {"kind": "light", "appended": False}
    last = messages[-1] if messages else {}
    result = ops.append_checkpoint(
        ctx, proj_id, nid, answer.splitlines()[0].strip(),
        author=last.get("user") or "",
        raw_permalink=ctx.permalink(state["channel"], last.get("ts") or
                                    state["thread_ts"]),
    )
    return {"kind": "light", "appended": True, "result": result}


def summarize_for_return(ctx, proj_id, nid, out_file=None, run=None):
    """`return` with no text: build the draft from the feed, not from thin air."""
    from . import feed as feed_mod
    node_dir = ctx.node_dir(proj_id, nid)
    segments = feed_mod.load_segments(node_dir)
    entries = []
    for segment in segments:
        entries.extend(segment.get("entries") or [])
    if not entries:
        return "(no checkpoints recorded yet)"
    return "\n".join(entries[-10:])


def recalibrate(ctx, proj_id, nid, chunk_size=80, out_file=None, run=None):
    """Loop C: read the whole history in chunks, rebuild every segment."""
    state = store.load_state(ctx.dh, proj_id, nid)
    node_dir = ctx.node_dir(proj_id, nid)
    history = ctx.slack.thread(state["channel"], state["thread_ts"], limit=1000)
    base = prompts.read_summarizer(ctx.dh, root=ctx.root)
    guide = prompts.read_guide(node_dir)

    notes = []
    for start in range(0, len(history), chunk_size):
        chunk = history[start:start + chunk_size]
        prompt = prompts.recalibrate_prompt(state, base, chunk,
                                            previous_notes=notes,
                                            guide_text=guide)
        answer = ((run or runner_mod.run)(ctx.cfg, prompt, node_dir,
                                          out_file=out_file) or "").strip()
        if not answer or answer == SKIP:
            continue
        notes.extend([l.strip() for l in answer.splitlines() if l.strip()])

    tree = ctx.tree(proj_id)
    feed = ctx.feed(proj_id, state, tree)
    entries = [feed.render_entry(note, raw_permalink=state.get("raw_permalink"))
               for note in notes]
    segments = feed.rebuild(entries)
    store.save_state(ctx.dh, proj_id, state)
    return {"kind": "recalibrate", "checkpoints": len(notes),
            "segments": len(segments)}


def handle(ctx, proj_id, nid, messages, agents=None, out_file=None, run=None):
    """One node, one batch of new messages. -> what was done, for the tick log."""
    agents = agents if agents is not None else agent_names(ctx.dh)
    kind, detail = classify(messages, agents)
    try:
        if kind == "command":
            return run_command(ctx, proj_id, nid, detail, out_file=out_file)
        if kind == "agent":
            return run_full(ctx, proj_id, nid, messages, detail["agent"],
                            out_file=out_file, run=run)
        return run_light(ctx, proj_id, nid, messages, out_file=out_file, run=run)
    except CanopyError as exc:
        return {"kind": kind, "error": str(exc)}
