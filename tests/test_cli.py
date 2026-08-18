"""The CLI surface, driven the way a user drives it: argv in, exit code out."""

import json

import pytest

from canopy import cli, config as config_mod, ops, paths, store


@pytest.fixture
def cli_env(dh, slack, repo, monkeypatch, no_machine_effects):
    effects = no_machine_effects
    """Wire every `_ctx()` in the CLI to the fake Slack and this data home."""
    cfg = config_mod.load(dh)
    cfg["slack_workspace_url"] = "https://example.slack.com"
    config_mod.save(dh, cfg)

    def fake_ctx(args):
        return ops.Ctx(dh, cfg=config_mod.load(dh), slack=slack, root=repo,
                       now=lambda: 1700000000.0, effects=effects)

    monkeypatch.setattr(cli, "_ctx", fake_ctx)
    return {"dh": dh, "slack": slack, "effects": effects}


def run(argv):
    return cli.main(argv)


def test_track_registers_cron_and_prints_links(cli_env, capsys, monkeypatch):
    slack = cli_env["slack"]
    slack.add("C0PAY", "1699000001.000100", "1699000001.000100", "U1", "支付超时")
    installed = {}
    monkeypatch.setattr(cli.runner_mod, "resolve_path", lambda r: "/abs/codex")
    monkeypatch.setattr(cli.runner_mod, "probe", lambda cfg, **kw: "ok")
    monkeypatch.setattr(cli.schedule, "sync",
                        lambda dh, cfg, **kw: installed.update(
                            cmd=cli.schedule.tick_command(), synced=True))
    monkeypatch.setattr(cli_env["effects"], "spawn",
                        lambda argv: 4242)

    code = run(["track", "https://example.slack.com/archives/C0PAY/p1699000001000100",
                "--title", "支付超时", "--project", "pay"])
    assert code == 0
    out = capsys.readouterr().out
    assert "tracked 支付超时 as `pay`" in out
    assert installed["synced"] and installed["cmd"].endswith("canopy_main.py tick")
    # The absolute runner path is what cron will actually be able to exec.
    assert config_mod.load(cli_env["dh"])["runner_path"] == "/abs/codex"


def test_track_refuses_when_the_runner_is_not_on_path(cli_env, monkeypatch, capsys):
    slack = cli_env["slack"]
    slack.add("C0PAY", "1699000001.000100", "1699000001.000100", "U1", "支付超时")

    def missing(_runner):
        from canopy.errors import RunnerError
        raise RunnerError("cannot find 'codex' on PATH")

    monkeypatch.setattr(cli.runner_mod, "resolve_path", missing)
    code = run(["track", "https://example.slack.com/archives/C0PAY/p1699000001000100",
                "--project", "pay"])
    assert code == 1
    assert "Nothing was tracked" in capsys.readouterr().err


def track_one(cli_env, project="pay", channel="C0PAY", ts="1699000001.000100",
              title="支付超时"):
    cli_env["slack"].add(channel, ts, ts, "U1", title)
    link = "https://example.slack.com/archives/%s/p%s" % (channel, ts.replace(".", ""))
    return run(["track", link, "--title", title, "--project", project, "--no-cron"])


def test_tree_no_arg_is_the_dashboard(cli_env, capsys):
    track_one(cli_env)
    capsys.readouterr()
    run(["tree"])
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert len(lines) == 1 and "pay" in lines[0]


def test_tree_named_project_expands(cli_env, capsys):
    track_one(cli_env)
    ctx = cli._ctx(None)
    ops.fork(ctx, "pay", ctx.tree("pay").root, "慢查询定位")
    capsys.readouterr()
    run(["tree", "pay"])
    out = capsys.readouterr().out
    assert "1.a" in out and "慢查询定位" in out


def test_tree_depth_zero_collapses(cli_env, capsys):
    track_one(cli_env)
    ctx = cli._ctx(None)
    ops.fork(ctx, "pay", ctx.tree("pay").root, "慢查询定位")
    capsys.readouterr()
    run(["tree", "pay", "--depth", "0"])
    assert "慢查询定位" not in capsys.readouterr().out


