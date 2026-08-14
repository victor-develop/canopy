import pytest

from canopy import noderef, store
from canopy.errors import AmbiguousRefError, NodeRefError


def build(proj_id="pay-timeout"):
    tree = store.Tree.new(proj_id, "C1-1.0", "支付超时", "A君")
    tree.add_child("C1-1.0", "C1-2.0", "慢查询定位")
    tree.add_child("C1-1.0", "C1-3.0", "重试风暴")
    tree.add_child("C1-2.0", "C1-4.0", "索引方案")
    tree.add_child("C1-4.0", "C1-5.0", "灰度计划")
    return tree


def test_alias_levels_cycle_number_letter_roman():
    tree = build()
    alias = noderef.aliases(tree)
    assert alias["C1-1.0"] == "1"
    assert alias["C1-2.0"] == "1.a"
    assert alias["C1-3.0"] == "1.b"
    assert alias["C1-4.0"] == "1.a.i"
    # Level 3 wraps back to numbers.
    assert alias["C1-5.0"] == "1.a.i.1"


def test_aliases_shift_when_a_sibling_is_inserted():
    """Positional by design: this is why durable things store node ids."""
    tree = build()
    before = noderef.aliases(tree)["C1-3.0"]
    tree.nodes["C1-1.0"]["children"].insert(0, "C1-9.0")
    tree.nodes["C1-9.0"] = {"parent": "C1-1.0", "children": [], "title": "新插入",
                            "status": "active"}
    after = noderef.aliases(tree)["C1-3.0"]
    assert before == "1.b" and after == "1.c"


def test_resolve_by_alias():
    trees = {"pay-timeout": build()}
    assert noderef.resolve("1.a", trees) == ("pay-timeout", "C1-2.0")


def test_resolve_by_project_id_gives_its_root():
    trees = {"pay-timeout": build()}
    assert noderef.resolve("pay-timeout", trees) == ("pay-timeout", "C1-1.0")


def test_resolve_by_title_substring():
    trees = {"pay-timeout": build()}
    assert noderef.resolve("慢查询", trees) == ("pay-timeout", "C1-2.0")


def test_resolve_by_node_id_and_prefix():
    trees = {"pay-timeout": build()}
    assert noderef.resolve("C1-4.0", trees) == ("pay-timeout", "C1-4.0")


def test_bare_alias_across_two_projects_is_ambiguous():
    trees = {"pay-timeout": build("pay-timeout"), "edd": build("edd")}
    with pytest.raises(AmbiguousRefError) as exc:
        noderef.resolve("1.a", trees)
    assert sorted(exc.value.candidates) == ["edd:1.a", "pay-timeout:1.a"]


def test_qualified_ref_disambiguates():
    trees = {"pay-timeout": build("pay-timeout"), "edd": build("edd")}
    assert noderef.resolve("edd:1.a", trees) == ("edd", "C1-2.0")


def test_unknown_ref_raises():
    trees = {"pay-timeout": build()}
    with pytest.raises(NodeRefError):
        noderef.resolve("nope", trees)


def test_empty_ref_raises():
    with pytest.raises(NodeRefError):
        noderef.resolve("  ", {"pay-timeout": build()})
