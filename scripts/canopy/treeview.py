"""`tree` / `status`: where to start and how far down, as two separate knobs.

Deep trees are the normal case. The depth cap is what stops Canopy from
re-creating the drowning problem it exists to solve, so a collapsed line always
carries its rollup counts — a truncated branch is visibly truncated.
"""

from . import noderef

STATUS_MARK = {"active": "active", "paused": "paused", "done": "done",
               "untracked": "untracked"}


def rollup(tree, nid, locked):
    counts = {"active": 0, "paused": 0, "done": 0, "untracked": 0, "locked": 0}
    for child in tree.descendants(nid):
        status = tree.node(child).get("status", "active")
        counts[status] = counts.get(status, 0) + 1
        if child in locked:
            counts["locked"] += 1
    return counts


def _rollup_text(counts):
    parts = []
    for key in ("active", "paused", "done"):
        if counts.get(key):
            parts.append("%d %s" % (counts[key], key))
    if counts.get("untracked"):
        parts.append("%d untracked" % counts["untracked"])
    text = " / ".join(parts)
    if counts.get("locked"):
        text = (text + "  " if text else "") + "lock:%d" % counts["locked"]
    return text


def render(trees, start=None, depth=None, locked=None, owners=None):
    """-> list of printable lines.

    `start` is a resolved (proj_id, node_id) or None for every root. `depth` is
    counted from wherever you started: 0 = starting node(s) only as a rollup
    line, N = expand N levels, None/"all" = no cap. Default is 0 for the no-arg
    form (the daily dashboard) and all once you name something (you already
    zoomed in, so expansion is what you asked for).
    """
    locked = set(locked or ())
    owners = owners or {}
    lines = []

    if start is None:
        cap = 0 if depth is None else depth
        for proj_id in sorted(trees):
            tree = trees[proj_id]
            lines.extend(_render_from(tree, proj_id, tree.root, cap, locked,
                                      owners, breadcrumb=False))
        return lines

    proj_id, nid = start
    tree = trees[proj_id]
    cap = depth
    lines.extend(_render_from(tree, proj_id, nid, cap, locked, owners,
                              breadcrumb=True))
    return lines


def _render_from(tree, proj_id, nid, cap, locked, owners, breadcrumb):
    alias_map = noderef.aliases(tree)
    lines = []

    if breadcrumb:
        chain = tree.ancestors(nid)
        if chain:
            crumbs = " / ".join([proj_id] + [alias_map[a] for a in chain])
            lines.append("↑ " + crumbs)

    if cap == 0:
        counts = rollup(tree, nid, locked)
        node = tree.node(nid)
        label = proj_id if tree.parent(nid) is None else alias_map[nid]
        lines.append(
            _line("", label, node.get("title") or "", node.get("status", "active"),
                  owners.get(nid) or node.get("owner") or "",
                  extra=_rollup_text(counts), lock=nid in locked)
        )
        return lines

    def walk(cur, level, prefix):
        node = tree.node(cur)
        alias = alias_map[cur]
        extra = ""
        deeper = tree.children(cur)
        at_cap = cap is not None and level >= cap
        if at_cap and deeper:
            extra = _rollup_text(rollup(tree, cur, locked))
        lines.append(
            _line(prefix, alias, node.get("title") or "",
                  node.get("status", "active"),
                  owners.get(cur) or node.get("owner") or "",
                  extra=extra, lock=cur in locked)
        )
        if at_cap:
            return
        for i, child in enumerate(deeper):
            last = i == len(deeper) - 1
            walk(child, level + 1, prefix + ("└ " if last else "├ "))

    walk(nid, 0, "")
    return lines


def _line(prefix, alias, title, status, owner, extra="", lock=False):
    cells = [
        "%s%-10s" % (prefix, alias),
        "%-16s" % title,
        "%-9s" % STATUS_MARK.get(status, status),
    ]
    if owner:
        cells.append("%-6s" % owner)
    text = " ".join(cells).rstrip()
    if lock:
        text += "  [lock]"
    if extra:
        text += "  " + extra
    return text
