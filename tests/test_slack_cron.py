import json
from pathlib import Path

import pytest

from canopy import config as config_mod
from canopy import cron, store
from canopy.errors import SlackError
from canopy.slack import Slack


def recorder(responses):
    calls = []

    def run(argv, stdin=""):
        calls.append((argv, stdin))
        code, out = responses.pop(0) if responses else (0, "")
        return code, out, ""

    return run, calls


# -- slackcli adapter ---------------------------------------------------------

def test_thread_read_normalises_messages():
    payload = json.dumps({"messages": [
        {"ts": "2.0", "user": "U2", "text": "b"},
        {"ts": "1.0", "user": "U1", "text": "a"},
    ]})
    run, calls = recorder([(0, payload)])
    msgs = Slack(run=run).thread("C1", "1.0")
    assert [m["ts"] for m in msgs] == ["1.0", "2.0"]     # oldest first
    assert "--thread-ts" in calls[0][0] and "--json" in calls[0][0]


def test_new_messages_excludes_the_cursor_itself():
    payload = json.dumps([{"ts": "1.0", "text": "old"}, {"ts": "2.0", "text": "new"}])
    run, _ = recorder([(0, payload)])
    msgs = Slack(run=run).new_messages("C1", "1.0", "1.0")
    assert [m["ts"] for m in msgs] == ["2.0"]


def test_latest_ts_falls_back_to_the_cursor_when_nothing_is_new():
    run, _ = recorder([(0, "[]")])
    assert Slack(run=run).latest_ts("C1", "1.0", after_ts="5.0") == "5.0"


def test_post_reads_the_ts_out_of_json():
    run, calls = recorder([(0, json.dumps({"ok": True, "ts": "1699000009.000200"}))])
    assert Slack(run=run).post("C1", "hi") == "1699000009.000200"
    assert "--recipient-id" in calls[0][0]


def test_post_reads_the_ts_out_of_plain_output():
    run, _ = recorder([(0, "Message sent to #pay (1699000009.000200)")])
    assert Slack(run=run).post("C1", "hi") == "1699000009.000200"


def test_post_without_a_ts_is_an_error_not_a_guess():
    run, _ = recorder([(0, "sent!")])
    with pytest.raises(SlackError):
        Slack(run=run).post("C1", "hi")


def test_nonzero_exit_raises():
    def run(argv, stdin=""):
        return 1, "", "not_in_channel"
    with pytest.raises(SlackError) as exc:
        Slack(run=run).thread("C1", "1.0")
    assert "not_in_channel" in str(exc.value)


def test_non_json_read_raises():
    run, _ = recorder([(0, "you are not logged in")])
    with pytest.raises(SlackError):
        Slack(run=run).thread("C1", "1.0")


def test_workspace_is_passed_through_when_configured():
    run, calls = recorder([(0, "[]")])
    Slack(run=run, workspace="aftership").thread("C1", "1.0")
    assert "--workspace" in calls[0][0]


# -- permalinks ---------------------------------------------------------------

def test_permalink_uses_the_configured_workspace():
    cfg = {"slack_workspace_url": "https://x.slack.com"}
    assert config_mod.permalink(cfg, "C1", "1699000001.000100") == \
        "https://x.slack.com/archives/C1/p1699000001000100"


def test_permalink_degrades_instead_of_crashing_a_tick():
    assert config_mod.permalink({}, "C1", "1.0") == "/archives/C1/p10"


# -- cron ---------------------------------------------------------------------

def test_install_replaces_only_the_canopy_line():
    existing = "0 9 * * * backup.sh\n*/5 * * * * old-canopy # canopy\n"
    run, calls = recorder([(0, existing), (0, "")])
    payload = cron.install("/bin/tick", 7, data_home="/data", run=run)
    assert "backup.sh" in payload
    assert payload.count("# canopy") == 1
    assert "*/7 * * * *" in payload and "CANOPY_DATA_HOME=/data" in payload


def test_uninstall_keeps_other_jobs():
    existing = "0 9 * * * backup.sh\n*/5 * * * * tick # canopy\n"
    run, _ = recorder([(0, existing), (0, "")])
    payload = cron.uninstall(run=run)
    assert payload.strip() == "0 9 * * * backup.sh"


