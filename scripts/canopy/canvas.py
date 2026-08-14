"""The Canvas: A君's fast navigation surface across the whole tree.

Rendered from a template into markdown and kept on disk. `slackcli` can list
and read canvases but not write one, so Canopy writes the file and stores the
canvas link once you paste it in (`canopy canvas --link <url>`); every later
render updates the file in place and every message that carries
`canvas_permalink` picks the link up from `tree.json`.
"""

from pathlib import Path

from . import noderef, paths, store

STATUS_MARK = {
    "active": "•",
    "paused": "‖",
    "done": "✔",
    "untracked": "×",
}

DEFAULT_TEMPLATE = """# {title}

{tree}

_{count} nodes · {active} active · {done} done_
"""


def canvas_path(dh, proj_id):
    return paths.project_dir(dh, proj_id) / "canvas.md"


def render(tree, states=None, template=None):
    states = states or {}
    alias_map = noderef.aliases(tree)
    lines = []

    def walk(nid, depth):
        node = tree.node(nid)
        state = states.get(nid) or {}
        status = node.get("status", "active")
        mark = STATUS_MARK.get(status, "•")
        label = "%s `%s` %s" % (mark, alias_map[nid], node.get("title") or nid)
        links = []
        if state.get("raw_permalink"):
            links.append("[thread](%s)" % state["raw_permalink"])
        feed_ts = (state.get("feed_ts") or [])
        if state.get("feed_permalink"):
            links.append("[feed](%s)" % state["feed_permalink"])
        elif feed_ts:
            links.append("feed `%s`" % feed_ts[-1])
        owner = node.get("owner") or state.get("owner")
        tail = []
        if owner:
            tail.append(owner)
        if status != "active":
            tail.append(status)
        suffix = ""
        if links or tail:
            suffix = " — " + " · ".join(links + tail)
        lines.append("%s- %s%s" % ("  " * depth, label, suffix))
        for child in tree.children(nid):
            walk(child, depth + 1)

    walk(tree.root, 0)

    counts = {"active": 0, "done": 0}
    for nid in tree.nodes:
        status = tree.node(nid).get("status", "active")
        counts[status] = counts.get(status, 0) + 1

    body = template or DEFAULT_TEMPLATE
    return body.format(
        title=tree.node(tree.root).get("title") or tree.proj_id,
        tree="\n".join(lines),
        count=len(tree.nodes),
        active=counts.get("active", 0),
        done=counts.get("done", 0),
    )


def load_template(root=None):
    root = Path(root) if root else paths.skill_root()
    path = root / "templates" / "canvas.tmpl"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return DEFAULT_TEMPLATE


def write(dh, tree, states=None, root=None):
    text = render(tree, states=states, template=load_template(root))
    path = canvas_path(dh, tree.proj_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def permalink(tree, dh):
    """The link messages point at: the real Canvas once set, the file until then."""
    return tree.data.get("canvas_permalink") or canvas_path(dh, tree.proj_id).as_uri()


def set_link(dh, proj_id, url):
    tree = store.Tree.load(dh, proj_id)
    tree.data["canvas_permalink"] = url
    tree.save()
    return tree
