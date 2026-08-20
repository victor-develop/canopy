"""Where things live, and the one-way copy from skill root to data home.

Runtime never writes a byte into the skill root: it is a git repo other people
install and PR against. Seeds are copied out on first use; from then on the
user's copy wins and a skill update can refresh the shipped defaults without
clobbering anything they wrote.
"""

import os
import shutil
from pathlib import Path

DEFAULT_DATA_HOME = "~/.canopy"


def skill_root():
    """The repo this file ships in (scripts/canopy/paths.py -> repo root)."""
    return Path(__file__).resolve().parents[2]


def data_home(override=None):
    if override:
        return Path(override).expanduser().resolve()
    env = os.environ.get("CANOPY_DATA_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path(DEFAULT_DATA_HOME).expanduser().resolve()


def config_path(dh):
    return Path(dh) / "config.json"


def profiles_dir(dh):
    return Path(dh) / "profiles"


def messages_dir(dh, locale):
    return Path(dh) / "messages" / locale


def scheduler_dir(dh):
    """Where `canopy loop` keeps its singleton lock.

    Not the data home itself: that already holds the per-tick lock, and a loop
    holding that one would block the very ticks it exists to run.
    """
    return Path(dh) / "scheduler"


def projects_dir(dh):
    return Path(dh) / "projects"


def project_dir(dh, proj_id):
    return projects_dir(dh) / proj_id


def project_messages_dir(dh, proj_id):
    return project_dir(dh, proj_id) / "messages"


def nodes_dir(dh, proj_id):
    return project_dir(dh, proj_id) / "nodes"


def node_dir(dh, proj_id, node_id):
    return nodes_dir(dh, proj_id) / node_id


def seed(dh, locale, root=None):
    """Copy shipped seeds into the data home. Never overwrites an existing file.

    Returns the list of files it created, so `track` can tell the user what
    appeared under their data home the first time they ran it.
    """
    root = Path(root) if root else skill_root()
    created = []

    prof_src = root / "templates" / "profiles"
    prof_dst = profiles_dir(dh)
    prof_dst.mkdir(parents=True, exist_ok=True)
    if prof_src.is_dir():
        for src in sorted(prof_src.glob("*.md")):
            dst = prof_dst / src.name
            if not dst.exists():
                shutil.copyfile(str(src), str(dst))
                created.append(dst)

    msg_src = root / "templates" / "messages" / locale
    msg_dst = messages_dir(dh, locale)
    msg_dst.mkdir(parents=True, exist_ok=True)
    if msg_src.is_dir():
        for src in sorted(msg_src.glob("*.md")):
            dst = msg_dst / src.name
            if not dst.exists():
                shutil.copyfile(str(src), str(dst))
                created.append(dst)

    projects_dir(dh).mkdir(parents=True, exist_ok=True)
    return created


def refresh(dh, locale, root=None, force=False):
    """Re-copy shipped message templates. -> (updated, kept).

    A file whose content differs from *both* the shipped version and nothing
    else is indistinguishable from an edit, so the rule is simple: identical
    files are skipped, different ones are kept and reported unless `force`.
    """
    root = Path(root) if root else skill_root()
    src_dir = root / "templates" / "messages" / locale
    dst_dir = messages_dir(dh, locale)
    dst_dir.mkdir(parents=True, exist_ok=True)
    updated, kept = [], []
    for src in sorted(src_dir.glob("*.md")):
        dst = dst_dir / src.name
        shipped = src.read_text(encoding="utf-8")
        if dst.exists() and dst.read_text(encoding="utf-8") == shipped:
            continue
        if dst.exists() and not force:
            kept.append(dst)
            continue
        dst.write_text(shipped, encoding="utf-8")
        updated.append(dst)
    return updated, kept
