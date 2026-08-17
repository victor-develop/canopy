import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from canopy import config as config_mod  # noqa: E402
from canopy import effects as effects_mod  # noqa: E402
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
        """Monotonic past everything already in the channel, like Slack.

        A counter that could hand out a ts *older* than a message already in the
        thread made "Canopy reads its own post back" untestable: the reply
        landed below the cursor and looked like history.
        """
        newest = 0
        for msgs in self.threads.values():
            for m in msgs:
                newest = max(newest, int(float(m["ts"])))
        self._counter = max(self._counter, newest) + 7
        return "%d.000100" % self._counter

    def post(self, channel, text, thread_ts=None):
        """Posting also makes the message readable — like the real Slack.

        The old fake kept `posted` and `threads` as separate worlds, so nothing
        Canopy said could ever be read back. That hid an entire class of bug:
        every message Canopy puts in a watched thread is something the next tick
        will read, and whether it recognises its own writing was untestable.
        Two real defects lived in that blind spot.
        """
        ts = self._next_ts()
        self.posted.append({"channel": channel, "text": text,
                            "thread_ts": thread_ts, "ts": ts})
        root = thread_ts or ts
        self.threads.setdefault(self._key(channel, root), []).append(
            {"ts": ts, "user": "UCANOPY", "text": text})
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


@pytest.fixture
def fake_crontab():
    """An in-memory crontab. The real one is off limits: a test once installed
    a job pointing at a pytest tmp dir and left it there."""
    state = {"text": ""}

    def run(argv, stdin=""):
        if argv[:2] == ["crontab", "-l"]:
            return (0, state["text"], "") if state["text"] else (1, "", "")
        if argv[:2] == ["crontab", "-"]:
            state["text"] = stdin
            return 0, "", ""
        raise AssertionError("unexpected command in a test: %r" % (argv,))

    state["run"] = run
    return state


@pytest.fixture(autouse=True)
def no_machine_effects(monkeypatch, fake_crontab):
    """One door for every touch of the machine, and it is shut.

    Replaces what used to be three separate symbol patches (runner, crontab,
    spawn). Those only guarded the holes somebody had already fallen into; a
    Recording() refuses anything it was not explicitly taught, so the next
    effect somebody adds fails in tests instead of on a laptop.
    """
    recording = effects_mod.Recording(run=fake_crontab["run"])
    monkeypatch.setattr(effects_mod, "DEFAULT", recording)
    return recording


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
def ctx(dh, slack, repo, no_machine_effects):
    cfg = config_mod.load(dh)
    cfg["slack_workspace_url"] = "https://example.slack.com"
    config_mod.save(dh, cfg)
    return ops.Ctx(dh, cfg=cfg, slack=slack, root=repo, now=lambda: 1700000000.0,
                   effects=no_machine_effects)


@pytest.fixture
def tracked(ctx, slack):
    """A tracked root with one message in the raw thread."""
    channel, ts = "C0PAY", "1699000001.000100"
    slack.add(channel, ts, ts, "U1", "支付超时,大家看下")
    link = "https://example.slack.com/archives/%s/p1699000001000100" % channel
    result = ops.track(ctx, link, owner="A君",
                       namer=lambda *a, **k: "pay-timeout")
    return result
