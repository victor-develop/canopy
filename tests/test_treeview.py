from canopy import store, treeview


def build():
    tree = store.Tree.new("pay-timeout", "C1-1.0", "支付超时", "A君")
    tree.add_child("C1-1.0", "C1-2.0", "慢查询定位", owner="E君")
    tree.add_child("C1-1.0", "C1-3.0", "重试风暴", owner="A君", status="untracked")
    tree.add_child("C1-2.0", "C1-4.0", "索引方案", owner="F君")
    tree.set_status("C1-4.0", "untracked")
    return {"pay-timeout": tree}


def test_no_arg_is_one_rollup_line_per_root():
    lines = treeview.render(build())
    assert len(lines) == 1
    assert "pay-timeout" in lines[0]
    assert "1 active" in lines[0] and "2 untracked" in lines[0]


def test_depth_expands_that_many_levels_and_rolls_up_the_rest():
    lines = treeview.render(build(), start=("pay-timeout", "C1-1.0"), depth=1)
    assert len(lines) == 3           # root + two children
    child = [l for l in lines if "慢查询定位" in l][0]
    assert "1 untracked" in child     # its own child collapsed into a rollup


def test_depth_all_expands_everything():
    lines = treeview.render(build(), start=("pay-timeout", "C1-1.0"), depth=None)
    assert len(lines) == 4
    assert any("索引方案" in l for l in lines)


def test_starting_mid_tree_prints_a_breadcrumb():
    lines = treeview.render(build(), start=("pay-timeout", "C1-4.0"), depth=None)
    assert lines[0].startswith("↑ pay-timeout / 1 / 1.a")


def test_lock_is_visible():
    lines = treeview.render(build(), start=("pay-timeout", "C1-1.0"), depth=None,
                            locked={"C1-4.0"})
    assert any("[lock]" in l and "索引方案" in l for l in lines)


def test_rollup_counts_locked_descendants():
    counts = treeview.rollup(build()["pay-timeout"], "C1-1.0", {"C1-4.0"})
    assert counts["locked"] == 1
    assert counts["active"] == 1 and counts["untracked"] == 2
