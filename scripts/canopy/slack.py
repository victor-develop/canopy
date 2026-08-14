"""The only place Canopy talks to Slack.

Everything goes through `slackcli` as a subprocess: it already holds the
workspace credential, so Canopy never handles a token itself. The command
runner is injectable, which is also how the tests drive every path here without
a network.

Posting always happens through this module, never with the user's own identity
implied — callers render an identity-prefixed template first (`[canopy]: …`).
"""

import json
import re
import subprocess

from .errors import SlackError

TS_RE = re.compile(r"\b(\d{10}\.\d{4,6})\b")


def _run(args):
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), \
        proc.stderr.decode("utf-8", "replace")


class Slack(object):
    def __init__(self, cli="slackcli", run=None, workspace=None):
        self.cli = cli
        self.run = run or _run
        self.workspace = workspace

    # -- plumbing ---------------------------------------------------------

    def _call(self, *args):
        argv = [self.cli] + list(args)
        if self.workspace:
            argv += ["--workspace", self.workspace]
        code, out, err = self.run(argv)
        if code != 0:
            raise SlackError("slackcli failed (%s): %s" % (" ".join(argv), err.strip() or out.strip()))
        return out

    @staticmethod
    def _messages(payload):
        try:
            data = json.loads(payload)
        except ValueError:
            raise SlackError("slackcli did not return JSON: %s" % (payload[:200],))
        if isinstance(data, dict):
            data = data.get("messages") or data.get("data") or []
        out = []
        for item in data:
            if not isinstance(item, dict):
                continue
            ts = item.get("ts") or item.get("timestamp")
            if not ts:
                continue
            out.append({
                "ts": str(ts),
                "user": item.get("user") or item.get("username") or item.get("user_name") or "",
                "text": item.get("text") or "",
            })
        out.sort(key=lambda m: float(m["ts"]))
        return out

    @staticmethod
    def _ts_from(payload):
        try:
            data = json.loads(payload)
        except ValueError:
            data = None
        if isinstance(data, dict):
            for key in ("ts", "timestamp"):
                if data.get(key):
                    return str(data[key])
            message = data.get("message")
            if isinstance(message, dict) and message.get("ts"):
                return str(message["ts"])
        match = TS_RE.search(payload or "")
        if match:
            return match.group(1)
        raise SlackError("could not read a message ts back from slackcli: %r"
                         % ((payload or "")[:200],))

    # -- reads ------------------------------------------------------------

    def thread(self, channel, thread_ts, oldest=None, limit=200):
        args = ["conversations", "read", channel, "--thread-ts", thread_ts,
                "--json", "--limit", str(limit)]
        if oldest:
            args += ["--oldest", str(oldest)]
        return self._messages(self._call(*args))

    def new_messages(self, channel, thread_ts, after_ts, limit=200):
        """Strictly newer than `after_ts` — the cursor itself is already seen."""
        msgs = self.thread(channel, thread_ts, oldest=after_ts, limit=limit)
        return [m for m in msgs if float(m["ts"]) > float(after_ts)]

    def latest_ts(self, channel, thread_ts, after_ts=None):
        msgs = self.thread(channel, thread_ts, oldest=after_ts, limit=200)
        return msgs[-1]["ts"] if msgs else (after_ts or thread_ts)

    # -- writes -----------------------------------------------------------

    def post(self, channel, text, thread_ts=None):
        args = ["messages", "send", "--recipient-id", channel, "--message", text]
        if thread_ts:
            args += ["--thread-ts", thread_ts]
        return self._ts_from(self._call(*args))

    def update(self, channel, ts, text):
        self._call("messages", "edit", "--channel-id", channel,
                   "--timestamp", ts, "--message", text)
        return ts

    def react(self, channel, ts, emoji):
        self._call("messages", "react", "--channel-id", channel,
                   "--timestamp", ts, "--emoji", emoji)
        return True


def parse_thread_link(link):
    """-> (channel, thread_ts) from a Slack archive URL.

    Accepts the two forms people actually paste: a top-level message link, and
    a reply link carrying `thread_ts` in the query string.
    """
    match = re.search(r"/archives/([A-Z0-9]+)/p(\d{10})(\d{6})", link or "")
    if not match:
        raise SlackError("not a Slack thread link: %r" % (link,))
    channel, secs, micros = match.groups()
    ts = "%s.%s" % (secs, micros)
    thread = re.search(r"[?&]thread_ts=([0-9.]+)", link or "")
    if thread:
        ts = thread.group(1)
    return channel, ts
