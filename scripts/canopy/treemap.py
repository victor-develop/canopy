"""The tree map: the whole tree, posted as Slack messages.

Slack Canvas is not reachable from `slackcli` (read-only there), and a Canvas
link that only opens on the machine that rendered it is worse than no link. So
the navigation surface is a plain message in the same channel, updated in place
whenever structure or status changes.

One message can't hold a deep tree, so the map is **segmented by depth**: four
levels per message. A node at the cut line stops being a row and becomes a
pointer to the message that continues from it, and that message links back. The
tree stays walkable by clicking, which is the whole point of having it.
"""

from . import noderef

DEPTH_PER_MESSAGE = 4

STATUS_MARK = {
    "active": "•",
    "paused": "‖",
    "done": "✔",
    "untracked": "×",
}


def segments(tree, depth_per_message=DEPTH_PER_MESSAGE):
    """-> [{"index", "root", "rows", "spawns"}] in posting order.

    `rows` is [(node_id, depth, is_pointer)]; a pointer row is a child that got
    cut off and lives in a later segment.
    """
    out = []
    queue = [tree.root]
    while queue:
        seg_root = queue.pop(0)
        seg = {"index": len(out) + 1, "root": seg_root, "rows": [], "spawns": []}

        def walk(nid, depth):
            seg["rows"].append((nid, depth, False))
            for child in tree.children(nid):
                if depth + 1 >= depth_per_message:
                    seg["rows"].append((child, depth + 1, True))
                    seg["spawns"].append(child)
                    queue.append(child)
                else:
                    walk(child, depth + 1)

        walk(seg_root, 0)
        out.append(seg)
    return out


def segment_of(tree, node_id, depth_per_message=DEPTH_PER_MESSAGE):
    """Which segment a node's row lives in — what a permalink should point at."""
    for seg in segments(tree, depth_per_message):
        for nid, _depth, pointer in seg["rows"]:
            if nid == node_id and not pointer:
                return seg["index"]
    return 1


def render_body(tree, seg, states=None, permalink=None, segment_link=None):
    """The rows of one segment, as Slack mrkdwn."""
    states = states or {}
    alias_map = noderef.aliases(tree)
    lines = []
    for nid, depth, pointer in seg["rows"]:
        node = tree.node(nid)
        alias = alias_map[nid]
        title = node.get("title") or nid
        indent = "    " * depth
        if pointer:
            link = segment_link(nid) if segment_link else None
            tail = "<%s|接着看>" % link if link else "(下一条消息)"
            lines.append("%s↳ `%s` %s — %s" % (indent, alias, title, tail))
            continue

        state = states.get(nid) or {}
        mark = STATUS_MARK.get(node.get("status", "active"), "•")
        bits = []
        raw = state.get("raw_permalink")
        if raw:
            bits.append("<%s|thread>" % raw)
        feed_ts = state.get("feed_ts") or []
        if feed_ts and permalink:
            bits.append("<%s|feed>" % permalink(state["channel"], feed_ts[-1]))
        owner = node.get("owner") or state.get("owner")
        if owner:
            bits.append(owner)
        status = node.get("status", "active")
        if status != "active":
            bits.append(status)
        suffix = ("  ·  " + "  ·  ".join(bits)) if bits else ""
        lines.append("%s%s `%s` %s%s" % (indent, mark, alias, title, suffix))
    return "\n".join(lines)


def counts(tree):
    tally = {"active": 0, "paused": 0, "done": 0, "untracked": 0}
    for nid in tree.nodes:
        status = tree.node(nid).get("status", "active")
        tally[status] = tally.get(status, 0) + 1
    return tally


def counts_text(tree):
    tally = counts(tree)
    parts = ["%d 个节点" % len(tree.nodes)]
    for key, label in (("active", "在跑"), ("paused", "暂停"), ("done", "完成")):
        if tally.get(key):
            parts.append("%d %s" % (tally[key], label))
    return " · ".join(parts)


def stored(tree):
    return tree.data.setdefault("tree_msgs", [])


def permalink(tree, cfg, node_id=None):
    """Link to the tree message a node's row lives in, if it has been posted."""
    from . import config as config_mod
    msgs = tree.data.get("tree_msgs") or []
    if not msgs:
        return None
    index = segment_of(tree, node_id) if node_id else 1
    for msg in msgs:
        if msg["index"] == index and msg.get("ts"):
            return config_mod.permalink(cfg, msg["channel"], msg["ts"])
    return config_mod.permalink(cfg, msgs[0]["channel"], msgs[0]["ts"])
