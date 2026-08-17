"""Message templates: front matter + body, substitution only, no logic.

The messages Canopy posts *are* the product, so none of their wording is
hardcoded. Rendering is deliberately dumb: `{{var}}` in, value out. A body that
references a variable its front matter never declared is a hard error and the
post is abandoned — better a failed tick in the log than `{{parent_permalink}}`
posted verbatim into a channel.
"""

import re

from . import paths
from .errors import RenderError

VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
FRONT_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
EMPTY_LINK_RE = re.compile(r"<\|([^>\n]*)>")

# Resolution layers, first hit wins.
LAYER_PROJECT = "project"
LAYER_USER = "user"
LAYER_SHIPPED = "shipped"


def parse(text):
    """-> (meta, body). `meta` has at least `moment` and `vars`."""
    match = FRONT_RE.match(text)
    if not match:
        raise RenderError("template has no front matter")
    meta = _parse_front_matter(match.group(1))
    body = text[match.end():]
    return meta, body


def _parse_front_matter(block):
    """A deliberately small YAML subset: `key: value` and `key: [a, b]`.

    Front matter here only ever holds `moment` and `vars`; pulling in a YAML
    dependency for two keys would put a package install between cron and a tick.
    """
    meta = {}
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise RenderError("bad front matter line: %r" % (raw,))
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            items = [v.strip() for v in inner.split(",")] if inner else []
            meta[key] = [v for v in items if v]
        else:
            meta[key] = value
    meta.setdefault("vars", [])
    return meta


def declared_vars(text):
    return list(parse(text)[0].get("vars", []))


def render(text, values):
    """Render one template. Undeclared variable in the body -> RenderError.

    A declared variable that is missing or None renders as nothing: `reason` on
    a plain `done`, or `entries` on a fresh feed, are legitimately empty.
    """
    meta, body = parse(text)
    declared = set(meta.get("vars", []))
    used = set(VAR_RE.findall(body))
    undeclared = sorted(used - declared)
    if undeclared:
        raise RenderError(
            "template body uses undeclared vars: %s (declared: %s)"
            % (", ".join(undeclared), ", ".join(sorted(declared)) or "none")
        )

    def sub(match):
        value = values.get(match.group(1))
        return "" if value is None else str(value)

    out = VAR_RE.sub(sub, body)
    # A link whose URL came out empty would post as `<|label>`, which Slack
    # renders verbatim. Degrade it to the label rather than showing markup.
    out = EMPTY_LINK_RE.sub(lambda m: m.group(1), out)
    # An empty variable leaves the space that preceded it; trim per line so a
    # blank `reason` doesn't post as a trailing space.
    return "\n".join(line.rstrip() for line in out.split("\n")).strip("\n")


def resolve(name, dh, locale, proj_id=None, root=None):
    """-> (path, layer). Project override wins regardless of locale."""
    root = root or paths.skill_root()
    candidates = []
    if proj_id:
        candidates.append((paths.project_messages_dir(dh, proj_id) / name, LAYER_PROJECT))
    candidates.append((paths.messages_dir(dh, locale) / name, LAYER_USER))
    candidates.append((root / "templates" / "messages" / locale / name, LAYER_SHIPPED))
    for path, layer in candidates:
        if path.exists():
            return path, layer
    raise RenderError(
        "no template %r for locale %r (looked in: %s)"
        % (name, locale, ", ".join(str(p) for p, _ in candidates))
    )


def render_named(name, values, dh, locale, proj_id=None, root=None):
    path, _layer = resolve(name, dh, locale, proj_id=proj_id, root=root)
    return render(path.read_text(encoding="utf-8"), values)


def inventory(dh, locale, proj_id=None, root=None):
    """Every template with the layer it resolved from — what `messages` prints."""
    root = root or paths.skill_root()
    names = set()
    shipped = root / "templates" / "messages" / locale
    for directory in (shipped, paths.messages_dir(dh, locale)):
        if directory.is_dir():
            names.update(p.name for p in directory.glob("*.md"))
    if proj_id:
        pdir = paths.project_messages_dir(dh, proj_id)
        if pdir.is_dir():
            names.update(p.name for p in pdir.glob("*.md"))
    out = []
    for name in sorted(names):
        path, layer = resolve(name, dh, locale, proj_id=proj_id, root=root)
        out.append((name, layer, path))
    return out
