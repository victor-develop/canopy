"""What actually happens once a node has new messages.

Three paths, cheapest first: a structural command runs as code, chatter goes to
the light summarizer, and only a genuine `@agent` question spends a full worker.
Every path ends the same way — advance the cursor, release the lock, exit — so a
worker that dies just gets re-run from the cursor on the next tick.
"""

from . import mentions, ops, prompts, runner as runner_mod, store
from .errors import CanopyError
from .prompts import SKIP


def _last_line(text):
    """The answer is the runner's last non-empty line.

    A runner without `-o` prints its own chatter first; taking the first line
    published a banner as a checkpoint.
    """
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return lines[-1] if lines else ""


def agent_names(dh):
    from . import paths
    directory = paths.profiles_dir(dh)
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.md"))


def classify(messages, agents):
    """-> {"kind", "commands": [...], "question": {...}|None}.

    Every structural command in the batch is kept, in order. Returning only the
    first one silently dropped the rest: two people typing `@canopy fork …`
    within one tick window produced one child, and the second fork was gone with
    no log and no reply — while the cursor moved past it.
    """
    commands, question = [], None
    for msg in messages:
        text = msg.get("text") or ""
        for agent in mentions.mentioned_agents(text, agents):
            cmd, arg = mentions.parse(text, agent)
            if cmd:
                commands.append({"message": msg, "agent": agent,
                                 "command": cmd, "arg": arg})
            elif question is None:
                question = {"message": msg, "agent": agent}
    kind = "command" if commands else ("agent" if question else "chatter")
    return {"kind": kind, "commands": commands, "question": question}


def _execute(ctx, proj_id, nid, messages, plan, out_file=None, run=None):
    """Commands first, in order; then one reply if somebody also asked something.

    Two rules here, both learned from failures:

    - **Each command is recorded as done the moment it succeeds.** The batch is
      retried when anything in it fails, and a `fork` is not idempotent: without
      this, one `fork` plus one bad `ack return` in the same batch grew a
      duplicate child (and three Slack messages) every five minutes forever.
    - **Each command is tried on its own.** One failure used to skip every
      command after it in the batch, silently.
    """
    results = []
    for detail in plan["commands"]:
        if _already_done(ctx, proj_id, nid, detail):
            results.append({"command": detail["command"], "skipped": "already done"})
            continue
        try:
            outcome = run_command(ctx, proj_id, nid, detail, out_file=out_file)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            results.append({"command": detail["command"],
                            "error": "%s: %s" % (type(exc).__name__, exc)})
            continue
        _mark_done(ctx, proj_id, nid, detail)
        results.append(outcome)
    if plan["question"]:
        # Same guard as the commands. A reply is not idempotent either: one
        # poison command plus one question in the same batch posted the same
        # answer three times and paid for three full workers.
        reply_key = {"message": plan["question"]["message"], "command": "reply"}
        if _already_done(ctx, proj_id, nid, reply_key):
            results.append({"kind": "full", "skipped": "already answered"})
        else:
            results.append(run_full(ctx, proj_id, nid, messages,
                                    plan["question"]["agent"],
                                    out_file=out_file, run=run))
            _mark_done(ctx, proj_id, nid, reply_key)
    if not results:
        # Nothing structural, nothing asked: the summarizer *is* the outcome,
        # and it keeps its own "light" kind for the tick log.
        return summarize(ctx, proj_id, nid, messages, plan, out_file=out_file,
                         run=run) or {"kind": plan["kind"]}
    outcome = (dict(results[0], kind=plan["kind"]) if len(results) == 1
               else {"kind": plan["kind"], "steps": results})
    light = summarize(ctx, proj_id, nid, messages, plan, out_file=out_file,
                      run=run)
    if light is not None:
        outcome["summarized"] = light
    return outcome


def summarize(ctx, proj_id, nid, messages, plan, out_file=None, run=None):
    """The feed and the digest, for any batch a human actually talked in.

    This used to be the `else` of "did anything else happen", so a message that
    carried an `@agent` question never reached the feed at all — and the cursor
    moved past it regardless, so it was gone for good. A node whose whole life
    is `@agent` traffic (every child node, right after a fork) ended up with an
    empty feed and an empty digest, and its own children had nothing to inherit.

    A batch that is *only* structural commands still costs nothing: `fork`,
    `guide:` and `untrack` are executed as code, and there is no conversation in
    them to summarize.
    """
    if not messages:
        return None
    command_ts = set((d.get("message") or {}).get("ts") for d in plan["commands"])
    if plan["commands"] and not plan["question"] and \
            all(m.get("ts") in command_ts for m in messages):
        return None
    # Keyed on the newest message: the batch is retried as a whole when any part
    # of it fails, and a second checkpoint for the same messages is a duplicate
    # nobody can tell apart from a real one.
    key = {"command": "summarize", "message": {"ts": messages[-1].get("ts")}}
    if _already_done(ctx, proj_id, nid, key):
        return None
    result = run_light(ctx, proj_id, nid, messages, out_file=out_file, run=run)
    _mark_done(ctx, proj_id, nid, key)
    return result


DONE_KEEP = 50


def _command_key(detail):
    return "%s:%s" % ((detail.get("message") or {}).get("ts"), detail["command"])


def _already_done(ctx, proj_id, nid, detail):
    state = store.load_state(ctx.dh, proj_id, nid)
    return _command_key(detail) in (state.get("commands_done") or [])


