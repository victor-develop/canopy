"""Every operation that changes a tree, in one place.

Both entry points land here: `/canopy …` from A君's terminal and `@agent …`
from a Slack thread. They must do the same thing — a fork typed in Slack and a
fork typed in the terminal cannot produce differently-shaped state — so the
command layers stay thin and this module holds the behaviour.
"""

import os
import time
from pathlib import Path

from . import config as config_mod
from . import effects as effects_mod
from . import feed as feed_mod
from . import locks, noderef, paths, shortid, slack as slack_mod, store, templates
from . import treemap as treemap_mod


class Ctx(object):
    def __init__(self, dh, cfg=None, slack=None, root=None, now=None,
                 effects=None):
        self.dh = Path(dh)
        self.cfg = cfg if cfg is not None else config_mod.load(self.dh)
        # Every touch of the machine goes through here; tests inject a
        # Recording() that refuses to spawn. See effects.py for why.
        self.effects = effects or effects_mod.DEFAULT
        self.slack = slack if slack is not None else slack_mod.Slack.from_config(
            self.cfg, effects=self.effects)
        self.root = Path(root) if root else None
        self._now = now

    def now(self):
        return self._now() if callable(self._now) else (self._now or time.time())

    def trees(self):
        return store.load_all(self.dh)

    def tree(self, proj_id):
        return store.Tree.load(self.dh, proj_id)

    def locale(self, tree=None):
        if tree is not None:
            return tree.data.get("locale") or self.cfg.get("locale", "zh")
        return self.cfg.get("locale", "zh")

    def agent(self, state=None):
        return (state or {}).get("reply_as") or self.cfg.get("default_agent", "canopy")

    def node_dir(self, proj_id, nid):
        return paths.node_dir(self.dh, proj_id, nid)

    def render(self, name, values, tree=None, proj_id=None):
        return templates.render_named(name, values, self.dh, self.locale(tree),
                                      proj_id=proj_id, root=self.root)

    def feed(self, proj_id, state, tree=None):
        return feed_mod.Feed(self.dh, self.cfg, proj_id, state,
                             self.node_dir(proj_id, state["node_id"]),
                             self.slack, locale=self.locale(tree), root=self.root)

    def permalink(self, channel, ts):
        return config_mod.permalink(self.cfg, channel, ts)


_HELD = set()


