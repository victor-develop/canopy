"""Reading the in-thread command set out of a Slack message.

Structural commands (`fork`, `return`, `done`, …) are parsed and executed by
code, never by the model: a fork writes an edge into `tree.json`, and a
hallucinated edge is a corrupted tree nobody notices for a week. The model is
only asked for the things that need judgement — replies and summaries.
"""

import re

COMMANDS = ("ack return", "fork", "return", "guide", "recalibrate", "done")


def mention_re(agent):
    return re.compile(r"(?:^|\s)@%s\b" % re.escape(agent), re.IGNORECASE)


def mentioned_agents(text, agents):
    """-> the agents `@`-mentioned in this message, in the order they appear."""
    found = []
    for agent in agents:
        match = mention_re(agent).search(text or "")
        if match:
            found.append((match.start(), agent))
    found.sort()
    return [agent for _pos, agent in found]


def is_own_post(text, agents):
    """Did Canopy write this? Its replies are identity-prefixed `*[agent]*`.

    Without this, every reply the tick posts shows up as a new message on the
    next tick and pays for a summarizer to read Canopy's own words back.
    """
    head = (text or "").lstrip()
    for agent in agents:
        if head.startswith("*[%s]*" % agent) or head.startswith("[%s]" % agent):
            return True
    return False


def parse(text, agent):
    """-> (command, argument) or (None, None).

    `ack return` is matched before `return`, otherwise every ack would read as
    a fresh draft and quietly re-post the summary upward.
    """
    match = mention_re(agent).search(text or "")
    if not match:
        return None, None
    rest = (text or "")[match.end():].strip()
    low = rest.lower()

    for cmd in COMMANDS:
        if cmd == "guide":
            guide = re.match(r"guide\s*[:：]\s*(.*)", rest, re.IGNORECASE | re.DOTALL)
            if guide:
                return "guide", guide.group(1).strip()
            continue
        if low == cmd or low.startswith(cmd + " ") or low.startswith(cmd + "\n"):
            arg = rest[len(cmd):].strip()
            return cmd, arg or None
    return None, None