def _mark_done(ctx, proj_id, nid, detail):
    # Re-read: the command itself (a fork) rewrote this file.
    state = store.load_state(ctx.dh, proj_id, nid)
    done = list(state.get("commands_done") or [])
    done.append(_command_key(detail))
    state["commands_done"] = done[-DONE_KEEP:]
    store.save_state(ctx.dh, proj_id, state)


def run_command(ctx, proj_id, nid, detail, out_file=None):
    """Structural commands: executed by code, never by the model."""
    cmd = detail["command"]
    arg = detail.get("arg")
    agent = detail.get("agent")
    msg = detail.get("message") or {}

    if cmd == "fork":
        if not arg:
            # Say so in the thread: a silent no-op leaves the person who typed
            # it believing a child thread exists somewhere. Through a template,
            # like every other byte Canopy posts — a hardcoded Chinese string
            # would land in an `en` tree.
            ops.post_notice(ctx, proj_id, nid, "fork-needs-title.md", agent=agent)
            return {"command": cmd, "skipped": "fork needs a title"}
        return dict(ops.fork(ctx, proj_id, nid, arg, agent=agent), command=cmd)
    if cmd == "guide":
        ops.guide(ctx, proj_id, nid, arg or "", message_ts=msg.get("ts"))
        return {"command": cmd, "guide": arg}
    if cmd == "untrack":
        return dict(ops.set_status(ctx, proj_id, nid, "untracked",
                                   reason=arg or "", agent=agent), command=cmd)
    if cmd == "track":
        # Re-open a node someone parked: `untrack` is not final, it is a toggle.
        return dict(ops.set_status(ctx, proj_id, nid, "active", reason=arg or "",
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


def upstream_for(ctx, proj_id, state):
    """The parent's digest, read at wake time. -> a block, or "".

    Read now rather than snapshotted at fork: the parent keeps moving, and a
    copy taken once is a second version of the truth that nothing maintains.
    Empty parent digest means an empty block — the worker then says it lacks
    context, which is honest, instead of being handed a stale one.
    """
    parent_nid = state.get("parent")
    if not parent_nid:
        return ""
    try:
        parent_state = store.load_state(ctx.dh, proj_id, parent_nid)
    except FileNotFoundError:
        return ""
    digest = prompts.read_digest(ctx.node_dir(proj_id, parent_nid))
    if not digest:
        return ""
    return prompts.upstream_block(parent_state, digest)


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
        upstream=upstream_for(ctx, proj_id, state),
    )
    answer = (run or runner_mod.run)(ctx.cfg, prompt, node_dir, out_file=out_file,
                                     effects=ctx.effects)
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
                                      out_file=out_file,
                                      effects=ctx.effects) or "").strip()
    parsed = prompts.parse_summary(answer)
    # The digest is written even when nothing was checkpoint-worthy: "still
    # arguing about X, nothing settled" is exactly what a child node needs, and
    # it is the state that goes stale fastest.
    digest = prompts.write_digest(node_dir, parsed.get("digest") or "")
    checkpoint = parsed.get("checkpoint")
    if not checkpoint:
        return {"kind": "light", "appended": False, "digest": bool(digest)}
    last = messages[-1] if messages else {}
    result = ops.append_checkpoint(
        ctx, proj_id, nid, checkpoint,
        author=last.get("user") or "",
        raw_permalink=ctx.permalink(state["channel"], last.get("ts") or
                                    state["thread_ts"]),
    )
    return {"kind": "light", "appended": True, "result": result,
            "digest": bool(digest)}


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
                                          out_file=out_file,
                                          effects=ctx.effects) or "").strip()
        if not answer or answer == SKIP:
            continue
        notes.extend([l.strip() for l in answer.splitlines() if l.strip()])

    tree = ctx.tree(proj_id)
    feed = ctx.feed(proj_id, state, tree)
    entries = [feed.render_entry(note, raw_permalink=state.get("raw_permalink"))
               for note in notes]
    segments = feed.rebuild(entries)
    store.save_state(ctx.dh, proj_id, state)

    # One more small call: the checkpoints boiled down to a current state. The
    # per-tick summarizer writes the digest as it goes, so without this the
    # heavy path left it exactly as stale as it found it.
    digest = ""
    if notes:
        answer = ((run or runner_mod.run)(
            ctx.cfg, prompts.digest_prompt(state, base, notes, guide_text=guide),
            node_dir, out_file=out_file, effects=ctx.effects) or "").strip()
        if answer and answer != SKIP:
            digest = prompts.write_digest(node_dir, _last_line(answer))
    return {"kind": "recalibrate", "checkpoints": len(notes),
            "segments": len(segments), "digest": bool(digest)}


def handle(ctx, proj_id, nid, messages, agents=None, out_file=None, run=None):
    """One node, one batch of new messages. -> what was done, for the tick log."""
    agents = agents if agents is not None else agent_names(ctx.dh)
    plan = classify(messages, agents)
    try:
        return _execute(ctx, proj_id, nid, messages, plan, out_file=out_file,
                        run=run)
    except (KeyboardInterrupt, SystemExit):
        # Ctrl-C is not a node-level failure. (The test guard rail raises an
        # EffectEscaped derived from BaseException, so it never lands here at
        # all — that is the point of it.)
        raise
    except Exception as exc:
        # Anything else. `ack return` with no draft raises a plain ValueError;
        # letting that escape killed the whole tick, left the cursor unmoved, and
        # replayed the same message every five minutes forever — while every
        # project after this one stopped being watched.
        return {"kind": plan["kind"], "error": "%s: %s" % (type(exc).__name__, exc)}