def test_empty_crontab_exit_code_is_not_an_error():
    run, _ = recorder([(1, "")])
    assert cron.read_crontab(run=run) == ""


def test_the_tick_runs_inside_a_login_shell():
    """cron reads no profile, so the environment a runner's auth lives in is not
    there. `codex` reads its provider key from an env var a profile exports:
    from a terminal it worked, from cron every worker died two seconds in."""
    got = cron.line("/bin/python3 /skill/canopy_main.py tick", 1,
                    data_home="/data", login_shell="/bin/zsh")
    assert got.startswith("*/1 * * * * /bin/zsh -lc ")
    assert "CANOPY_DATA_HOME=/data /bin/python3 /skill/canopy_main.py tick" in got
    assert got.endswith("# canopy")


def test_the_command_is_quoted_as_one_argument():
    got = cron.line("/bin/tick", 5, data_home="/data", login_shell="/bin/zsh")
    inner = got.split("-lc ", 1)[1].rsplit(" # canopy", 1)[0]
    assert inner.startswith("'") and inner.endswith("'")


def test_no_login_shell_leaves_the_bare_command():
    """A runner that needs nothing from a profile should not pay for a shell."""
    got = cron.line("/bin/tick", 5, data_home="/data")
    assert got == "*/5 * * * * CANOPY_DATA_HOME=/data /bin/tick # canopy"


def test_a_percent_in_the_command_is_escaped():
    """crontab reads an unescaped % as a newline and feeds the rest to stdin."""
    got = cron.line("/bin/tick", 5, data_home="/data/100%done")
    assert "100\\%done" in got and "100%done" not in got


def test_install_carries_the_login_shell_through():
    run, _ = recorder([(0, ""), (0, "")])
    payload = cron.install("/bin/tick", 5, data_home="/data",
                           login_shell="/bin/bash", run=run)
    assert "/bin/bash -lc " in payload


# -- link degradation and the API backend -------------------------------------

def test_slackcli_edits_lose_rich_links_so_they_degrade():
    """slackcli's edit HTML-escapes the text; `&lt;url|label&gt;` is a dead link."""
    run, calls = recorder([(0, "")])
    Slack(run=run).update("C1", "1.0", "见 <https://x/p1|thread> 吧")
    sent = calls[0][0][calls[0][0].index("--message") + 1]
    assert sent == "见 thread https://x/p1 吧"


def test_slackcli_posts_keep_rich_links():
    run, calls = recorder([(0, '{"ts": "1.0"}')])
    Slack(run=run).post("C1", "见 <https://x/p1|thread>")
    sent = calls[0][0][calls[0][0].index("--message") + 1]
    assert sent == "见 <https://x/p1|thread>"


def test_api_backend_keeps_rich_links_on_edit():
    seen = {}

    def http(url, data, token):
        seen.update(url=url, data=data, token=token)
        return '{"ok": true, "ts": "1.0"}'

    Slack(backend="api", token="xoxp-test", http=http).update(
        "C1", "1.0", "见 <https://x/p1|thread>")
    assert seen["url"].endswith("chat.update")
    assert seen["data"]["text"] == "见 <https://x/p1|thread>"


def test_api_backend_reports_slack_errors():
    def http(url, data, token):
        return '{"ok": false, "error": "message_not_found"}'
    with pytest.raises(SlackError) as exc:
        Slack(backend="api", token="t", http=http).update("C1", "1.0", "x")
    assert "message_not_found" in str(exc.value)


def test_api_backend_refuses_without_a_token(monkeypatch):
    monkeypatch.delenv("CANOPY_SLACK_TOKEN", raising=False)
    with pytest.raises(SlackError) as exc:
        Slack.from_config({"slack_backend": "api"})
    # Names the env var; never reads a token from a file.
    assert "CANOPY_SLACK_TOKEN" in str(exc.value)


def test_from_config_defaults_to_slackcli():
    assert Slack.from_config({}).backend == "slackcli"


def test_a_patched_slackcli_keeps_labels_on_edit():
    """`slack_cli_escapes_on_edit: false` for a CLI that sends parse=none."""
    run, calls = recorder([(0, "")])
    Slack(run=run, escapes_on_edit=False).update("C1", "1.0", "见 <https://x/p1|全文>")
    sent = calls[0][0][calls[0][0].index("--message") + 1]
    assert sent == "见 <https://x/p1|全文>"


