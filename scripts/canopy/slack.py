"""The only place Canopy talks to Slack.

Two backends, same interface:

- **slackcli** (default) shells out to the CLI that already holds the workspace
  credential, so Canopy never handles a token. One catch, found the hard way:
  upstream slackcli (through v0.9.0) omits `parse=none` on `chat.update`, so Slack
  escapes the text and `<url|label>` is stored as `&lt;url|label&gt;` — a dead
  link. Since the feed is updated in place on every checkpoint, that breaks
  every link in it. With such a CLI, set `slack_cli_escapes_on_edit: true` and
  this backend degrades rich links to bare URLs: ugly, but clickable. With a
  patched CLI, set it false and keep the labels.
- **api** posts straight to the Slack Web API with a user token read from the
  environment variable named in `config.json` (`slack_token_env`). Canopy never
  reads a token from a file and never logs one. Rich links survive edits here.

Posting always happens through this module, never with the user's own identity
implied — callers render an identity-prefixed template first (`[canopy]: …`).
"""

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from .errors import SlackError

TS_RE = re.compile(r"\b(\d{10}\.\d{4,6})\b")
LINK_RE = re.compile(r"<(https?://[^|>\s]+)\|([^>]*)>")
API_BASE = "https://slack.com/api/"


def degrade_links(text):
    """`<url|label>` -> `label url`. For backends that escape angle brackets."""
    return LINK_RE.sub(lambda m: "%s %s" % (m.group(2).strip(), m.group(1)), text or "")


def _run(argv):
    from . import effects as effects_mod
    return effects_mod.DEFAULT.run(argv)


def _http(url, data, token):
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": "Bearer %s" % token,
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise SlackError("Slack API call failed: %s" % (exc,))


class Slack(object):
    def __init__(self, cli="slackcli", run=None, workspace=None, backend="slackcli",
                 token=None, http=None, escapes_on_edit=True):
        self.cli = cli
        self.run = run or _run
        self.workspace = workspace
        self.backend = backend
        self.token = token
        self.http = http or _http
        self.escapes_on_edit = escapes_on_edit

    @classmethod
    def from_config(cls, cfg, effects=None, **kwargs):
        backend = cfg.get("slack_backend", "slackcli")
        token = None
        if backend == "api":
            env_name = cfg.get("slack_token_env") or "CANOPY_SLACK_TOKEN"
            token = os.environ.get(env_name)
            if not token:
                raise SlackError(
                    "slack_backend is \"api\" but $%s is not set. Export the "
                    "token in the environment cron runs with, or switch "
                    "slack_backend back to \"slackcli\"." % (env_name,))
        # cron has no shell profile, so a PATH lookup for `slackcli` fails
        # there even though it works in a terminal. Use the resolved path.
        cli = cfg.get("slack_cli_path") or cfg.get("slack_cli") or "slackcli"
        if effects is not None and "run" not in kwargs:
            kwargs["run"] = lambda argv: effects.run(argv)
        return cls(cli=cli, workspace=cfg.get("slack_workspace"),
                   backend=backend, token=token,
                   escapes_on_edit=bool(cfg.get("slack_cli_escapes_on_edit", True)),
                   **kwargs)

    # -- plumbing ---------------------------------------------------------

    def _call(self, *args):
        argv = [self.cli] + list(args)
        if self.workspace:
            argv += ["--workspace", self.workspace]
        code, out, err = self.run(argv)
        if code != 0:
            raise SlackError("slackcli failed (%s): %s"
                             % (" ".join(argv), err.strip() or out.strip()))
        return out

    def _api(self, method, **params):
        payload = self.http(API_BASE + method,
                            dict((k, v) for k, v in params.items() if v is not None),
                            self.token)
        try:
            data = json.loads(payload)
        except ValueError:
            raise SlackError("Slack API returned non-JSON: %s" % (payload[:200],))
        if not data.get("ok"):
            raise SlackError("Slack API %s failed: %s"
                             % (method, data.get("error", "unknown")))
        return data

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

    def _text_for(self, text, editing=False):
        if self.backend == "slackcli" and editing and self.escapes_on_edit:
            return degrade_links(text)
        return text

    # -- reads ------------------------------------------------------------

    def thread(self, channel, thread_ts, oldest=None, limit=200):
        if self.backend == "api":
            data = self._api("conversations.replies", channel=channel, ts=thread_ts,
                             oldest=oldest, limit=limit)
            return self._messages(json.dumps(data.get("messages", [])))
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
        text = self._text_for(text)
        if self.backend == "api":
            data = self._api("chat.postMessage", channel=channel, text=text,
                             thread_ts=thread_ts)
            return str(data["ts"])
        args = ["messages", "send", "--recipient-id", channel, "--message", text]
        if thread_ts:
            args += ["--thread-ts", thread_ts]
        return self._ts_from(self._call(*args))

    def update(self, channel, ts, text):
        text = self._text_for(text, editing=True)
        if self.backend == "api":
            self._api("chat.update", channel=channel, ts=ts, text=text)
            return ts
        self._call("messages", "edit", "--channel-id", channel,
                   "--timestamp", ts, "--message", text)
        return ts

    def react(self, channel, ts, emoji):
        if self.backend == "api":
            self._api("reactions.add", channel=channel, timestamp=ts, name=emoji)
            return True
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
