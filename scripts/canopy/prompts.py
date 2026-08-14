"""What a woken worker is actually told.

Two rules shape everything here: a worker sees its node plus the increment,
never the whole tree or the full history; and the profile is global, so the
prompt wraps it with a stay-on-this-node instruction — otherwise a general
profile wanders into the parent thread's argument.
"""

import json
from pathlib import Path

FOCUS = (
    "You are woken for ONE node of a problem tree — one Slack thread, one "
    "sub-problem. Stay on it. You are reading only the messages that arrived "
    "since the last checkpoint, which is deliberate: do not ask for, invent, or "
    "assume the rest of the history. If you genuinely need context from the "
    "parent thread, say so in your reply and ask the node's owner — do not go "
    "read it on your own."
)

REPLY_CONTRACT = (
    "Your final message is what gets posted to the thread, verbatim, prefixed "
    "with your agent name. Write it as a colleague would: a few lines, no "
    "preamble, no restating what everyone just said. If you have nothing worth "
    "posting, make your final message exactly: SKIP"
)

SUMMARY_CONTRACT = (
    "Decide first whether anything here is checkpoint-worthy — a decision, a "
    "result, a blocker, a handoff. Chatter is not. If nothing qualifies, make "
    "your final message exactly: SKIP\n"
    "Otherwise your final message is ONE line: the checkpoint itself, in the "
    "language the thread is speaking, no bullet, no prefix."
)

SKIP = "SKIP"


def _messages_block(messages):
    lines = []
    for msg in messages:
        lines.append("[%s] %s: %s" % (msg.get("ts"), msg.get("user") or "?",
                                      (msg.get("text") or "").strip()))
    return "\n".join(lines) if lines else "(no new messages)"


def _read(path):
    path = Path(path)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def node_block(state):
    keep = ("node_id", "title", "owner", "status", "alias", "raw_permalink",
            "canvas_permalink")
    slim = dict((k, state.get(k)) for k in keep if state.get(k) is not None)
    return json.dumps(slim, ensure_ascii=False, indent=2)


def worker_prompt(state, profile_text, messages, guide_text="", agent="canopy"):
    parts = [
        FOCUS,
        "",
        "## Who you are (profile: %s)" % agent,
        profile_text.strip() or "(empty profile)",
        "",
        "## This node",
        node_block(state),
    ]
    if guide_text.strip():
        parts += ["", "## Standing guidance for this node", guide_text.strip()]
    parts += [
        "",
        "## New messages since the last checkpoint",
        _messages_block(messages),
        "",
        "## What to do",
        REPLY_CONTRACT,
    ]
    return "\n".join(parts)


def summarizer_prompt(state, base_prompt, messages, guide_text="",
                      recent_entries=None):
    parts = [
        base_prompt.strip() or "You maintain a checkpoint feed for one node.",
        "",
        "## This node",
        node_block(state),
    ]
    if guide_text.strip():
        parts += ["", "## Standing guidance (what to record, what to skip)",
                  guide_text.strip()]
    if recent_entries:
        parts += ["", "## Checkpoints already recorded (do not repeat them)",
                  "\n".join(recent_entries[-10:])]
    parts += [
        "",
        "## New messages",
        _messages_block(messages),
        "",
        "## What to do",
        SUMMARY_CONTRACT,
    ]
    return "\n".join(parts)


def recalibrate_prompt(state, base_prompt, chunk, previous_notes=None,
                       guide_text=""):
    """One chunk of a full rebuild: compress, carry forward, never re-read."""
    parts = [
        base_prompt.strip() or "You rebuild a checkpoint feed from raw history.",
        "",
        "## This node",
        node_block(state),
    ]
    if guide_text.strip():
        parts += ["", "## Standing guidance", guide_text.strip()]
    if previous_notes:
        parts += ["", "## Checkpoints from earlier chunks (keep them, do not redo)",
                  "\n".join(previous_notes)]
    parts += [
        "",
        "## This chunk of history",
        _messages_block(chunk),
        "",
        "## What to do",
        "Return ONLY the checkpoints this chunk adds, one per line, oldest "
        "first, in the language the thread speaks. No numbering, no commentary. "
        "If this chunk adds nothing, return exactly: SKIP",
    ]
    return "\n".join(parts)


def read_guide(node_dir):
    return _read(Path(node_dir) / "guide.md")


def read_profile(dh, agent):
    from . import paths
    return _read(paths.profiles_dir(dh) / ("%s.md" % agent))


def read_summarizer(dh, root=None):
    from . import paths
    root = Path(root) if root else paths.skill_root()
    user = Path(dh) / "default-summarizer.md"
    if user.exists():
        return _read(user)
    return _read(root / "templates" / "default-summarizer.md")
