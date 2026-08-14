"""Every operation that changes a tree, in one place.

Both entry points land here: `/canopy …` from A君's terminal and `@agent …`
from a Slack thread. They must do the same thing — a fork typed in Slack and a
fork typed in the terminal cannot produce differently-shaped state — so the
command layers stay thin and this module holds the behaviour.
"""

import time
from pathlib import Path

from . import config as config_mod
from . import feed as feed_mod
from . import noderef, paths, shortid, slack as slack_mod, store, templates
from . import treemap as treemap_mod


class Ctx(object):
    def __init__(self, dh, cfg=None, slack=None, root=None, now=None):
        self.dh = Path(dh)
        self.cfg = cfg if cfg is not None else config_mod.load(self.dh)
        self.slack = slack if slack is not None else slack_mod.Slack(
            workspace=self.cfg.get("slack_workspace"))
        self.root = root
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
        values = {
            "title": title,
            "proj_id": tree.proj_id,
            "segment_index": seg["index"],
            "body": treemap_mod.render_body(tree, seg, states=states,
                                            permalink=ctx.permalink,
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
        suggested = shortid.suggest(ctx.cfg, title, ctx.dh, run=namer)
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

    sync_treemap(ctx, tree)
    tree_link = treemap_mod.permalink(tree, ctx.cfg, nid)
    state["tree_permalink"] = tree_link

    feed = ctx.feed(proj_id, state, tree)
    feed_ts = feed.open("root", {
        "title": title,
        "alias": _alias(tree, nid),
        "owner": owner,
        "status": "active",
        "raw_permalink": state["raw_permalink"],
        "tree_permalink": tree_link,
    })

    announce = ctx.render("track-announce.md", {
        "agent": agent,
        "feed_permalink": ctx.permalink(channel, feed_ts),
        "tree_permalink": tree_link,
    }, tree=tree, proj_id=proj_id)
    announce_ts = ctx.slack.post(channel, announce, thread_ts=thread_ts)

    state["cursor"] = announce_ts
    store.save_state(ctx.dh, proj_id, state)
    sync_treemap(ctx, tree)

    return {"proj_id": proj_id, "node_id": nid, "feed_ts": feed_ts,
            "announce_ts": announce_ts, "tree_permalink": tree_link,
            "title": title}


# -- fork ---------------------------------------------------------------------

def fork(ctx, proj_id, parent_nid, title, agent=None):
    """Open a sub-problem: new thread, new feed, edge written now."""
    tree = ctx.tree(proj_id)
    parent_state = store.load_state(ctx.dh, proj_id, parent_nid)
    channel = parent_state["channel"]
    agent = agent or ctx.agent(parent_state)
    tree_link = treemap_mod.permalink(tree, ctx.cfg, parent_nid)
    parent_permalink = ctx.permalink(channel, parent_state["thread_ts"])

    kickoff = ctx.render("fork-thread.md", {
        "agent": agent,
        "title": title,
        "parent_permalink": parent_permalink,
        "tree_permalink": tree_link,
    }, tree=tree, proj_id=proj_id)
    child_ts = ctx.slack.post(channel, kickoff)

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
        "owner": child_state.get("owner") or "",
        "status": "active",
        "breadcrumb": breadcrumb,
        "raw_permalink": child_state["raw_permalink"],
        "parent_permalink": parent_permalink,
        "tree_permalink": tree_link,
    })

    announce = ctx.render("fork-announce.md", {
        "agent": agent,
        "alias": alias,
        "title": title,
        "child_raw_permalink": child_state["raw_permalink"],
        "feed_permalink": ctx.permalink(channel, feed_ts),
    }, tree=tree, proj_id=proj_id)
    ctx.slack.post(channel, announce, thread_ts=parent_state["thread_ts"])

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
    ts = ctx.slack.post(parent_state["channel"], text,
                        thread_ts=parent_state["thread_ts"])
    state.pop("return_draft", None)
    store.save_state(ctx.dh, proj_id, state)
    return {"ts": ts, "parent": parent, "summary": summary}


def set_status(ctx, proj_id, nid, status, reason="", agent=None):
    tree = ctx.tree(proj_id)
    state = store.load_state(ctx.dh, proj_id, nid)
    tree.set_status(nid, status)
    tree.save()
    state["status"] = status
    store.save_state(ctx.dh, proj_id, state)
    sync_treemap(ctx, tree)

    agent = agent or ctx.agent(state)
    text = ctx.render("status-change.md", {
        "agent": agent,
        "title": state.get("title"),
        "alias": _alias(tree, nid),
        "status": status,
        "reason": reason,
        "tree_permalink": treemap_mod.permalink(tree, ctx.cfg, nid),
    }, tree=tree, proj_id=proj_id)
    ts = None
    if state.get("feed_ts"):
        ts = ctx.slack.post(state["channel"], text,
                            thread_ts=state["feed_ts"][-1])
    return {"status": status, "ts": ts}


def reply(ctx, proj_id, nid, body, agent=None):
    """The one way anything gets posted into a node's thread by a worker."""
    tree = ctx.tree(proj_id)
    state = store.load_state(ctx.dh, proj_id, nid)
    agent = agent or ctx.agent(state)
    text = ctx.render("reply.md", {"agent": agent, "body": body},
                      tree=tree, proj_id=proj_id)
    return ctx.slack.post(state["channel"], text, thread_ts=state["thread_ts"])
