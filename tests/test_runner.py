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
