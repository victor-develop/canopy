"""Registering the tick with cron, and taking it back out.

One entry for the whole install, not one per tree: the tick already walks every
tracked project, and N crontab lines would mean N wakeups doing the same walk.
"""

import subprocess

MARKER = "# canopy"


def _run(argv, stdin=""):
    proc = subprocess.run(argv, input=stdin.encode("utf-8"),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return (proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


def line(tick_cmd, interval_minutes, data_home=None):
    every = "*/%d * * * *" % int(interval_minutes)
    env = "CANOPY_DATA_HOME=%s " % data_home if data_home else ""
    return "%s %s%s %s" % (every, env, tick_cmd, MARKER)


def read_crontab(run=None):
    code, out, err = (run or _run)(["crontab", "-l"])
    if code != 0:
        # An empty crontab exits non-zero on macOS; that is not an error here.
        return ""
    return out


def install(tick_cmd, interval_minutes, data_home=None, run=None):
    run = run or _run
    current = read_crontab(run=run)
    kept = [l for l in current.splitlines() if MARKER not in l]
    kept.append(line(tick_cmd, interval_minutes, data_home=data_home))
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
