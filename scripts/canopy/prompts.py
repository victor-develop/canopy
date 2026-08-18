"""What a woken worker is actually told.

Two rules shape everything here: a worker sees its node plus the increment,
never the whole tree or the full history; and the profile is global, so the
prompt wraps it with a stay-on-this-node instruction — otherwise a general
profile wanders into the parent thread's argument.
"""

import json
import re
from pathlib import Path

DIGEST_FILE = "digest.md"
DIGEST_MAX = 400

FOCUS = (
    "You are woken for ONE node of a problem tree — one Slack thread, one "
    "sub-problem. Stay on it. You are reading only the messages that arrived "
    "since the last checkpoint, plus a short digest of where the parent problem "
    "stands. That is deliberate: do not invent or assume the rest of the "
    "history. The full thread is a Slack link away (`raw_permalink` below, and "
    "the parent's link if there is one) — if you genuinely need it, say so in "
    "your reply and ask the node's owner first."
)

REPLY_CONTRACT = (
    "Your final message is what gets posted to the thread, verbatim, prefixed "
    "with your agent name. Write it as a colleague would: a few lines, no "
    "preamble, no restating what everyone just said. If you have nothing worth "
    "posting, make your final message exactly: SKIP"
)

SUMMARY_CONTRACT = (
    "Your final message is exactly two lines, in this order and nothing else:\n"
    "\n"
    "CHECKPOINT: <one line, in the language the thread speaks, no bullet, no "
    "prefix — or the single word SKIP>\n"
    "DIGEST: <where this problem stands *now*, at most %d characters>\n"
    "\n"
    "CHECKPOINT is for the feed, which people read as history: only a decision, "
    "a result, a blocker or a handoff earns one. Chatter earns SKIP.\n"
    "\n"
    "DIGEST is not history. Rewrite it from scratch every time, describing the "
    "current state: what the problem is, where it has got to, what it is "
    "waiting on. Workers on child threads read it and cannot see this thread at "
    "all, so write it for someone who has never read a word of it. Keep it "
    "short enough to stay true — a digest that grows is a second feed."
) % (DIGEST_MAX,)

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
            "tree_permalink")
    slim = dict((k, state.get(k)) for k in keep if state.get(k) is not None)
    return json.dumps(slim, ensure_ascii=False, indent=2)


def parse_summary(answer):
    """-> {"checkpoint", "digest"}, either possibly None.

    Tolerant on purpose: models label the lines, wrap them in code fences, or
    answer with the bare checkpoint the old contract asked for. A summarizer
    that returns something unparseable should cost a lost digest, never a lost
    checkpoint.
    """
    text = (answer or "").strip().strip("`").strip()
    if not text:
        return {"checkpoint": None, "digest": None}
    got = {}
    for label in ("checkpoint", "digest"):
        match = re.search(r"^\s*%s\s*[:：]\s*(.+?)\s*$" % label,
                          text, re.IGNORECASE | re.MULTILINE | re.DOTALL)
        got[label] = match.group(1).strip() if match else None
    if got["checkpoint"] is None and got["digest"] is None:
        # An answer in the old shape: one bare line, and it is the checkpoint.
        got["checkpoint"] = text.splitlines()[-1].strip()
    for label in ("checkpoint", "digest"):
        if got[label] and got[label].strip().upper() == SKIP:
            got[label] = None
    if got["digest"]:
        got["digest"] = shorten_digest(got["digest"])
    return got


def shorten_digest(text, limit=DIGEST_MAX):
    """A digest that grows is a second feed, so the cap is enforced here too."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def upstream_block(parent_state, digest):
    """What a child worker is told about the problem it was split off from."""
    lines = ["Parent: %s" % (parent_state.get("title") or parent_state.get("node_id"))]
    if parent_state.get("raw_permalink"):
        lines.append("Its thread: %s" % parent_state["raw_permalink"])
    lines.append("")
    lines.append(digest.strip())
    return "\n".join(lines)


def worker_prompt(state, profile_text, messages, guide_text="", agent="canopy",
                  upstream=None):
    parts = [
        FOCUS,
        "",
        "## Who you are (profile: %s)" % agent,
        profile_text.strip() or "(empty profile)",
        "",
        "## This node",
        node_block(state),
    ]
    if upstream and upstream.strip():
        parts += ["", "## Upstream — the parent problem, as it stands now",
                  upstream.strip()]
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


def digest_prompt(state, base_prompt, entries, guide_text=""):
    """One last small call after a rebuild: the checkpoints, boiled to a state.

    `recalibrate` is the other way a feed comes into existence, so without this
    the escape hatch left the digest exactly as stale as it found it — and
    `track` uses that path to adopt a thread mid-argument, which is precisely
    when a child node would inherit nothing.
    """
    parts = [
        base_prompt.strip() or "You maintain a checkpoint feed for one node.",
        "",
        "## This node",
        node_block(state),
    ]
    if guide_text.strip():
        parts += ["", "## Standing guidance", guide_text.strip()]
    parts += [
        "",
        "## Every checkpoint recorded for this node, oldest first",
        "\n".join(entries) or "(none)",
        "",
        "## What to do",
        "Return ONE paragraph of at most %d characters and nothing else: where "
        "this problem stands now — what it is, where it has got to, what it is "
        "waiting on. Not a list, not a history. Workers on child threads read "
        "it and have never seen this thread, so write it for them. If there is "
        "nothing to say, return exactly: %s" % (DIGEST_MAX, SKIP),
    ]
    return "\n".join(parts)


def read_guide(node_dir):
    return _read(Path(node_dir) / "guide.md")


def read_digest(node_dir):
    """The node's current-state summary. Overwritten every time, never appended."""
    return _read(Path(node_dir) / DIGEST_FILE).strip()


def write_digest(node_dir, text):
    text = shorten_digest(text)
    if not text:
        return ""
    Path(node_dir).mkdir(parents=True, exist_ok=True)
    (Path(node_dir) / DIGEST_FILE).write_text(text + "\n", encoding="utf-8")
    return text


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