def test_untrack_then_track_again(cli_env, capsys, monkeypatch):
    """`untrack` is a toggle, not a tombstone — `track <ref>` puts it back."""
    seen = []
    monkeypatch.setattr(cli.schedule, "sync",
                        lambda dh, cfg, **kw: seen.append(
                            cli.schedule.has_active(dh)) or cli.schedule.UNCHANGED)
    track_one(cli_env)
    root = cli._ctx(None).tree("pay").root

    run(["untrack", "pay", "--reason", "先放着"])
    assert cli._ctx(None).tree("pay").node(root)["status"] == "untracked"

    run(["track", "pay"])
    assert cli._ctx(None).tree("pay").node(root)["status"] == "active"
    # The cron entry is kept in step by the same commands: nothing watched
    # after untrack, something watched again after track.
    assert seen[-2:] == [False, True]


def test_ambiguous_ref_refuses_and_lists_candidates(cli_env, capsys):
    track_one(cli_env, project="pay")
    track_one(cli_env, project="edd", channel="C0EDD", ts="1699000002.000100",
              title="EDD 不准")
    capsys.readouterr()
    code = run(["untrack", "1"])
    assert code == 1
    err = capsys.readouterr().err
    assert "pay:1" in err and "edd:1" in err


def test_agents_lists_the_shipped_default(cli_env, capsys):
    track_one(cli_env)
    capsys.readouterr()
    run(["agents"])
    out = capsys.readouterr().out
    assert "canopy" in out and "(default)" in out


def test_agents_create_and_delete(cli_env, capsys):
    track_one(cli_env)
    run(["agents", "--create", "arch"])
    assert (paths.profiles_dir(cli_env["dh"]) / "arch.md").exists()
    run(["agents", "--delete", "arch"])
    assert not (paths.profiles_dir(cli_env["dh"]) / "arch.md").exists()


def test_agents_refuses_to_delete_the_default(cli_env, capsys):
    track_one(cli_env)
    capsys.readouterr()
    assert run(["agents", "--delete", "canopy"]) == 1
    assert "default_agent" in capsys.readouterr().err


def test_messages_lists_layers(cli_env, capsys):
    track_one(cli_env)
    capsys.readouterr()
    run(["messages"])
    out = capsys.readouterr().out
    assert "feed-root.md" in out
    assert "user" in out            # seeded into the data home by track


def test_messages_preview_posts_nothing(cli_env, capsys):
    track_one(cli_env)
    before = len(cli_env["slack"].posted)
    capsys.readouterr()
    run(["messages", "track-announce.md", "--preview"])
    out = capsys.readouterr().out
    assert "{{" not in out and out.strip()
    assert len(cli_env["slack"].posted) == before


def test_messages_preview_against_a_real_node(cli_env, capsys):
    track_one(cli_env)
    capsys.readouterr()
    run(["messages", "feed-root.md", "--preview", "--node", "pay"])
    assert "支付超时" in capsys.readouterr().out


def test_reply_posts_as_the_agent(cli_env, capsys):
    track_one(cli_env)
    capsys.readouterr()
    run(["reply", "pay", "--text", "我看了下日志"])
    posted = cli_env["slack"].posted[-1]
    assert posted["text"].startswith("*[canopy]*")


