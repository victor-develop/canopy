"""Naming a project: ask the runner for a short semantic id.

The projId is typed in every CLI command, so it should read like something a
person would have chosen — `figma-free-design`, not a 40-character slug of a
Chinese sentence. Deriving it mechanically fails both ways: an ASCII slug of a
Chinese title is empty, and a unicode slug is the whole sentence.

So `track` spends one small model call on it, once per tree, and falls back to
the mechanical slug whenever that call fails, times out, or returns something
unusable. A tree that gets tracked under an ugly id is a nuisance; a `track`
that fails because the namer was offline is a real problem.
"""

import re

from . import runner as runner_mod
from .errors import CanopyError

PROMPT = """Name this discussion, twice.

What was said to open it:
%s

Reply with exactly two lines, nothing else:

id: <2 to 4 English words, lowercase, hyphen-joined, ASCII even if the text is
not — it names the problem, not the format: `figma-free-design`, not
`slack-thread`; no dates, no names, no "project"/"issue"/"thread">
title: <a headline in the language of the text above, at most 12 Chinese
characters or 24 latin ones — what the problem IS, not how it was phrased. Drop
openers like "问题:" / "请问" / "想讨论一下". No trailing punctuation.>"""

TITLE_MAX = 24        # ~12 Chinese characters
BREAKS = "。!?；;\n:：,，、 "

VALID = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+){0,4}$")


def sanitize(raw):
    """-> a usable id, or None. Models like to add quotes, backticks, prose."""
    if not raw:
        return None
    line = [l.strip() for l in str(raw).strip().splitlines() if l.strip()]
    if not line:
        return None
    candidate = line[-1].strip().strip("`\"'.,: ").lower()
    candidate = re.sub(r"\s+", "-", candidate)
    candidate = re.sub(r"[^a-z0-9-]", "", candidate)
    candidate = re.sub(r"-{2,}", "-", candidate).strip("-")
    if not candidate or len(candidate) > 40 or not VALID.match(candidate):
        return None
    return candidate


def shorten(text, limit=TITLE_MAX):
    """A readable fallback title: cut at punctuation, not mid-word.

    The first 60 characters of a Slack message is not a title. It arrives with
    the opener still attached and sliced through the middle of a word
    (`…html/react compone`), and that string then shows up in the map header and
    in every row.
    """
    text = " ".join((text or "").split())
    # Drop a leading label like "问题：" / "求助:" / "Question:" — a colon inside
    # the first few characters is an opener, not content.
    head = text[:10]
    for colon in ("：", ":"):
        if colon in head:
            text = text.split(colon, 1)[1].strip()
            break
    if _width(text) <= limit:
        return text
    cut, best = "", ""
    for ch in text:
        if _width(cut + ch) > limit:
            break
        cut += ch
        if ch in BREAKS:
            best = cut.rstrip(BREAKS)
    return (best or cut).rstrip(BREAKS) + "…"


def _width(text):
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def suggest(cfg, title, cwd, run=None, effects=None):
    """-> {"id", "title"} with either key possibly None."""
    timeout = cfg.get("shortid_timeout_seconds") or 90
    try:
        if run:
            answer = run(cfg, PROMPT % (title,), cwd)
        else:
            answer = runner_mod.run(cfg, PROMPT % (title,), cwd, timeout=timeout,
                                    effects=effects)
    except CanopyError:
        return {"id": None, "title": None}
    except OSError:
        return {"id": None, "title": None}
    return parse(answer)


def parse(answer):
    """-> {"id", "title"}. Either can come back None; both are optional."""
    out = {"id": None, "title": None}
    for line in (answer or "").splitlines():
        line = line.strip().strip("`")
        low = line.lower()
        if low.startswith("id:"):
            out["id"] = sanitize(line.split(":", 1)[1])
        elif low.startswith("title:"):
            candidate = line.split(":", 1)[1].strip().strip("\"'。.")
            if candidate and _width(candidate) <= TITLE_MAX * 1.5:
                out["title"] = shorten(candidate)
    if out["id"] is None:
        # A model that ignored the format and answered with just an id.
        out["id"] = sanitize(answer)
    return out