class tree_lock(object):
    """One writer per project.

    `tree.json` is read whole, modified and written whole by `fork`,
    `set_status`, `rename` and `sync_treemap` — and the CLI paths took no lock
    at all. A cron tick handling `@canopy fork X` while someone typed
    `canopy untrack 1.b` in a terminal produced a lost update: whichever wrote
    last won, and the fork's edge vanished while its Slack messages and node
    state stayed behind, unreachable.

    Atomic writes do not help here; this is lost update, not torn file.
    """

    def __init__(self, ctx, proj_id):
        self.dir = paths.project_dir(ctx.dh, proj_id)
        self.stale = int(ctx.cfg.get("lock_stale_seconds", 1800))
        self.held = None
        self.reentered = False

    def __enter__(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        if str(self.dir) in _HELD:
            # Reentrant within this process: `fork` holds the lock and then
            # calls `sync_treemap`, which takes it too.
            #
            # Tracked in memory, not inferred from the pid in the lock file: pids
            # get recycled, so a SIGKILLed tick's leftover lock would eventually
            # match a later tick's pid, which read as "I already hold this" and
            # turned the mutex into a no-op that also never released.
            self.reentered = True
            return self
        deadline = time.time() + 30
        while True:
            try:
                self.held = locks.acquire(self.dir, stale_after=self.stale)
                _HELD.add(str(self.dir))
                return self
            except locks.LockedError:
                if time.time() > deadline:
                    raise
                time.sleep(0.2)

    def __exit__(self, *exc):
        if not self.reentered:
            _HELD.discard(str(self.dir))
            locks.release(self.dir)
        return False


def _date(ctx):
    return time.strftime("%Y-%m-%d", time.localtime(ctx.now()))


def _title_from_thread(ctx, channel, thread_ts):
    msgs = ctx.slack.thread(channel, thread_ts, limit=1)
    text = (msgs[0]["text"] if msgs else "").strip().splitlines()
    first = text[0] if text else ""
    return (first[:60] or "thread %s" % thread_ts)


def _alias(tree, nid):
    return noderef.aliases(tree)[nid]


def _states(ctx, tree):
    states = {}
    for nid in tree.nodes:
        try:
            states[nid] = store.load_state(ctx.dh, tree.proj_id, nid)
        except FileNotFoundError:
            continue
    return states


def sync_treemap(ctx, tree):
    """Post or update the tree message(s), under the project lock."""
    with tree_lock(ctx, tree.proj_id):
        return _sync_treemap(ctx, tree)


def _sync_treemap(ctx, tree):
    """Post or update the tree message(s). -> the segments, with their ts.

    Two passes on purpose: a segment's rows contain links to *other* segments,
    which only have a ts once they have been posted. So post/refresh every
    segment first with pointer text, then rewrite them now that every ts is
    known. Costs one extra `chat.update` per segment on the tick where the tree
    actually changed shape, and nothing on the ticks where it didn't.
    """
    channel = store.split_node_id(tree.root)[0]
    states = _states(ctx, tree)
    segments = treemap_mod.segments(tree)
    stored = tree.data.setdefault("tree_msgs", [])
    by_index = dict((m["index"], m) for m in stored)

    for seg in segments:
        if seg["index"] not in by_index:
            entry = {"index": seg["index"], "channel": channel, "ts": None,
                     "root": seg["root"]}
            stored.append(entry)
            by_index[seg["index"]] = entry
            entry["ts"] = ctx.slack.post(channel, "…")
        by_index[seg["index"]]["root"] = seg["root"]

    def segment_link(node_id):
        index = treemap_mod.segment_of(tree, node_id)
        msg = by_index.get(index)
        return ctx.permalink(channel, msg["ts"]) if msg and msg.get("ts") else None

    alias_map = noderef.aliases(tree)
    title = tree.node(tree.root).get("title")
    for seg in segments:
        root_state = states.get(tree.root) or {}
        values = {
            "title": title,
            "proj_id": tree.proj_id,
            "root_permalink": root_state.get("raw_permalink", ""),
            "segment_index": seg["index"],
            "body": treemap_mod.render_body(
                tree, seg,
                lambda name, values: ctx.render(name, values, tree=tree,
                                                proj_id=tree.proj_id),
                states=states, permalink=ctx.permalink,
                segment_link=segment_link),
            "counts": treemap_mod.counts_text(tree),
        }
        name = "tree-map.md"
        if seg["index"] > 1:
            parent = tree.parent(seg["root"])
            name = "tree-map-more.md"
            values["parent_alias"] = alias_map[seg["root"]]
            values["parent_permalink"] = segment_link(parent) if parent else ""
        text = ctx.render(name, values, tree=tree, proj_id=tree.proj_id)
        ctx.slack.update(channel, by_index[seg["index"]]["ts"], text)

    for entry in stored:
        if entry["index"] > len(segments):
            # The tree shrank (a branch was untracked). Leave a pointer rather
            # than a stale copy of a tree that no longer exists.
            ctx.slack.update(channel, entry["ts"], ctx.render(
                "tree-map-merged.md", {
                    "title": title,
                    "proj_id": tree.proj_id,
                    "segment_index": entry["index"],
                    "first_permalink": segment_link(tree.root),
                }, tree=tree, proj_id=tree.proj_id))

    tree.save()
    return segments


# -- track --------------------------------------------------------------------

def track(ctx, link, title=None, owner=None, locale=None, proj_id=None,
          agent=None, namer=None):
    """Adopt a live thread: root node, feed, in-thread announce, Canvas."""
    channel, thread_ts = slack_mod.parse_thread_link(link)
    locale = locale or ctx.cfg.get("locale", "zh")
    paths.seed(ctx.dh, locale, root=ctx.root)

    title = title or _title_from_thread(ctx, channel, thread_ts)
    if proj_id:
        if (paths.project_dir(ctx.dh, proj_id) / "tree.json").exists():
            raise ValueError("project %r already tracked" % (proj_id,))
    else:
        suggested = shortid.suggest(ctx.cfg, title, ctx.dh, run=namer,
                                    effects=ctx.effects)
        proj_id = store.unique_proj_id(ctx.dh, suggested or store.slugify(title))

    for existing in store.list_projects(ctx.dh):
        if store.Tree.load(ctx.dh, existing).root == store.node_id(channel, thread_ts):
            raise ValueError("that thread is already tracked as %r" % (existing,))

    nid = store.node_id(channel, thread_ts)
    owner = owner or ctx.cfg.get("owner") or ""
    agent = agent or ctx.cfg.get("default_agent", "canopy")

    tree = store.Tree.new(proj_id, nid, title, owner)
    tree.data["locale"] = locale
    tree.save(ctx.dh)

    state = store.new_state(channel, thread_ts, None, title, owner,
                            ctx.permalink(channel, thread_ts))
    node_dir = ctx.node_dir(proj_id, nid)
    node_dir.mkdir(parents=True, exist_ok=True)
    store.save_state(ctx.dh, proj_id, state)

    _sync_treemap(ctx, tree)
    tree_link = treemap_mod.permalink(tree, ctx.cfg, nid)
    state["tree_permalink"] = tree_link

    feed = ctx.feed(proj_id, state, tree)
    feed_ts = feed.open("root", {
        "title": title,
        "alias": _alias(tree, nid),
        "raw_permalink": state["raw_permalink"],
        "tree_permalink": tree_link,
    })

    announce = ctx.render("track-announce.md", {
        "agent": agent,
        "feed_permalink": ctx.permalink(channel, feed_ts),
        "tree_permalink": tree_link,
    }, tree=tree, proj_id=proj_id)
    # What was already in the thread before we said anything. Setting the cursor
    # to our own announce skipped whatever people posted during the handful of
    # Slack round-trips above; the announce itself is filtered by its identity
    # prefix, so it does not need to be jumped over.
    seen = ctx.slack.thread(channel, thread_ts, limit=200)
    latest_before = seen[-1]["ts"] if seen else thread_ts

    announce_ts = post_into_thread(ctx, channel, announce, thread_ts, agent)

    state["cursor"] = latest_before
    store.save_state(ctx.dh, proj_id, state)
    _sync_treemap(ctx, tree)

    return {"proj_id": proj_id, "node_id": nid, "feed_ts": feed_ts,
            "announce_ts": announce_ts, "tree_permalink": tree_link,
            "title": title}


# -- fork ---------------------------------------------------------------------

def fork(ctx, proj_id, parent_nid, title, agent=None):
    """Open a sub-problem: new thread, new feed, edge written now."""
    with tree_lock(ctx, proj_id):
        return _fork(ctx, proj_id, parent_nid, title, agent=agent)


def _fork(ctx, proj_id, parent_nid, title, agent=None):
    tree = ctx.tree(proj_id)
    parent_state = store.load_state(ctx.dh, proj_id, parent_nid)
    channel = parent_state["channel"]
    agent = agent or ctx.agent(parent_state)
    tree_link = treemap_mod.permalink(tree, ctx.cfg, parent_nid)
    parent_permalink = ctx.permalink(channel, parent_state["thread_ts"])

    def kickoff_text(feed_permalink):
        return ctx.render("fork-thread.md", {
            "agent": agent,
            "title": title,
            "parent_permalink": parent_permalink,
            "feed_permalink": feed_permalink,
            "tree_permalink": tree_link,
        }, tree=tree, proj_id=proj_id)

    # Chicken and egg: the feed can only be opened once the thread exists, and
    # the thread only exists once this message is posted. So post it without the
    # digest link — the empty link degrades to plain text — and edit it back in
    # below. Otherwise the child thread is the one place in the tree with no way
    # to reach its own feed.
    child_ts = ctx.slack.post(channel, kickoff_text(""))

    child_id = store.node_id(channel, child_ts)
    tree.add_child(parent_nid, child_id, title, owner=parent_state.get("owner"))
    tree.save()

    child_state = store.new_state(channel, child_ts, parent_nid, title,
                                  parent_state.get("owner"),
                                  ctx.permalink(channel, child_ts),
                                  tree_permalink=tree_link,
                                  reply_as=parent_state.get("reply_as"))
    ctx.node_dir(proj_id, child_id).mkdir(parents=True, exist_ok=True)
    store.save_state(ctx.dh, proj_id, child_state)

    alias = _alias(tree, child_id)
    breadcrumb = " / ".join([proj_id] +
                            [_alias(tree, a) for a in tree.ancestors(child_id)])
    feed = ctx.feed(proj_id, child_state, tree)
    feed_ts = feed.open("fork", {
        "title": title,
        "alias": alias,
        "breadcrumb": breadcrumb,
        "raw_permalink": child_state["raw_permalink"],
        "parent_permalink": parent_permalink,
        "tree_permalink": tree_link,
    })

    ctx.slack.update(channel, child_ts,
                     kickoff_text(ctx.permalink(channel, feed_ts)))

    announce = ctx.render("fork-announce.md", {
        "agent": agent,
        "alias": alias,
        "title": title,
        "child_raw_permalink": child_state["raw_permalink"],
        "feed_permalink": ctx.permalink(channel, feed_ts),
        "tree_permalink": tree_link,
    }, tree=tree, proj_id=proj_id)
    post_into_thread(ctx, channel, announce, parent_state["thread_ts"], agent)

    store.save_state(ctx.dh, proj_id, child_state)
    sync_treemap(ctx, tree)
    return {"proj_id": proj_id, "node_id": child_id, "alias": alias,
            "thread_ts": child_ts, "feed_ts": feed_ts}


# -- steering -----------------------------------------------------------------

def guide(ctx, proj_id, nid, text, message_ts=None):
    """Append to this node's guide; react instead of posting.

    Steering the summarizer should not add noise to the thread everyone is
    already reading.
    """
    node_dir = ctx.node_dir(proj_id, nid)
    node_dir.mkdir(parents=True, exist_ok=True)
    path = node_dir / "guide.md"
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(ctx.now()))
    with path.open("a", encoding="utf-8") as fh:
        fh.write("- (%s) %s\n" % (stamp, text.strip()))
    if message_ts:
        state = store.load_state(ctx.dh, proj_id, nid)
        ctx.slack.react(state["channel"], message_ts, "white_check_mark")
    return path


