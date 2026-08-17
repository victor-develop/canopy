"""Every touch of the machine outside this data home, in one object.

Two incidents came from the same shape of bug: side effects reachable from a
command handler with no seam. The test suite installed a real cron entry into
the developer's crontab and left it there; a later run scattered 59 detached
HTTP servers that outlived it. Both were "fixed" by monkeypatching the symbol
that happened to be involved — which only guards the holes somebody already
fell into.

So the effects live here instead. Anything that spawns a process, edits the
crontab, opens a browser or binds a port goes through this object, tests get
`Recording()`, and a new effect added tomorrow has to come through the same
door — or it has no way to reach the machine at all.

Not covered on purpose: reads and writes inside `$CANOPY_DATA_HOME`. Those are
the program's own state, tests point it at a tmp dir, and routing them through
an effects object would obscure far more than it protects.
"""

import subprocess


class Effects(object):
    """The real thing."""

    name = "real"

    def run(self, argv, stdin=None, timeout=None, cwd=None):
        """A subprocess that runs to completion. -> (code, stdout, stderr)."""
        proc = subprocess.run(
            argv,
            input=stdin.encode("utf-8") if stdin is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(cwd) if cwd else None, timeout=timeout,
            start_new_session=True)
        return (proc.returncode,
                proc.stdout.decode("utf-8", "replace"),
                proc.stderr.decode("utf-8", "replace"))

    def spawn(self, argv):
        """A process that outlives this one. -> pid."""
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL,
                                start_new_session=True)
        return proc.pid

    def open_url(self, url):
        for argv in (["open", str(url)], ["xdg-open", str(url)]):
            try:
                subprocess.run(argv, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=True)
                return True
            except (OSError, subprocess.CalledProcessError):
                continue
        return False


class Recording(Effects):
    """For tests: records instead of doing, and refuses to spawn.

    A test that reaches a spawning path fails loudly and says which argv it was
    about to run, rather than leaving a process behind for someone to find with
    `ps` two hours later.
    """

    name = "recording"

    def __init__(self, run=None):
        self.calls = []
        self.opened = []
        self._run = run

    def run(self, argv, stdin=None, timeout=None, cwd=None):
        self.calls.append({"argv": list(argv), "stdin": stdin, "cwd": cwd})
        if self._run is None:
            raise AssertionError(
                "a test ran a subprocess with no stub: %r" % (list(argv),))
        return self._run(list(argv), stdin or "")

    def spawn(self, argv):
        raise AssertionError("a test tried to spawn a process: %r" % (list(argv),))

    def open_url(self, url):
        self.opened.append(str(url))
        return True


DEFAULT = Effects()
