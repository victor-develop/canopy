"""Spawning a worker: the only place that knows which CLI does the thinking.

Workers run with no sandbox and no approval prompts. A worker woken by cron has
no TTY, so an approval prompt is a hang; a sandbox that blocks network or writes
outside the node dir leaves a woken worker unable to post to Slack or advance
its own cursor, and it fails silently. That is a real trade: the model gets the
same reach over this machine as the person who installed Canopy. Put your own
`cmd` wrapper here (container, separate account, another box) if you need a
boundary — that is what the escape hatch is for.
"""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from .errors import RunnerError


def runner_env(argv, base=None):
    """The environment a runner needs, not the one cron happens to give us.

    Resolving `codex` to an absolute path is not enough: it is a node script
    whose shebang is `#!/usr/bin/env node`, and cron's PATH has no node. Every
    worker died with `exit 127: env: node: No such file or directory` — from a
    terminal everything worked, from cron nothing ever did.

    A version manager keeps the interpreter next to the tool, so putting the
    binary's own directory first is exactly the missing piece.
    """
    env = dict(os.environ if base is None else base)
    binary = Path(argv[0])
    if binary.is_absolute():
        # No existence check: prepending a directory that isn't there costs
        # nothing, and skipping the fix when it *is* there is the bug.
        env["PATH"] = "%s:%s" % (binary.parent, env.get("PATH", ""))
    return env


def failure_detail(out, err, limit=1500):
    """What a failed runner said, in the part that says why.

    Keeping the *head* of the output loses the reason. `codex exec` prints its
    banner and then echoes the whole prompt — a worker prompt is thousands of
    characters — so the first 500 characters never reach the error, and three
    consecutive failures logged the identical string, cut mid-sentence in the
    prompt. The reason is the last thing printed, so keep the tail.

    Both streams, not `err or out`: codex puts its transcript on stderr and the
    final message on stdout, and which one carries the failure depends on how
    far it got. Picking one meant the other was unrecoverable.
    """
    parts = []
    for label, stream in (("stderr", err), ("stdout", out)):
        text = (stream or "").strip()
        if not text:
            continue
        if len(text) > limit:
            text = "…" + text[-limit:]
        parts.append("%s: %s" % (label, text))
    return "\n".join(parts) or "(no output on either stream)"


def _run(argv, prompt, cwd, timeout=None, effects=None):
    """A hung worker holds its node's lock, so every run is time-boxed."""
    from . import effects as effects_mod
    try:
        return (effects or effects_mod.DEFAULT).run(
            argv, stdin=prompt, timeout=timeout, cwd=cwd,
            env=runner_env(argv))
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


PROBE_PROMPT = "Reply with exactly: ok"


def cron_env(base=None):
    """Near enough to the environment cron hands a job: no profile, bare PATH.

    `track` runs from a terminal, where everything already works — which is
    exactly why probing with the caller's own environment proves nothing about
    the environment the tick will get.
    """
    src = dict(os.environ if base is None else base)
    return {"HOME": src.get("HOME", ""), "PATH": "/usr/bin:/bin",
            "LOGNAME": src.get("LOGNAME", ""), "USER": src.get("USER", "")}


def cron_style_argv(argv, login_shell=None):
    """The same argv, wrapped the way the crontab line wraps it."""
    if not login_shell:
        return list(argv)
    return [login_shell, "-lc", " ".join(shlex.quote(str(a)) for a in argv)]


def probe(cfg, cwd=None, effects=None):
    """Run the runner once, the way cron will. -> what it answered.

    Two earlier versions of this check each proved one layer too little.
    `command -v` proved a file existed, and workers still died with
    `exit 127: env: node: No such file or directory`. `--version` proved the
    binary started, and workers still died two seconds in with
    `ERROR: Missing environment variable: AM_API_KEY` — because `--version`
    needs no credentials and `track`'s own shell had the variable anyway.

    So this one asks the runner for a real (tiny) answer, in a stripped
    environment, through the same login shell the crontab line uses. It costs a
    few thousand tokens once per tree, and it turns "every tick fails silently
    for a week" into "track refuses, and says why".
    """
    from . import effects as effects_mod
    binary = cfg.get("runner_path") or cfg.get("runner") or "codex"
    argv = cron_style_argv(build_argv(cfg, cwd or "."),
                           cfg.get("cron_login_shell"))
    try:
        code, out, err = (effects or effects_mod.DEFAULT).run(
            argv, stdin=PROBE_PROMPT, timeout=cfg.get("shortid_timeout_seconds") or 90,
            cwd=str(cwd) if cwd else None, env=runner_env(argv, base=cron_env()))
    except OSError as exc:
        raise RunnerError("cannot start %s: %s" % (binary, exc))
    if code != 0:
        raise RunnerError(
            "%s will not do a job in cron's environment (exit %s): %s\n"
            "A missing variable means the profile: `track` runs from your "
            "terminal, cron runs no profile — see cron_login_shell in "
            "config.json. Anything from the provider (429, a timeout) is "
            "probably transient; run `track` again."
            % (binary, code, failure_detail(out, err, limit=400)))
    return (out or "").strip()


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
        raise RunnerError("runner exited %s: %s"
                          % (code, failure_detail(out, err)))
    if wants_file and Path(out_file).exists():
        text = Path(out_file).read_text(encoding="utf-8")
        if text.strip():
            return text.strip()
    return out.strip()
