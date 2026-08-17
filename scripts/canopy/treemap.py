"""The tree map: the whole tree, posted as Slack messages.

Slack Canvas is not reachable from `slackcli` (read-only there), and a Canvas
link that only opens on the machine that rendered it is worse than no link. So
the navigation surface is a plain message in the same channel, updated in place
whenever structure or status changes.

One message can't hold a deep tree, so the map is **segmented by depth**: four
levels per message. A node at the cut line stops being a row and becomes a
pointer to the message that continues from it, and that message links back.

A row is four things: the depth bullet, the alias (so you can type
`canopy untrack 1.b`), the title, and the two links you actually click — the
update feed and the raw thread. Nothing else. Every candidate for a fifth
column (owner, checkpoint count, "returned", "quiet since") was tried and cut:
the map is scanned, not read, and each extra token pushes the next row's links
further right. `canopy tree` is where the detail belongs.
"""

from . import noderef

DEPTH_PER_MESSAGE = 4
BULLETS = ["•", "◦", "▪︎", "▪︎"]
UNTRACKED_MARK = "×"
INDENT = "    "


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


def render_body(tree, seg, render, states=None, permalink=None,
                segment_link=None):
    """The rows of one segment. `render(name, values)` renders a template."""
    states = states or {}
    alias_map = noderef.aliases(tree)
    lines = []

    for nid, depth, pointer in seg["rows"]:
        node = tree.node(nid)
        state = states.get(nid) or {}
        indent = INDENT * depth
        alias = alias_map[nid]
        title = node.get("title") or nid

        if pointer:
            lines.append(render("tree-map-pointer.md", {
                "indent": indent,
                "alias": alias,
                "title": title,
                "segment_url": segment_link(nid) if segment_link else "",
            }))
            continue

        feed_ts = state.get("feed_ts") or []
        mark = (UNTRACKED_MARK if node.get("status") == "untracked"
                else BULLETS[min(depth, len(BULLETS) - 1)])
        lines.append(render("tree-map-row.md", {
            "indent": indent,
            "mark": mark,
            "alias": alias,
            "title": title,
            "feed_url": permalink(state["channel"], feed_ts[-1]) if feed_ts else "",
            "raw_url": state.get("raw_permalink") or "",
        }))
    return "\n".join(lines)


def counts(tree):
    tally = {"active": 0, "untracked": 0}
    for nid in tree.nodes:
        status = tree.node(nid).get("status", "active")
        tally[status] = tally.get(status, 0) + 1
    return tally


def counts_text(tree):
    tally = counts(tree)
    parts = ["%d 个节点" % len(tree.nodes)]
    if tally.get("untracked"):
        parts.append("%d 已收" % tally["untracked"])
    return " · ".join(parts)


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
