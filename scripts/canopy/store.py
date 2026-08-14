"""Disk is the source of truth. Every read and write of it goes through here.

Writes are atomic (temp file + rename): a tick that dies mid-write must not
leave a half-written `tree.json` that the next tick can't parse.
"""

import json
import os
import re
import tempfile
from pathlib import Path

from . import paths

STATUSES = ("active", "paused", "done", "untracked")


def read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    with p.open(encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp, str(p))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return p


def node_id(channel, thread_ts):
    return "%s-%s" % (channel, thread_ts)


def split_node_id(nid):
    channel, _, thread_ts = nid.partition("-")
    if not channel or not thread_ts:
        raise ValueError("not a node id: %r" % (nid,))
    return channel, thread_ts


def slugify(title, fallback="tree"):
    """projId is the human name for a project's single root."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:40] or fallback


class Tree(object):
    """`tree.json` for one project: the edges, written at fork time."""

    def __init__(self, proj_id, data, path=None):
        self.proj_id = proj_id
        self.data = data
        self.path = Path(path) if path else None

    @classmethod
    def new(cls, proj_id, root_id, title, owner):
        data = {
            "root": root_id,
            "canvas_id": None,
            "canvas_permalink": None,
            "nodes": {
                root_id: {
                    "parent": None,
                    "children": [],
                    "title": title,
                    "owner": owner,
                    "status": "active",
                }
            },
        }
        return cls(proj_id, data)

    @classmethod
    def load(cls, dh, proj_id):
        path = paths.project_dir(dh, proj_id) / "tree.json"
        data = read_json(path)
        if data is None:
            raise FileNotFoundError("no tree.json for project %r" % (proj_id,))
        return cls(proj_id, data, path)

    def save(self, dh=None):
        path = self.path
        if path is None:
            if dh is None:
                raise ValueError("need a data home to save a fresh tree")
            path = paths.project_dir(dh, self.proj_id) / "tree.json"
            self.path = path
        return write_json(path, self.data)

    @property
    def root(self):
        return self.data["root"]

    @property
    def nodes(self):
        return self.data["nodes"]

    def node(self, nid):
        return self.data["nodes"][nid]

    def children(self, nid):
        return list(self.data["nodes"][nid].get("children", []))

    def parent(self, nid):
        return self.data["nodes"][nid].get("parent")

    def ancestors(self, nid):
        """Root-first chain above `nid`."""
        chain = []
        cur = self.parent(nid)
        while cur:
            chain.append(cur)
            cur = self.parent(cur)
        chain.reverse()
        return chain

    def descendants(self, nid):
        out = []
        stack = self.children(nid)
        while stack:
            cur = stack.pop(0)
            out.append(cur)
            stack = self.children(cur) + stack
        return out

    def add_child(self, parent_id, child_id, title, owner=None, status="active"):
        if child_id in self.nodes:
            raise ValueError("node already in tree: %s" % (child_id,))
        self.nodes[child_id] = {
            "parent": parent_id,
            "children": [],
            "title": title,
            "owner": owner,
            "status": status,
        }
        self.nodes[parent_id].setdefault("children", []).append(child_id)
        return self.nodes[child_id]

    def set_status(self, nid, status):
        if status not in STATUSES:
            raise ValueError("unknown status %r" % (status,))
        self.nodes[nid]["status"] = status


def node_state_path(dh, proj_id, nid):
    return paths.node_dir(dh, proj_id, nid) / "state.json"


def load_state(dh, proj_id, nid):
    state = read_json(node_state_path(dh, proj_id, nid))
    if state is None:
        raise FileNotFoundError("no state.json for node %s" % (nid,))
    return state


def save_state(dh, proj_id, state):
    return write_json(node_state_path(dh, proj_id, state["node_id"]), state)


def new_state(channel, thread_ts, parent, title, owner, raw_permalink,
              canvas_permalink=None, reply_as=None):
    return {
        "node_id": node_id(channel, thread_ts),
        "channel": channel,
        "thread_ts": thread_ts,
        "parent": parent,
        "title": title,
        "owner": owner,
        "status": "active",
        "cursor": thread_ts,
        "feed_ts": [],
        "raw_permalink": raw_permalink,
        "canvas_permalink": canvas_permalink,
        "reply_as": reply_as,
    }


def list_projects(dh):
    root = paths.projects_dir(dh)
    if not root.is_dir():
        return []
    out = []
    for child in sorted(root.iterdir()):
        if (child / "tree.json").exists():
            out.append(child.name)
    return out


def load_all(dh):
    """Every project's tree, keyed by projId. The CLI is global by design."""
    return dict((p, Tree.load(dh, p)) for p in list_projects(dh))
