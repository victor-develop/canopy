import pytest

from canopy import runner
from canopy.errors import RunnerError


def test_codex_argv_has_no_sandbox_and_reads_stdin():
    argv = runner.build_argv({"runner": "codex"}, "/nodes/n1", out_file="/tmp/last")
    assert argv[0:2] == ["codex", "exec"]
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert "--sandbox" not in argv
    assert argv[-1] == "-"                       # prompt arrives on stdin
    assert "-C" in argv and "/nodes/n1" in argv
    assert argv[argv.index("-o") + 1] == "/tmp/last"


def test_claude_argv_skips_permissions_and_takes_no_prompt_arg():
    argv = runner.build_argv({"runner": "claude"}, "/nodes/n1")
    assert argv == ["claude", "-p", "--output-format", "text",
                    "--dangerously-skip-permissions"]


def test_runner_path_overrides_the_binary():
    cfg = {"runner": "codex", "runner_path": "/abs/bin/codex"}
    assert runner.build_argv(cfg, "/nodes/n1")[0] == "/abs/bin/codex"


def test_custom_cmd_is_passed_through():
    cfg = {"runner": {"cmd": ["my-wrapper", "--flag"]}}
    assert runner.build_argv(cfg, "/nodes/n1") == ["my-wrapper", "--flag"]


def test_unknown_runner_names_the_options():
    with pytest.raises(RunnerError) as exc:
        runner.build_argv({"runner": "gpt5"}, "/n")
    assert "codex" in str(exc.value) and "claude" in str(exc.value)


def test_resolve_path_uses_which():
    found = runner.resolve_path("codex", which=lambda n: "/opt/%s" % n)
    assert found == "/opt/codex"


def test_resolve_path_refuses_when_not_on_path():
    with pytest.raises(RunnerError) as exc:
        runner.resolve_path("codex", which=lambda n: None)
    # cron would not find it either, and a silent tree is the failure mode.
    assert "cron" in str(exc.value)


def test_run_prefers_the_output_file_over_stdout(tmp_path):
    out = tmp_path / "last.txt"
    calls = {}

    def fake_exec(argv, prompt, cwd):
        calls["prompt"] = prompt
        out.write_text("the real answer", encoding="utf-8")
        return 0, "noisy stdout", ""

    answer = runner.run({"runner": "codex"}, "PROMPT", tmp_path, out_file=out,
                        exec_fn=fake_exec)
    assert answer == "the real answer"
    assert calls["prompt"] == "PROMPT"


def test_run_falls_back_to_stdout(tmp_path):
    answer = runner.run({"runner": "claude"}, "P", tmp_path,
                        exec_fn=lambda a, p, c: (0, " hello \n", ""))
    assert answer == "hello"


def test_nonzero_exit_raises_with_stderr(tmp_path):
    with pytest.raises(RunnerError) as exc:
        runner.run({"runner": "claude"}, "P", tmp_path,
                   exec_fn=lambda a, p, c: (1, "", "boom"))
    assert "boom" in str(exc.value)


# -- the project's short id ---------------------------------------------------

def test_shortid_accepts_a_clean_answer():
    from canopy import shortid
    assert shortid.sanitize("figma-free-design") == "figma-free-design"


def test_shortid_strips_what_models_add():
    from canopy import shortid
    assert shortid.sanitize("Here you go:\n`Figma-Free Design`\n") == \
        "figma-free-design"


def test_shortid_rejects_prose_and_falls_back():
    from canopy import shortid
    assert shortid.sanitize("I think a good name would be something like this") is None
    assert shortid.sanitize("") is None
    assert shortid.sanitize("---") is None


def test_shortid_returns_none_when_the_runner_fails(tmp_path):
    from canopy import shortid
    from canopy.errors import RunnerError

    def boom(*a, **k):
        raise RunnerError("codex not installed")

    assert shortid.suggest({}, "支付超时", tmp_path, run=boom) is None


def test_track_uses_the_suggested_id(ctx, slack):
    from canopy import ops
    channel, ts = "C0NEW", "1699000900.000100"
    slack.add(channel, ts, ts, "U1", "设计师不用 Figma 的方案")
    link = "https://example.slack.com/archives/%s/p1699000900000100" % channel
    result = ops.track(ctx, link, namer=lambda *a, **k: "figma-free-design")
    assert result["proj_id"] == "figma-free-design"


def test_track_falls_back_to_the_slug_when_the_namer_fails(ctx, slack):
    from canopy import ops
    from canopy.errors import RunnerError

    def boom(*a, **k):
        raise RunnerError("no runner")

    channel, ts = "C0NEW", "1699000900.000100"
    slack.add(channel, ts, ts, "U1", "支付超时")
    link = "https://example.slack.com/archives/%s/p1699000900000100" % channel
    result = ops.track(ctx, link, title="支付超时", namer=boom)
    assert result["proj_id"] == "支付超时"


def test_a_stale_answer_file_is_never_read_back(tmp_path):
    """`claude` and a custom `cmd` take no `-o`, so a file left behind by a
    codex-era run was being returned as this run's answer, every time."""
    out = tmp_path / "last-message.txt"
    out.write_text("OLD ANSWER FROM A PREVIOUS RUN", encoding="utf-8")

    answer = runner.run({"runner": "claude"}, "P", tmp_path, out_file=out,
                        exec_fn=lambda a, p, c: (0, "the actual new answer", ""))
    assert answer == "the actual new answer"


def test_the_answer_file_is_cleared_before_the_run(tmp_path):
    out = tmp_path / "last-message.txt"
    out.write_text("OLD", encoding="utf-8")
    seen = {}

    def exec_fn(argv, prompt, cwd):
        seen["existed"] = out.exists()
        out.write_text("fresh", encoding="utf-8")
        return 0, "", ""

    assert runner.run({"runner": "codex"}, "P", tmp_path, out_file=out,
                      exec_fn=exec_fn) == "fresh"
    assert seen["existed"] is False
