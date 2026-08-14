"""The checkpoint feed: one message per segment, appended to in place.

A Slack message has a length cap, so when the active segment fills it is sealed
(never edited again) and a new message becomes the next segment. That is also
what keeps `recalibrate` cheap: the per-tick path only ever rewrites the last
segment, so it reads the increment and can't blow a context window. Sealed
segments are immutable history.
"""

from pathlib import Path

from . import config as config_mod
from . import store, templates

SEGMENTS_FILE = "feed.json"

KIND_TEMPLATE = {
    "root": "feed-root.md",
    "fork": "feed-fork.md",
    "segment": "feed-segment.md",
}


def segments_path(node_dir):
    return Path(node_dir) / SEGMENTS_FILE


def load_segments(node_dir):
    return store.read_json(segments_path(node_dir), default=[]) or []


def save_segments(node_dir, segments):
    return store.write_json(segments_path(node_dir), segments)


class Feed(object):
    def __init__(self, dh, cfg, proj_id, state, node_dir, slack, locale=None,
                 root=None):
        self.dh = dh
        self.cfg = cfg
        self.proj_id = proj_id
        self.state = state
        self.node_dir = Path(node_dir)
        self.slack = slack
        self.locale = locale or cfg.get("locale", "zh")
        self.root = root
        self.max_chars = int(cfg.get("feed_segment_max_chars", 3500))

    # -- rendering --------------------------------------------------------

    def _render(self, name, values):
        return templates.render_named(name, values, self.dh, self.locale,
                                      proj_id=self.proj_id, root=self.root)

    def render_segment(self, segment, extra_entry=None):
        entries = list(segment.get("entries") or [])
        if extra_entry:
            entries.append(extra_entry)
        values = dict(segment.get("vars") or {})
        values["entries"] = "\n".join(entries)
        return self._render(KIND_TEMPLATE[segment["kind"]], values)

    def render_entry(self, summary, author="", date="", icon="•",
                     raw_permalink=None):
        return self._render("feed-entry.md", {
            "icon": icon,
            "summary": summary,
            "author": author,
            "date": date,
            "raw_permalink": raw_permalink or self.state.get("raw_permalink"),
        })

    # -- lifecycle --------------------------------------------------------

    def open(self, kind, values):
        """Post the first segment of this node's feed. -> ts."""
        segment = {"index": 1, "kind": kind, "vars": dict(values), "entries": [],
                   "sealed": False, "ts": None}
        text = self.render_segment(segment)
        ts = self.slack.post(self.state["channel"], text)
        segment["ts"] = ts
        save_segments(self.node_dir, [segment])
        self.state.setdefault("feed_ts", []).append(ts)
        return ts

    def append(self, summary, author="", date="", icon="•", raw_permalink=None):
        """Add one checkpoint. -> {"ts", "segment", "sealed"}.

        Overflows into a fresh segment instead of truncating: an entry that
        would push the active message past the cap opens segment N+1 and stamps
        a pointer onto the one it just sealed.
        """
        segments = load_segments(self.node_dir)
        if not segments:
            raise ValueError("feed has no segments yet; call open() first")
        active = segments[-1]
        entry = self.render_entry(summary, author=author, date=date, icon=icon,
                                  raw_permalink=raw_permalink)
        candidate = self.render_segment(active, extra_entry=entry)

        if len(candidate) <= self.max_chars or not active.get("entries"):
            active.setdefault("entries", []).append(entry)
            text = self.render_segment(active)
            self.slack.update(self.state["channel"], active["ts"], text)
            save_segments(self.node_dir, segments)
            return {"ts": active["ts"], "segment": active["index"], "sealed": False}

        return self._overflow(segments, active, entry)

    def _overflow(self, segments, active, entry):
        index = active["index"] + 1
        prev_permalink = config_mod.permalink(self.cfg, self.state["channel"],
                                              active["ts"])
        new_segment = {
            "index": index,
            "kind": "segment",
            "vars": {
                "title": self.state.get("title"),
                "alias": (active.get("vars") or {}).get("alias"),
                "segment_index": index,
                "prev_segment_index": active["index"],
                "prev_segment_permalink": prev_permalink,
                "tree_permalink": self.state.get("tree_permalink"),
            },
            "entries": [entry],
            "sealed": False,
            "ts": None,
        }
        text = self.render_segment(new_segment)
        ts = self.slack.post(self.state["channel"], text)
        new_segment["ts"] = ts

        footer = self._render("feed-sealed-footer.md", {
            "segment_index": active["index"],
            "next_segment_index": index,
            "next_segment_permalink": config_mod.permalink(
                self.cfg, self.state["channel"], ts),
        })
        sealed_text = self.render_segment(active) + "\n\n" + footer
        self.slack.update(self.state["channel"], active["ts"], sealed_text)
        active["sealed"] = True

        segments.append(new_segment)
        save_segments(self.node_dir, segments)
        self.state.setdefault("feed_ts", []).append(ts)
        return {"ts": ts, "segment": index, "sealed": True}

    def rebuild(self, entries):
        """`recalibrate`: rewrite every segment in one pass.

        Sealed segments are rewritten here and only here — that is the whole
        point of the heavy path.
        """
        segments = load_segments(self.node_dir)
        if not segments:
            raise ValueError("feed has no segments yet")
        for segment in segments:
            segment["entries"] = []
        cursor = 0
        for entry in entries:
            segment = segments[cursor]
            candidate = self.render_segment(segment, extra_entry=entry)
            if len(candidate) > self.max_chars and segment["entries"]:
                cursor += 1
                if cursor >= len(segments):
                    # More content than segments: keep the tail in the last one
                    # rather than dropping checkpoints on the floor.
                    cursor = len(segments) - 1
                segment = segments[cursor]
            segment["entries"].append(entry)

        for i, segment in enumerate(segments):
            text = self.render_segment(segment)
            if i < len(segments) - 1:
                nxt = segments[i + 1]
                text += "\n\n" + self._render("feed-sealed-footer.md", {
                    "segment_index": segment["index"],
                    "next_segment_index": nxt["index"],
                    "next_segment_permalink": config_mod.permalink(
                        self.cfg, self.state["channel"], nxt["ts"]),
                })
                segment["sealed"] = True
            self.slack.update(self.state["channel"], segment["ts"], text)
        save_segments(self.node_dir, segments)
        return segments
