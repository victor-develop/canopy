"""Registering the tick with cron, and taking it back out.

One entry for the whole install, not one per tree: the tick already walks every
tracked project, and N crontab lines would mean N wakeups doing the same walk.
"""

import shlex

MARKER = "# canopy"


def _run(argv, stdin=""):
    from . import effects as effects_mod
    return effects_mod.DEFAULT.run(argv, stdin=stdin)


def escape(command):
    """crontab reads an unescaped `%` as a newline: everything after the first
    one becomes the job's stdin instead of part of the command. Harmless until a
    data-home path or a quoted command carries one."""
    return command.replace("%", "\\%")


def line(tick_cmd, interval_minutes, data_home=None, login_shell=None):
    """One crontab line, run the way the person who installed canopy runs things.

    cron starts no shell and reads no profile, so the environment a runner's
    auth lives in simply does not exist there. `codex` reads its provider key
    from an environment variable (`env_key` in ~/.codex/config.toml) exported by
    a shell profile: from a terminal it worked, from cron every worker died two
    seconds in. Absolute paths were not the missing piece — the profile was.

    So the tick runs inside a login shell, which sources that profile and hands
    the tick the same environment a person gets. Nothing is copied into the
    crontab or into config.json, so no credential ends up at rest anywhere new.
    """
    every = "*/%d * * * *" % int(interval_minutes)
    env = "CANOPY_DATA_HOME=%s " % data_home if data_home else ""
    command = "%s%s" % (env, tick_cmd)
    if login_shell:
        command = "%s -lc %s" % (login_shell, shlex.quote(command))
    return "%s %s %s" % (every, escape(command), MARKER)


def read_crontab(run=None):
    code, out, err = (run or _run)(["crontab", "-l"])
    if code != 0:
        # An empty crontab exits non-zero on macOS; that is not an error here.
        return ""
    return out


def install(tick_cmd, interval_minutes, data_home=None, login_shell=None,
            run=None):
    run = run or _run
    current = read_crontab(run=run)
    kept = [l for l in current.splitlines() if MARKER not in l]
    kept.append(line(tick_cmd, interval_minutes, data_home=data_home,
                     login_shell=login_shell))
    payload = "\n".join([l for l in kept if l.strip()]) + "\n"
    code, out, err = run(["crontab", "-"], payload)
    if code != 0:
        raise RuntimeError("crontab install failed: %s" % (err.strip() or out.strip(),))
    return payload


def uninstall(run=None):
    run = run or _run
    current = read_crontab(run=run)
    kept = [l for l in current.splitlines() if MARKER not in l]
    payload = "\n".join([l for l in kept if l.strip()])
    payload = payload + "\n" if payload else ""
    code, out, err = run(["crontab", "-"], payload)
    if code != 0:
        raise RuntimeError("crontab uninstall failed: %s" % (err.strip() or out.strip(),))
    return payload


def installed(run=None):
    return any(MARKER in l for l in read_crontab(run=run).splitlines())
