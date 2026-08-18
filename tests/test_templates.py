import pytest

from canopy import paths, templates
from canopy.errors import RenderError

BODY = """---
moment: track
vars: [title, reason]
---
*{{title}}* {{reason}}
"""


def test_parse_front_matter_list_and_scalar():
    meta, body = templates.parse(BODY)
    assert meta["moment"] == "track"
    assert meta["vars"] == ["title", "reason"]
    assert body.strip() == "*{{title}}* {{reason}}"


def test_render_substitutes_declared_vars():
    assert templates.render(BODY, {"title": "支付超时", "reason": "慢"}) == "*支付超时* 慢"


def test_declared_but_empty_renders_as_nothing():
    # `reason` on a plain `done` is legitimately empty.
    assert templates.render(BODY, {"title": "x"}) == "*x*"


def test_undeclared_var_is_a_hard_error():
    bad = BODY.replace("{{reason}}", "{{parent_permalink}}")
    with pytest.raises(RenderError) as exc:
        templates.render(bad, {"title": "x", "parent_permalink": "#"})
    assert "parent_permalink" in str(exc.value)


def test_missing_front_matter_is_an_error():
    with pytest.raises(RenderError):
        templates.parse("no front matter here")


def test_resolution_order_user_beats_shipped(dh, repo):
    shipped, layer = templates.resolve("reply.md", dh, "zh", root=repo)
    assert layer == templates.LAYER_SHIPPED

    user_dir = paths.messages_dir(dh, "zh")
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "reply.md").write_text(shipped.read_text(encoding="utf-8"),
                                       encoding="utf-8")
    _path, layer = templates.resolve("reply.md", dh, "zh", root=repo)
    assert layer == templates.LAYER_USER


def test_project_override_wins_over_locale(dh, repo):
    proj_dir = paths.project_messages_dir(dh, "pay")
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "reply.md").write_text(BODY, encoding="utf-8")
    _path, layer = templates.resolve("reply.md", dh, "en", proj_id="pay", root=repo)
    assert layer == templates.LAYER_PROJECT


def test_every_shipped_template_renders_from_its_own_vars(repo):
    """Ships-broken is a failed tick in production, so check all locales here."""
    for locale in ("zh", "en"):
        directory = repo / "templates" / "messages" / locale
        for path in sorted(directory.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            declared = templates.declared_vars(text)
            values = dict((name, "x") for name in declared)
            rendered = templates.render(text, values)
            assert "{{" not in rendered, "%s left a placeholder" % path


def test_locales_declare_the_same_vars(repo):
    zh = repo / "templates" / "messages" / "zh"
    en = repo / "templates" / "messages" / "en"
    assert sorted(p.name for p in zh.glob("*.md")) == \
        sorted(p.name for p in en.glob("*.md"))
    for path in sorted(zh.glob("*.md")):
        other = en / path.name
        a = templates.parse(path.read_text(encoding="utf-8"))[0]
        b = templates.parse(other.read_text(encoding="utf-8"))[0]
        assert a["moment"] == b["moment"], path.name
        assert sorted(a["vars"]) == sorted(b["vars"]), path.name


def test_inventory_reports_layers(dh, repo):
    rows = templates.inventory(dh, "zh", root=repo)
    names = [r[0] for r in rows]
    assert "feed-root.md" in names
    assert all(layer == templates.LAYER_SHIPPED for _n, layer, _p in rows)


def test_empty_link_degrades_to_its_label():
    """`<{{url}}|看整棵树>` with no url must not post as literal `<|看整棵树>`."""
    body = """---
moment: x
vars: [url]
---
a <{{url}}|看整棵树> b
"""
    assert templates.render(body, {}) == "a 看整棵树 b"
    assert templates.render(body, {"url": "#x"}) == "a <#x|看整棵树> b"


def test_refresh_updates_untouched_copies_and_keeps_edits(dh, repo):
    """A skill update must reach the user's copies, without eating their edits."""
    paths.seed(dh, "zh", root=repo)
    user_dir = paths.messages_dir(dh, "zh")
    (user_dir / "reply.md").write_text("stale shipped copy", encoding="utf-8")
    (user_dir / "feed-entry.md").write_text("---\nmoment: x\nvars: []\n---\nmine\n",
                                            encoding="utf-8")

    # Both differ from shipped, so without --force they are reported, not eaten.
    updated, kept = paths.refresh(dh, "zh", root=repo)
    assert {p.name for p in kept} == {"feed-entry.md", "reply.md"}
    assert updated == []

    updated, kept = paths.refresh(dh, "zh", root=repo, force=True)
    assert {p.name for p in updated} == {"feed-entry.md", "reply.md"}


def test_refresh_reports_instead_of_overwriting_when_asked(dh, repo):
    paths.seed(dh, "zh", root=repo)
    user_dir = paths.messages_dir(dh, "zh")
    (user_dir / "reply.md").write_text("my wording", encoding="utf-8")

    updated, kept = paths.refresh(dh, "zh", root=repo, force=False)
    assert [p.name for p in kept] == ["reply.md"] and updated == []
    assert (user_dir / "reply.md").read_text(encoding="utf-8") == "my wording"

    updated, kept = paths.refresh(dh, "zh", root=repo, force=True)
    assert [p.name for p in updated] == ["reply.md"] and kept == []


def test_channel_messages_open_with_a_divider(repo):
    """Canopy posts the digest and the map back to back; without a rule at the
    top they read as one wall of text."""
    for locale in ("zh", "en"):
        for name in ("feed-root.md", "feed-fork.md", "feed-segment.md",
                     "tree-map.md", "tree-map-more.md"):
            path = repo / "templates" / "messages" / locale / name
            body = templates.parse(path.read_text(encoding="utf-8"))[1]
            assert body.lstrip().startswith("─"), "%s/%s" % (locale, name)


def test_in_thread_messages_do_not_get_a_divider(repo):
    """A rule inside a thread is noise: those are one-liners between humans."""
    for locale in ("zh", "en"):
        for name in ("reply.md", "track-announce.md", "fork-announce.md",
                     "return-post.md", "status-untracked.md"):
            path = repo / "templates" / "messages" / locale / name
            body = templates.parse(path.read_text(encoding="utf-8"))[1]
            assert not body.lstrip().startswith("─"), "%s/%s" % (locale, name)


def test_the_digest_is_pinnable_at_a_glance(repo):
    """The digest is the one message an observer pins, so it says so."""
    for locale in ("zh", "en"):
        for name in ("feed-root.md", "feed-fork.md"):
            body = templates.parse(
                (repo / "templates" / "messages" / locale / name)
                .read_text(encoding="utf-8"))[1]
            assert "📌" in body
