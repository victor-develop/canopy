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


def test_nonzero_exit_keeps_the_reason_not_the_banner(tmp_path):
    """The reason is the last thing printed, and codex prints a lot before it.

    Three real failures in a row logged the identical 530-character string,
    every one of them cut mid-prompt-echo, so what codex actually complained
    about was gone. Head-truncation is the bug, not the length.
    """
    noise = "OpenAI Codex v0.147.0\n" + "You are woken for ONE node. " * 400
    with pytest.raises(RunnerError) as exc:
        runner.run({"runner": "codex"}, "P", tmp_path,
                   exec_fn=lambda a, p, c: (1, "", noise + "\nstream error: 429"))
    assert "stream error: 429" in str(exc.value)
    assert "OpenAI Codex v0.147.0" not in str(exc.value)


def test_nonzero_exit_keeps_both_streams(tmp_path):
    """`err or out` made whichever stream lost unrecoverable."""
    with pytest.raises(RunnerError) as exc:
        runner.run({"runner": "codex"}, "P", tmp_path,
                   exec_fn=lambda a, p, c: (1, "half an answer", "died here"))
    message = str(exc.value)
    assert "died here" in message and "half an answer" in message


def test_failure_detail_says_so_when_there_was_no_output():
    assert "no output" in runner.failure_detail("", "   ")


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


def test_shortid_returns_nothing_when_the_runner_fails(tmp_path):
    from canopy import shortid
    from canopy.errors import RunnerError

    def boom(*a, **k):
        raise RunnerError("codex not installed")

    assert shortid.suggest({}, "支付超时", tmp_path, run=boom) == \
        {"id": None, "title": None}


def test_shortid_parses_the_two_line_answer():
    from canopy import shortid
    got = shortid.parse("id: figma-free-design\ntitle: 设计师不用 Figma 出码")
    assert got == {"id": "figma-free-design", "title": "设计师不用 Figma 出码"}


def test_shortid_still_takes_a_bare_id():
    from canopy import shortid
    assert shortid.parse("figma-free-design")["id"] == "figma-free-design"


def test_shorten_cuts_at_punctuation_and_drops_the_opener():
    from canopy import shortid
    raw = ("问题: AI Agent 生成 prototype 对于设计师来说很实用,可以指出 "
           "html/react components,但是在脑暴的情况下…")
    short = shortid.shorten(raw)
    assert not short.startswith("问题")
    assert shortid._width(short) <= 26          # limit plus the ellipsis
    assert not short.rstrip("…").endswith("compone")   # never mid-word


def test_track_uses_the_suggested_id_and_title(ctx, slack):
    from canopy import ops
    channel, ts = "C0NEW", "1699000900.000100"
    slack.add(channel, ts, ts, "U1",
              "问题: AI Agent 生成 prototype 对设计师很实用,但是精度有损失……")
    link = "https://example.slack.com/archives/%s/p1699000900000100" % channel
    result = ops.track(ctx, link, namer=lambda *a, **k:
                       "id: figma-free-design\ntitle: 设计师不用 Figma 出码")
    assert result["proj_id"] == "figma-free-design"
    assert result["title"] == "设计师不用 Figma 出码"


def test_track_falls_back_to_a_shortened_opening_line(ctx, slack):
    """No model answer: still not 60 raw characters cut mid-word."""
    from canopy import ops, shortid
    channel, ts = "C0NEW", "1699000900.000100"
    slack.add(channel, ts, ts, "U1",
              "问题: AI Agent 生成 prototype 对于设计师来说很实用,可以指出 html/react components")
    link = "https://example.slack.com/archives/%s/p1699000900000100" % channel
    result = ops.track(ctx, link, namer=lambda *a, **k: "")
    assert shortid._width(result["title"]) <= 26
    assert not result["title"].startswith("问题")


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


def test_the_runner_gets_its_own_directory_on_PATH():
    """`codex` is a node script whose shebang runs `env node`. Resolving codex to
    an absolute path was not enough: cron's PATH has no node, so every worker
    died with `exit 127: env: node: No such file or directory` — while the same
    command worked fine from a shell."""
    env = runner.runner_env(["/opt/mise/installs/node/22/bin/codex", "exec"],
                            base={"PATH": "/usr/bin:/bin"})
    assert env["PATH"].startswith("/opt/mise/installs/node/22/bin:")


def test_a_relative_runner_leaves_PATH_alone():
    env = runner.runner_env(["codex"], base={"PATH": "/usr/bin"})
    assert env["PATH"] == "/usr/bin"


def test_probe_reports_a_runner_that_exists_but_will_not_start():
    """`command -v` only proves a file exists — this is the failure it misses."""
    from canopy import effects as effects_mod

    rec = effects_mod.Recording(
        run=lambda argv, stdin="": (127, "", "env: node: No such file or directory"))
    with pytest.raises(RunnerError) as exc:
        runner.probe({"runner_path": "/abs/codex"}, effects=rec)
    assert "will not start" in str(exc.value) and "node" in str(exc.value)


def test_probe_passes_the_fixed_environment():
    from canopy import effects as effects_mod
    rec = effects_mod.Recording(run=lambda argv, stdin="": (0, "codex 1.0", ""))
    assert runner.probe({"runner_path": "/opt/bin/codex"}, effects=rec) == "codex 1.0"
    assert rec.calls[0]["env"]["PATH"].startswith("/opt/bin:")