def append_checkpoint(ctx, proj_id, nid, summary, author="", raw_permalink=None,
                      icon="•"):
    tree = ctx.tree(proj_id)
    state = store.load_state(ctx.dh, proj_id, nid)
    feed = ctx.feed(proj_id, state, tree)
    result = feed.append(summary, author=author, date=_date(ctx), icon=icon,
                         raw_permalink=raw_permalink)
    store.save_state(ctx.dh, proj_id, state)
    return result


# -- return / ack / done ------------------------------------------------------

def return_draft(ctx, proj_id, nid, summary, agent=None):
    """Draft to a NEW message for A君's review — nothing goes up unreviewed."""
    tree = ctx.tree(proj_id)
    state = store.load_state(ctx.dh, proj_id, nid)
    agent = agent or ctx.agent(state)
    alias = _alias(tree, nid)
    feed_permalink = ctx.permalink(state["channel"], state["feed_ts"][-1]) \
        if state.get("feed_ts") else state.get("raw_permalink")
    text = ctx.render("return-draft.md", {
        "agent": agent,
        "title": state.get("title"),
        "alias": alias,
        "summary": summary,
        "feed_permalink": feed_permalink,
        "tree_permalink": treemap_mod.permalink(tree, ctx.cfg, nid),
    }, tree=tree, proj_id=proj_id)
    ts = ctx.slack.post(state["channel"], text)
    state["return_draft"] = {"ts": ts, "summary": summary}
    store.save_state(ctx.dh, proj_id, state)
    return {"ts": ts, "summary": summary}


