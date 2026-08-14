"""Turning `1.a.ii`, `慢查询`, or a raw node id into exactly one node.

Aliases are positional, not stored: recomputed from each node's place among its
siblings on every render, and they shift when a sibling is inserted. Anything
durable (cron args, tree.json, logs) stores node ids instead.
"""

from .errors import AmbiguousRefError, NodeRefError

LETTERS = "abcdefghijklmnopqrstuvwxyz"
ROMAN = [
    (1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
    (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"),
]


def _roman(n):
    out = []
    for value, sym in ROMAN:
        while n >= value:
            out.append(sym)
            n -= value
    return "".join(out)


def _letters(n):
    """1 -> a, 26 -> z, 27 -> aa."""
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = LETTERS[rem] + out
    return out


def _segment(level, index):
    """Level 0 is a number, level 1 letters, level 2 roman, then it cycles."""
    kind = level % 3
    if kind == 0:
        return str(index + 1)
    if kind == 1:
        return _letters(index + 1)
    return _roman(index + 1)


def aliases(tree):
    """-> {node_id: alias} for one project's tree."""
    out = {}

    def walk(nid, level, index):
        segment = _segment(level, index)
        parent = tree.parent(nid)
        out[nid] = segment if parent is None else out[parent] + "." + segment
        for i, child in enumerate(tree.children(nid)):
            walk(child, level + 1, i)

    walk(tree.root, 0, 0)
    return out


def by_alias(tree):
    return dict((alias, nid) for nid, alias in aliases(tree).items())


def qualified(proj_id, alias):
    return "%s:%s" % (proj_id, alias)


def resolve(ref, trees):
    """-> (proj_id, node_id).

    Accepts, in order: a path alias (`1.a`), a unique title substring, a full or
    prefix node id. Optionally qualified as `<projId>:<ref>` because a bare
    alias only means something inside one project and the CLI is global.

    Never guesses: more than one match raises with the candidates attached.
    """
    if not ref or not str(ref).strip():
        raise NodeRefError("empty node ref")
    ref = str(ref).strip()

    scope = None
    if ":" in ref:
        scope, _, ref = ref.partition(":")
        if scope not in trees:
            raise NodeRefError("no such project: %s" % (scope,))
        ref = ref.strip()
        if not ref:
            tree = trees[scope]
            return scope, tree.root

    if scope is None and ref in trees:
        # A projId is just the human name for that project's single root.
        return ref, trees[ref].root

    pairs = [(p, t) for p, t in trees.items() if scope is None or p == scope]

    hits = []
    for proj_id, tree in pairs:
        alias_map = by_alias(tree)
        if ref in alias_map:
            hits.append((proj_id, alias_map[ref], qualified(proj_id, ref)))
    if len(hits) == 1:
        return hits[0][0], hits[0][1]
    if len(hits) > 1:
        raise AmbiguousRefError(ref, [h[2] for h in hits])

    hits = []
    for proj_id, tree in pairs:
        alias_map = aliases(tree)
        for nid, node in tree.nodes.items():
            if ref == nid or nid.startswith(ref):
                hits.append((proj_id, nid, qualified(proj_id, alias_map[nid])))
    if len(hits) == 1:
        return hits[0][0], hits[0][1]
    if len(hits) > 1:
        raise AmbiguousRefError(ref, [h[2] for h in hits])

    needle = ref.lower()
    for proj_id, tree in pairs:
        alias_map = aliases(tree)
        for nid, node in tree.nodes.items():
            if needle in (node.get("title") or "").lower():
                hits.append((proj_id, nid, qualified(proj_id, alias_map[nid])))
    if len(hits) == 1:
        return hits[0][0], hits[0][1]
    if len(hits) > 1:
        raise AmbiguousRefError(ref, [h[2] for h in hits])

    raise NodeRefError("nothing matches node ref %r" % (ref,))