def test_from_config_reads_the_escaping_flag():
    assert Slack.from_config({}).escapes_on_edit is True
    assert Slack.from_config({"slack_cli_escapes_on_edit": False}).escapes_on_edit is False


# -- the cron entry follows whether anything is watched ------------------------

def make_tree(dh, proj_id, status="active"):
    tree = store.Tree.new(proj_id, "C1-1.0", "支付超时", "A君")
    tree.nodes["C1-1.0"]["status"] = status
    tree.save(dh)
    return tree


def test_sync_installs_when_something_is_watched(dh):
    from canopy import schedule
    make_tree(dh, "pay")
    run, calls = recorder([(1, ""), (0, "")])      # empty crontab, then the write
    assert schedule.sync(dh, {}, run=run) == schedule.INSTALLED
    assert "# canopy" in calls[-1][1]


def test_sync_removes_the_entry_when_the_last_node_is_untracked(dh):
    """`untrack` should not leave a cron waking every 5 minutes for nothing."""
    from canopy import schedule
    make_tree(dh, "pay", status="untracked")
    existing = "*/5 * * * * tick # canopy\n"
    run, calls = recorder([(0, existing), (0, existing), (0, "")])
    assert schedule.sync(dh, {}, run=run) == schedule.REMOVED
    assert calls[-1][1].strip() == ""


def test_sync_is_a_no_op_when_already_correct(dh):
    from canopy import schedule
    make_tree(dh, "pay")
    run, _ = recorder([(0, "*/5 * * * * tick # canopy\n")])
    assert schedule.sync(dh, {}, run=run) == schedule.UNCHANGED


def test_one_parked_tree_does_not_stop_the_others(dh):
    from canopy import schedule
    make_tree(dh, "pay", status="untracked")
    make_tree(dh, "edd", status="active")
    run, _ = recorder([(0, "*/5 * * * * tick # canopy\n")])
    assert schedule.sync(dh, {}, run=run) == schedule.UNCHANGED


def test_the_suite_cannot_touch_the_real_crontab(fake_crontab):
    """Guard for the guard: `canopy.cron._run` is stubbed for every test."""
    from canopy import cron
    cron.install("/bin/tick", 5, data_home="/tmp/x")
    assert "# canopy" in fake_crontab["text"]


def test_the_effects_door_refuses_anything_it_was_not_taught(no_machine_effects):
    """The guard that replaces three symbol patches: a new side effect added
    tomorrow fails here instead of on someone's laptop."""
    import pytest as _pytest
    from canopy import effects as effects_mod

    # EffectEscaped derives from BaseException so application code — which
    # catches Exception to keep a tick alive — cannot swallow the alarm.
    assert not issubclass(effects_mod.EffectEscaped, Exception)
    with _pytest.raises(effects_mod.EffectEscaped):
        no_machine_effects.spawn(["some-new-daemon"])
    assert effects_mod.DEFAULT is no_machine_effects       # nothing bypasses it


def test_recording_effects_report_what_was_attempted():
    from canopy import effects as effects_mod
    rec = effects_mod.Recording(run=lambda argv, stdin="": (0, "ok", ""))
    assert rec.run(["crontab", "-l"]) == (0, "ok", "")
    assert rec.calls[0]["argv"] == ["crontab", "-l"]
    rec.open_url("http://127.0.0.1:1/")
    assert rec.opened == ["http://127.0.0.1:1/"]


def test_no_cron_line_when_something_else_owns_the_waking():
    """Every tick calls sync, so without this an external scheduler's own first
    tick reinstalls the crontab line it was started to replace — and the tree
    gets woken twice a minute by two schedulers."""
    from canopy import schedule
    calls = []
    outcome = schedule.sync(Path("/nope"), {"schedule_backend": "none"},
                            run=lambda *a, **k: calls.append(a) or (0, "", ""))
    assert outcome == schedule.UNCHANGED
    assert calls == []


def test_the_cron_backend_is_still_the_default():
    from canopy import config
    assert config.DEFAULTS["schedule_backend"] == "cron"