def ack_return(ctx, proj_id, nid, agent=None, summary=None):
    tree = ctx.tree(proj_id)
    state = store.load_state(ctx.dh, proj_id, nid)
    draft = state.get("return_draft") or {}
    summary = summary or draft.get("summary")
    if not summary:
        raise ValueError("nothing to ack: no return draft on %s" % (nid,))
    parent = state.get("parent")
    if not parent:
        raise ValueError("root node has no parent to return to")
    parent_state = store.load_state(ctx.dh, proj_id, parent)
    agent = agent or ctx.agent(state)
    feed_permalink = ctx.permalink(state["channel"], state["feed_ts"][-1]) \
        if state.get("feed_ts") else state.get("raw_permalink")
    text = ctx.render("return-post.md", {
        "agent": agent,
        "title": state.get("title"),
        "alias": _alias(tree, nid),
        "summary": summary,
        "feed_permalink": feed_permalink,
    }, tree=tree, proj_id=proj_id)
    ts = post_into_thread(ctx, parent_state["channel"], text,
                          parent_state["thread_ts"], agent)
    # Nothing is recorded about the fact that a return happened. `return` is a
    # convenience for drafting the summary, not a step in a lifecycle: a human
    # who types the conclusion into the parent thread themselves has closed the
    # sub-problem just as well, and the summarizer picks that up into the
    # parent's feed like any other message. State that only some paths maintain
    # is state that lies.
    state.pop("return_draft", None)
    store.save_state(ctx.dh, proj_id, state)
    return {"ts": ts, "parent": parent, "summary": summary}


def set_status(ctx, proj_id, nid, status, reason="", agent=None):
    with tree_lock(ctx, proj_id):
        return _set_status(ctx, proj_id, nid, status, reason=reason, agent=agent)


