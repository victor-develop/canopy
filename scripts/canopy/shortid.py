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

PROMPT = """Name a project directory after this discussion title.

Title: %s

Rules:
- 2 to 4 English words, lowercase, joined by hyphens
- ASCII only, even when the title is not English — translate the idea
- it names the problem, not the format: `figma-free-design`, not `slack-thread`
- no dates, no owner names, no "project" / "issue" / "thread"

Reply with ONLY the id, nothing else."""

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


def suggest(cfg, title, cwd, run=None):
    """-> a short id, or None to let the caller fall back to the slug."""
    timeout = cfg.get("shortid_timeout_seconds") or 90
    try:
        if run:
            answer = run(cfg, PROMPT % (title,), cwd)
        else:
            answer = runner_mod.run(cfg, PROMPT % (title,), cwd, timeout=timeout)
    except CanopyError:
        return None
    except OSError:
        return None
    return sanitize(answer)
