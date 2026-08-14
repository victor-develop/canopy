import json

import pytest

from canopy import canvas as canvas_mod
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


# -- canvas -------------------------------------------------------------------

def build_tree():
    tree = store.Tree.new("pay", "C1-1.0", "支付超时", "A君")
    tree.add_child("C1-1.0", "C1-2.0", "慢查询定位", owner="E君")
    tree.set_status("C1-2.0", "done")
    return tree


def test_canvas_marks_done_and_nests():
    text = canvas_mod.render(build_tree(), states={
        "C1-1.0": {"raw_permalink": "https://x/p1", "feed_permalink": "https://x/f1"},
    })
    assert "• `1` 支付超时" in text
    assert "  - ✔ `1.a` 慢查询定位" in text
    assert "[feed](https://x/f1)" in text


def test_canvas_permalink_is_the_file_until_a_link_is_set(dh, repo):
    tree = build_tree()
    tree.save(dh)
    assert canvas_mod.permalink(tree, dh).startswith("file://")
    canvas_mod.set_link(dh, "pay", "https://x.slack.com/canvas/F1")
    assert canvas_mod.permalink(store.Tree.load(dh, "pay"), dh) == \
        "https://x.slack.com/canvas/F1"