def _set_status(ctx, proj_id, nid, status, reason="", agent=None):
    tree = ctx.tree(proj_id)
    state = store.load_state(ctx.dh, proj_id, nid)
    tree.set_status(nid, status)
    tree.save()
    state["status"] = status
    store.save_state(ctx.dh, proj_id, state)
    sync_treemap(ctx, tree)

    agent = agent or ctx.agent(state)
    # One template per state rather than one with a `{{status}}` hole: the
    # sentence differs, and "→ untracked" inside a Chinese message reads like a
    # log line, not like a colleague.
    name = "status-untracked.md" if status == "untracked" else "status-tracked.md"
    text = ctx.render(name, {
        "agent": agent,
        "title": state.get("title"),
        "alias": _alias(tree, nid),
        "reason": reason,
    }, tree=tree, proj_id=proj_id)
    ts = None
    if state.get("feed_ts"):
        ts = ctx.slack.post(state["channel"], text,
                            thread_ts=state["feed_ts"][-1])
    return {"status": status, "ts": ts}


def rename(ctx, proj_id, nid, title):
    with tree_lock(ctx, proj_id):
        return _rename(ctx, proj_id, nid, title)


def _rename(ctx, proj_id, nid, title):
    """Retitle a node everywhere it already got written.

    `track` derives a title from the thread's first line, which is a guess —
    often a truncated sentence. Fixing it has to reach three places: the tree,
    the node state, and the feed segment headers that were rendered from the old
    one. Missing any of them leaves two names for one problem.
    """
    tree = ctx.tree(proj_id)
    state = store.load_state(ctx.dh, proj_id, nid)
    tree.nodes[nid]["title"] = title
    tree.save()
    state["title"] = title
    store.save_state(ctx.dh, proj_id, state)

    node_dir = ctx.node_dir(proj_id, nid)
    segments = feed_mod.load_segments(node_dir)
    feed = ctx.feed(proj_id, state, tree)
    for segment in segments:
        if "title" in (segment.get("vars") or {}):
            segment["vars"]["title"] = title
        ctx.slack.update(state["channel"], segment["ts"],
                         feed.render_segment(segment))
    if segments:
        feed_mod.save_segments(node_dir, segments)

    sync_treemap(ctx, tree)
    return {"node_id": nid, "title": title, "segments": len(segments)}


def reply(ctx, proj_id, nid, body, agent=None):
    """The one way anything gets posted into a node's thread by a worker."""
    tree = ctx.tree(proj_id)
    state = store.load_state(ctx.dh, proj_id, nid)
    agent = agent or ctx.agent(state)
    text = ctx.render("reply.md", {"agent": agent, "body": body},
                      tree=tree, proj_id=proj_id)
    return post_into_thread(ctx, state["channel"], text, state["thread_ts"],
                            agent)


def post_notice(ctx, proj_id, nid, template, agent=None, **values):
    """A one-line notice from Canopy into the node's thread."""
    tree = ctx.tree(proj_id)
    state = store.load_state(ctx.dh, proj_id, nid)
    agent = agent or ctx.agent(state)
    text = ctx.render(template, dict(values, agent=agent), tree=tree,
                      proj_id=proj_id)
    _must_be_recognisable(text, agent)
    return ctx.slack.post(state["channel"], text, thread_ts=state["thread_ts"])


def post_into_thread(ctx, channel, text, thread_ts, agent):
    """Every message Canopy puts inside a watched thread goes through here.

    The check used to sit on `reply` and `post_notice` only, so
    `fork-announce.md` and `return-post.md` — both of which land in a *parent*
    node's thread, before that node's cursor — could be edited into something
    Canopy no longer recognises as its own, and the parent would then wake on
    its own announcement every tick.
    """
    _must_be_recognisable(text, agent)
    return ctx.slack.post(channel, text, thread_ts=thread_ts)


def _must_be_recognisable(text, agent):
    """Refuse to post anything Canopy could not recognise as its own.

    The tick skips its own messages by looking for the identity prefix. Edit
    `reply.md` so the prefix moves or changes — which the whole
    every-byte-is-a-template design invites — and Canopy answers its own reply,
    every tick, burning a worker each time. Fail here, on the machine of the
    person who just edited the template, instead of at 3am in the channel.
    """
    from . import mentions
    if not mentions.is_own_post(text, [agent]):
        raise ValueError(
            "this template renders a message Canopy cannot recognise as its "
            "own, which would make it reply to itself every tick; keep the "
            "`[%s]` prefix at the start: %r" % (agent, text[:80]))
