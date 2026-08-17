"""Spawning a worker: the only place that knows which CLI does the thinking.

Workers run with no sandbox and no approval prompts. A worker woken by cron has
no TTY, so an approval prompt is a hang; a sandbox that blocks network or writes
outside the node dir leaves a woken worker unable to post to Slack or advance
its own cursor, and it fails silently. That is a real trade: the model gets the
same reach over this machine as the person who installed Canopy. Put your own
`cmd` wrapper here (container, separate account, another box) if you need a
boundary — that is what the escape hatch is for.
"""

import shutil
import subprocess
from pathlib import Path

from .errors import RunnerError


def _run(argv, prompt, cwd, timeout=None, effects=None):
    """A hung worker holds its node's lock, so every run is time-boxed."""
    from . import effects as effects_mod
    try:
        return (effects or effects_mod.DEFAULT).run(
            argv, stdin=prompt, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        raise RunnerError("runner did not finish within %ss: %s"
                          % (timeout, " ".join(argv)))


def resolve_path(runner, which=None):
    """cron has a minimal PATH and no shell profile, so a mise/nvm/asdf install
    is invisible to it. Resolve once, store the absolute path in config.json."""
    which = which or shutil.which
    name = runner if isinstance(runner, str) else (runner or {}).get("cmd", [None])[0]
    if not name:
        raise RunnerError("runner has no command")
    found = which(name)
    if not found:
        raise RunnerError(
            "cannot find %r on PATH; cron will not find it either. Install it, "
            "or set runner_path in config.json to an absolute path." % (name,)
        )
    return found


def build_argv(cfg, node_dir, out_file=None):
    """-> argv. The prompt always arrives on stdin, never in argv."""
    runner = cfg.get("runner") or "codex"
    node_dir = str(node_dir)

    if isinstance(runner, dict):
        cmd = list(runner.get("cmd") or [])
        if not cmd:
            raise RunnerError("runner.cmd is empty")
        if cfg.get("runner_path"):
            cmd[0] = cfg["runner_path"]
        return cmd

    binary = cfg.get("runner_path") or runner

    if runner == "codex":
        argv = [binary, "exec", "--skip-git-repo-check", "--ephemeral",
                "--dangerously-bypass-approvals-and-sandbox",
                "-C", node_dir]
        if out_file:
            argv += ["-o", str(out_file)]
        argv.append("-")
        return argv

    if runner == "claude":
        return [binary, "-p", "--output-format", "text",
                "--dangerously-skip-permissions"]

    raise RunnerError(
        "unknown runner %r; use \"codex\", \"claude\", or {\"cmd\": [...]}" % (runner,)
    )


def run(cfg, prompt, node_dir, out_file=None, exec_fn=None, timeout=None,
        effects=None):
    """-> the worker's last message.

    Prefers codex's `-o` file when it exists, since stdout also carries the
    run's own chatter; falls back to stdout for runners without that flag.
    """
    argv = build_argv(cfg, node_dir, out_file=out_file)
    # Only trust the file if this argv actually asked for it, and never trust
    # what was in it before. `claude` and a custom `cmd` take no `-o`, so a
    # leftover file from a codex-era run was being read back as this run's
    # answer, every time.
    wants_file = out_file and "-o" in argv
    if wants_file and Path(out_file).exists():
        Path(out_file).unlink()
    if timeout is None:
        timeout = cfg.get("runner_timeout_seconds") or None
    if exec_fn:
        code, out, err = exec_fn(argv, prompt, node_dir)
    else:
        code, out, err = _run(argv, prompt, node_dir, timeout=timeout,
                              effects=effects)
    if code != 0:
        raise RunnerError("runner exited %s: %s" % (code, (err or out).strip()[:500]))
    if wants_file and Path(out_file).exists():
        text = Path(out_file).read_text(encoding="utf-8")
        if text.strip():
            return text.strip()
    return out.strip()
