"""The ops page, served locally.

This is the one long-lived process in the design, so it is kept as harmless as
possible: it **only reads disk and renders**. It never talks to Slack, never
starts a runner, never writes state. Kill it and nothing is lost — the cron tick
keeps working, and the next `canopy serve` picks up wherever the disk is.

Bound to loopback on purpose. The snapshot carries channel names, thread titles
and absolute paths from this machine; that is local operational data, not
something to expose on a LAN.
"""

import json
import os
import signal
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import opsview, store

STATE = "serve.json"
DEFAULT_PORT = 8787
HOST = "127.0.0.1"
# A viewer nobody is looking at is just a process leak with a port. Exit rather
# than outlive the person who opened it.
IDLE_TIMEOUT = 1800


def state_path(dh):
    from pathlib import Path
    return Path(dh) / STATE


def _alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def status(dh):
    """-> {"pid", "port", "url", "running"} for whatever was started last."""
    info = store.read_json(state_path(dh), default=None)
    if not info:
        return {"running": False}
    info = dict(info)
    info["running"] = _alive(info.get("pid"))
    info["url"] = "http://%s:%s/" % (HOST, info.get("port"))
    return info


def stop(dh):
    info = status(dh)
    if not info.get("running"):
        state_path(dh).unlink() if state_path(dh).exists() else None
        return False
    try:
        os.kill(int(info["pid"]), signal.SIGTERM)
    except OSError:
        pass
    if state_path(dh).exists():
        state_path(dh).unlink()
    return True


def free_port(preferred=DEFAULT_PORT):
    """Take the configured port if it is free, otherwise let the OS pick one."""
    for port in (preferred, 0):
        sock = socket.socket()
        try:
            sock.bind((HOST, port))
            chosen = sock.getsockname()[1]
            sock.close()
            return chosen
        except OSError:
            sock.close()
    raise OSError("no port available")


def make_handler(dh, cfg, root=None, seen=None):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, content_type):
            payload = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):  # noqa: N802 - http.server's interface
            if seen is not None:
                seen["at"] = time.time()
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                data = opsview.snapshot(dh, cfg)
                self._send(200, opsview.render(data, root=root), "text/html; charset=utf-8")
            elif path == "/api/snapshot":
                data = opsview.snapshot(dh, cfg)
                self._send(200, json.dumps(data, ensure_ascii=False),
                           "application/json; charset=utf-8")
            else:
                self._send(404, "not found", "text/plain; charset=utf-8")

        def log_message(self, *args):
            """Quiet: this runs in the background of someone's terminal."""

    return Handler


def make_server(dh, cfg, root=None, port=None, seen=None):
    port = free_port(port or int(cfg.get("serve_port", DEFAULT_PORT)))
    return HTTPServer((HOST, port), make_handler(dh, cfg, root=root, seen=seen))


def _idle_watchdog(httpd, seen, timeout, stop_after=None):
    """Shut the server down once nobody has asked it anything for `timeout`."""
    def loop():
        checks = 0
        while True:
            time.sleep(min(30, max(1, timeout / 10.0)))
            checks += 1
            if time.time() - seen["at"] > timeout:
                httpd.shutdown()
                return
            if stop_after is not None and checks >= stop_after:
                return
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


def serve(dh, cfg, root=None, port=None, record=True, idle_timeout=None):
    """Run until interrupted, or until nobody has looked for a while."""
    seen = {"at": time.time()}
    httpd = make_server(dh, cfg, root=root, port=port, seen=seen)
    timeout = idle_timeout if idle_timeout is not None else \
        int(cfg.get("serve_idle_timeout", IDLE_TIMEOUT))
    if timeout:
        _idle_watchdog(httpd, seen, timeout)
    if record:
        store.write_json(state_path(dh),
                         {"pid": os.getpid(), "port": httpd.server_port})
    try:
        httpd.serve_forever()
    finally:
        if record and state_path(dh).exists():
            state_path(dh).unlink()


def serve_in_thread(dh, cfg, root=None, port=None):
    """For tests: a real server on a real port, no subprocess."""
    httpd = make_server(dh, cfg, root=root, port=port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, "http://%s:%s" % (HOST, httpd.server_port)


def start_background(dh, cfg, root=None, port=None, spawn=None):
    """Start the viewer if it is not already up. -> status dict.

    Reuses a live one rather than starting a second: `track` calls this every
    time, and nobody wants a port-per-tracked-tree.
    """
    current = status(dh)
    if current.get("running") and _responds(current.get("port")):
        return current
    if current.get("running"):
        # Recorded pid is alive but nothing answers on the port: a stale record,
        # or someone else's process reusing the pid. Do not stack another one on
        # top without clearing it first.
        stop(dh)

    import subprocess
    import sys
    from pathlib import Path

    chosen = free_port(port or int(cfg.get("serve_port", DEFAULT_PORT)))
    argv = [sys.executable, str(Path(__file__).resolve().parents[1] / "canopy_main.py"),
            "serve", "--port", str(chosen), "--no-open"]
    spawn = spawn or _spawn
    pid = spawn(argv, dh)
    store.write_json(state_path(dh), {"pid": pid, "port": chosen})
    return {"pid": pid, "port": chosen, "running": True,
            "url": "http://%s:%s/" % (HOST, chosen)}


def _responds(port):
    if not port:
        return False
    sock = socket.socket()
    sock.settimeout(0.3)
    try:
        sock.connect((HOST, int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _spawn(argv, dh):
    import subprocess
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    return proc.pid