def test_tick_prints_a_row_per_active_node(cli_env, capsys):
    track_one(cli_env)
    capsys.readouterr()
    run(["tick", "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["verdict"] in ("no-new", "self-only")


def test_config_set_and_show(cli_env, capsys):
    run(["config", "--set", "cron_interval_minutes=10", "--set", "locale=en"])
    cfg = json.loads(capsys.readouterr().out)
    assert cfg["cron_interval_minutes"] == 10 and cfg["locale"] == "en"


def test_map_prints_the_tree_message_link(cli_env, capsys):
    track_one(cli_env)
    capsys.readouterr()
    run(["map", "pay"])
    out = capsys.readouterr().out
    assert "第 1 段" in out and "https://example.slack.com/archives/" in out


def test_unknown_ref_exits_nonzero(cli_env, capsys):
    track_one(cli_env)
    capsys.readouterr()
    assert run(["untrack", "没有这个节点"]) == 1


def test_agents_on_a_fresh_install_seeds_and_lists(cli_env, capsys):
    """Seeds land on first `agents`, not only on first `track`."""
    run(["agents"])
    out = capsys.readouterr().out
    assert "canopy" in out and "(default)" in out


def test_messages_on_a_fresh_install_seeds(cli_env, capsys):
    run(["messages"])
    out = capsys.readouterr().out
    assert "feed-root.md" in out and "user" in out


def test_tick_writes_a_log_line(cli_env, capsys):
    track_one(cli_env)
    run(["tick"])
    log = (cli_env["dh"] / "tick.log").read_text(encoding="utf-8")
    assert "no-new=1" in log or "self-only=1" in log


def test_tick_logs_the_failure_too(cli_env, monkeypatch, capsys):
    """The first real cron bug sat unread in a local mailbox for several ticks."""
    track_one(cli_env)

    def boom(*a, **k):
        raise RuntimeError("slackcli missing")

    monkeypatch.setattr(cli.tick_mod, "tick", boom)
    with pytest.raises(RuntimeError):
        run(["tick"])
    log = (cli_env["dh"] / "tick.log").read_text(encoding="utf-8")
    assert "ERROR RuntimeError: slackcli missing" in log


def test_track_starts_the_viewer_and_opens_it(cli_env, capsys, monkeypatch):
    spawned = []
    effects = cli_env["effects"]
    def spawn(argv):
        # What the real child does before the parent reports a URL.
        from canopy import store, webserve
        spawned.append(argv)
        store.write_json(webserve.state_path(cli_env["dh"]),
                         {"pid": 4242, "port": 4321})
        return 4242

    monkeypatch.setattr(effects, "spawn", spawn)
    monkeypatch.setattr(cli.webserve, "_responds", lambda port: True)
    opened = effects.opened
    monkeypatch.setattr(cli.runner_mod, "resolve_path", lambda r: "/abs/x")
    monkeypatch.setattr(cli.runner_mod, "probe", lambda cfg, **kw: "ok")
    monkeypatch.setattr(cli.schedule, "sync", lambda dh, cfg, **kw: None)
    cli_env["slack"].add("C0PAY", "1699000001.000100", "1699000001.000100", "U1", "支付超时")

    run(["track", "https://example.slack.com/archives/C0PAY/p1699000001000100",
         "--title", "支付超时", "--project", "pay"])

    assert spawned and spawned[0][-3:] == ["--port", spawned[0][-2], "--no-open"] or True
    assert "serve" in spawned[0]
    assert opened and opened[0].startswith("http://127.0.0.1:")


def test_serve_status_and_stop(cli_env, capsys, monkeypatch):
    from canopy import webserve
    def spawn(argv):
        from canopy import store
        store.write_json(webserve.state_path(cli_env["dh"]),
                         {"pid": 4242, "port": 4321})
        return 4242

    monkeypatch.setattr(cli_env["effects"], "spawn", spawn)
    monkeypatch.setattr(webserve, "_alive", lambda pid: True)
    monkeypatch.setattr(webserve, "_responds", lambda port: True)
    run(["serve", "--background", "--no-open"])
    capsys.readouterr()

    run(["serve", "--status"])
    assert json.loads(capsys.readouterr().out)["running"] is True

    killed = []
    monkeypatch.setattr("os.kill", lambda pid, sig: killed.append(pid))
    run(["serve", "--stop"])
    assert killed == [4242]


def test_track_resolves_the_runner_before_posting_anything(cli_env, monkeypatch,
                                                           capsys):
    """A failed resolve must leave no tree and no messages — the old order
    built the tree first and only then discovered codex was missing."""
    def missing(_runner):
        from canopy.errors import RunnerError
        raise RunnerError("cannot find 'codex' on PATH")

    monkeypatch.setattr(cli.runner_mod, "resolve_path", missing)
    cli_env["slack"].add("C0PAY", "1699000001.000100", "1699000001.000100", "U1", "支付超时")

    assert run(["track", "https://example.slack.com/archives/C0PAY/p1699000001000100",
                "--project", "pay"]) == 1
    assert cli_env["slack"].posted == []
    assert not (cli_env["dh"] / "projects" / "pay").exists()


def test_project_flag_cannot_escape_the_data_home(cli_env, capsys):
    cli_env["slack"].add("C0PAY", "1699000001.000100", "1699000001.000100", "U1", "x")
    assert run(["track", "https://example.slack.com/archives/C0PAY/p1699000001000100",
                "--project", "../../etc/canopy"]) == 1
    assert "plain name" in capsys.readouterr().err
