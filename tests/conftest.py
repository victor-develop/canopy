import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from canopy import config as config_mod  # noqa: E402
from canopy import ops, paths, store  # noqa: E402


class FakeSlack(object):
    """Records every call so tests can assert on what would hit the channel."""

    def __init__(self, thread_messages=None):
        self.posted = []
        self.updates = []
        self.reactions = []
        self.threads = dict(thread_messages or {})
        self._counter = 1700000000

    # -- reads
    def _key(self, channel, thread_ts):
        return "%s-%s" % (channel, thread_ts)

    def thread(self, channel, thread_ts, oldest=None, limit=200):
        msgs = list(self.threads.get(self._key(channel, thread_ts), []))
        if oldest:
            msgs = [m for m in msgs if float(m["ts"]) >= float(oldest)]
        return msgs[:limit]

    def new_messages(self, channel, thread_ts, after_ts, limit=200):
        return [m for m in self.thread(channel, thread_ts, oldest=after_ts,
                                       limit=limit)
                if float(m["ts"]) > float(after_ts)]

    def latest_ts(self, channel, thread_ts, after_ts=None):
        msgs = self.thread(channel, thread_ts, oldest=after_ts)
        return msgs[-1]["ts"] if msgs else (after_ts or thread_ts)

    # -- writes
    def _next_ts(self):
        self._counter += 7
        return "%d.000100" % self._counter

    def post(self, channel, text, thread_ts=None):
        ts = self._next_ts()
        self.posted.append({"channel": channel, "text": text,
                            "thread_ts": thread_ts, "ts": ts})
        return ts

    def update(self, channel, ts, text):
        self.updates.append({"channel": channel, "ts": ts, "text": text})
        return ts

    def react(self, channel, ts, emoji):
        self.reactions.append({"channel": channel, "ts": ts, "emoji": emoji})
        return True

    # -- helpers for tests
    def add(self, channel, thread_ts, ts, user, text):
        self.threads.setdefault(self._key(channel, thread_ts), []).append(
            {"ts": ts, "user": user, "text": text})

    def text_of(self, ts):
        for entry in self.updates[::-1]:
            if entry["ts"] == ts:
                return entry["text"]
        for entry in self.posted:
            if entry["ts"] == ts:
                return entry["text"]
        return None


@pytest.fixture(autouse=True)
def no_real_runner(monkeypatch):
    """No test may spawn a real codex/claude: it would hang or cost money."""
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to spawn the real runner")
    monkeypatch.setattr("canopy.runner._run", refuse)


@pytest.fixture
def repo():
    return REPO


@pytest.fixture
def dh(tmp_path, monkeypatch):
    home = tmp_path / "data"
    home.mkdir()
    monkeypatch.setenv("CANOPY_DATA_HOME", str(home))
    return home


@pytest.fixture
def slack():
    return FakeSlack()


@pytest.fixture
def ctx(dh, slack, repo):
    cfg = config_mod.load(dh)
    cfg["slack_workspace_url"] = "https://example.slack.com"
    config_mod.save(dh, cfg)
    return ops.Ctx(dh, cfg=cfg, slack=slack, root=repo, now=lambda: 1700000000.0)


@pytest.fixture
def tracked(ctx, slack):
    """A tracked root with one message in the raw thread."""
    channel, ts = "C0PAY", "1699000001.000100"
    slack.add(channel, ts, ts, "U1", "支付超时,大家看下")
    link = "https://example.slack.com/archives/%s/p1699000001000100" % channel
    result = ops.track(ctx, link, owner="A君",
                       namer=lambda *a, **k: "pay-timeout")
    return result
