"""`/canopy <cmd>` — the local CLI A君 drives the tree with."""

import argparse
import json
import sys
from pathlib import Path

from . import config as config_mod
from . import cron, events, noderef, ops, opsview, paths
from . import runner as runner_mod, schedule, webserve
from . import store, templates, tick as tick_mod, treemap as treemap_mod
from . import treeview, worker
from .errors import CanopyError, NodeRefError


def _ctx(args):
    dh = paths.data_home(getattr(args, "data_dir", None))
    dh.mkdir(parents=True, exist_ok=True)
    cfg = config_mod.load(dh)
    return ops.Ctx(dh, cfg=cfg)


def _resolve(ctx, ref):
    trees = ctx.trees()
    if not trees:
        raise NodeRefError("nothing tracked yet — start with `canopy track <link>`")
    return noderef.resolve(ref, trees)


def _locked_nodes(ctx, proj_id, tree):
    from . import locks
    stale = int(ctx.cfg.get("lock_stale_seconds", 1800))
    held = set()
    for nid in tree.nodes:
        if locks.is_held(ctx.node_dir(proj_id, nid), stale_after=stale):
            held.add(nid)
    return held


# -- commands -----------------------------------------------------------------

def cmd_track(args):
    ctx = _ctx(args)
    if "/archives/" not in args.link:
        # `track <node ref>` re-opens a node someone parked. Same verb as
        # adopting a fresh thread, because it is the same intent: watch this.
        proj_id, nid = _resolve(ctx, args.link)
        result = ops.set_status(ctx, proj_id, nid, "active",
                                reason=getattr(args, "reason", "") or "")
        print("%s -> %s%s" % (nid, result["status"], _cron_note(ctx)))
        return 0
    result = ops.track(ctx, args.link, title=args.title, owner=args.owner,
                       locale=args.locale, proj_id=args.project)
    if not args.no_cron:
        try:
            ctx.cfg = config_mod.set_values(
                ctx.dh,
                runner_path=runner_mod.resolve_path(ctx.cfg.get("runner", "codex")),
                slack_cli_path=runner_mod.resolve_path(
                    ctx.cfg.get("slack_cli", "slackcli")),
            )
        except CanopyError as exc:
            raise CanopyError(
                "%s\nNo cron job was registered — a tree that looks watched but "
                "never ticks is worse than a failed track." % (exc,))
        schedule.sync(ctx.dh, ctx.cfg)
        # Same gate as cron: `--no-cron` means "set up the state, wire nothing
        # up", and that includes not leaving a viewer process behind.
        viewer = webserve.start_background(ctx.dh, ctx.cfg, root=ctx.root)
        _open(viewer["url"])
    print("tracked %s as `%s`" % (result["title"], result["proj_id"]))
    print("  feed     %s" % ctx.permalink(result["node_id"].split("-")[0],
                                          result["feed_ts"]))
    print("  树消息   %s" % result["tree_permalink"])
    if not args.no_cron:
        print("  运维页   %s" % viewer["url"])
    return 0


def _open(path):
    """Open the ops page in whatever the desktop uses. Never fatal."""
    import subprocess
    for argv in (["open", str(path)], ["xdg-open", str(path)]):
        try:
            subprocess.run(argv, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=True)
            return True
        except (OSError, subprocess.CalledProcessError):
            continue
    return False


def cmd_serve(args):
    """The ops page, served locally.

    Foreground by default so it is obvious a process is running; `track` starts
    it detached because nobody wants to keep a terminal open for a dashboard.
    """
    ctx = _ctx(args)
    if args.stop:
        print("stopped" if webserve.stop(ctx.dh) else "not running")
        return 0
    if args.status:
        print(json.dumps(webserve.status(ctx.dh), ensure_ascii=False))
        return 0
    if args.background:
        viewer = webserve.start_background(ctx.dh, ctx.cfg, root=ctx.root,
                                           port=args.port)
        print(viewer["url"])
        if not args.no_open:
            _open(viewer["url"])
        return 0

    port = webserve.free_port(args.port or int(ctx.cfg.get("serve_port", 8787)))
    url = "http://127.0.0.1:%d/" % port
    print("serving %s  (Ctrl-C 停)" % url)
    if not args.no_open:
        _open(url)
    try:
        webserve.serve(ctx.dh, ctx.cfg, root=ctx.root, port=port)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def cmd_agents(args):
    ctx = _ctx(args)
    # Same rule as track: seeds land on first use, so `agents` on a fresh
    # install shows the default instead of an empty list.
    paths.seed(ctx.dh, ctx.cfg.get("locale", "zh"), root=ctx.root)
    directory = paths.profiles_dir(ctx.dh)
    directory.mkdir(parents=True, exist_ok=True)
    default = ctx.cfg.get("default_agent", "canopy")

    if args.delete:
        if args.delete == default:
            raise CanopyError(
                "%s is the default agent; point default_agent at another "
                "profile first, or nodes without reply_as have nobody to "
                "answer as." % (args.delete,))
        path = directory / ("%s.md" % args.delete)
        if not path.exists():
            raise CanopyError("no such profile: %s" % (args.delete,))
        path.unlink()
        print("deleted %s" % path)
        return 0

    if args.create:
        path = directory / ("%s.md" % args.create)
        if path.exists():
            raise CanopyError("profile already exists: %s" % (path,))
        path.write_text("# %s\n\n(describe what this agent is for)\n"
                        % args.create, encoding="utf-8")
        print("created %s" % path)
        return 0

    for name in worker.agent_names(ctx.dh):
        mark = "  (default)" if name == default else ""
        print("%-16s %s%s" % (name, directory / ("%s.md" % name), mark))
    return 0


def cmd_messages(args):
    ctx = _ctx(args)
    locale = args.locale or ctx.cfg.get("locale", "zh")
    paths.seed(ctx.dh, locale, root=ctx.root)

    if args.refresh:
        # Seeds are copied out once and then never touched again, so a skill
        # update leaves the user reading their stale copy and wondering why the
        # new wording never showed up. This re-copies the ones they never
        # edited; anything they did edit is reported, not overwritten.
        updated, kept = paths.refresh(ctx.dh, locale, root=ctx.root,
                                      force=args.force)
        for path in updated:
            print("updated  %s" % path)
        for path in kept:
            print("kept     %s  (edited — use --force to overwrite)" % path)
        if not updated and not kept:
            print("already up to date")
        return 0

    if args.name and args.preview:
        values = _preview_values(ctx, args)
        text = templates.render_named(args.name, values, ctx.dh, locale,
                                      proj_id=args.project, root=ctx.root)
        print(text)
        return 0

    if args.name:
        path, layer = templates.resolve(args.name, ctx.dh, locale,
                                        proj_id=args.project, root=ctx.root)
        print("# %s (%s)\n" % (path, layer))
        print(path.read_text(encoding="utf-8"))
        return 0

    for name, layer, path in templates.inventory(ctx.dh, locale,
                                                 proj_id=args.project,
                                                 root=ctx.root):
        print("%-24s %-8s %s" % (name, layer, path))
    return 0


def _preview_values(ctx, args):
    """Render against a real node when given one, a fixture otherwise."""
    if args.node:
        proj_id, nid = _resolve(ctx, args.node)
        tree = ctx.tree(proj_id)
        state = store.load_state(ctx.dh, proj_id, nid)
        alias = noderef.aliases(tree)[nid]
        feed_ts = (state.get("feed_ts") or [None])[-1]
        return {
            "agent": ctx.agent(state), "title": state.get("title"),
            "alias": alias, "owner": state.get("owner"), "status": state.get("status"),
            "breadcrumb": " / ".join([proj_id] +
                                     [noderef.aliases(tree)[a]
                                      for a in tree.ancestors(nid)]),
            "raw_permalink": state.get("raw_permalink"),
            "parent_permalink": state.get("raw_permalink"),
            "child_raw_permalink": state.get("raw_permalink"),
            "tree_permalink": treemap_mod.permalink(tree, ctx.cfg, nid) or "#tree",
            "feed_permalink": ctx.permalink(state["channel"], feed_ts) if feed_ts else "",
            "entries": "", "summary": "(summary)", "body": "(reply body)",
            "reason": "", "icon": "•", "author": "someone", "date": "2026-01-01",
            "segment_index": 2, "prev_segment_index": 1, "next_segment_index": 2,
            "prev_segment_permalink": "#", "next_segment_permalink": "#",
        }
    return {
        "agent": ctx.cfg.get("default_agent", "canopy"), "title": "示例问题",
        "alias": "1.a", "owner": "A君", "status": "active",
        "breadcrumb": "example / 1", "raw_permalink": "#raw",
        "parent_permalink": "#parent", "child_raw_permalink": "#child",
        "tree_permalink": "#tree", "feed_permalink": "#feed",
        "entries": "• 示例 checkpoint", "summary": "(summary)",
        "body": "(reply body)", "reason": "", "icon": "•", "author": "someone",
        "date": "2026-01-01", "segment_index": 2, "prev_segment_index": 1,
        "next_segment_index": 2, "prev_segment_permalink": "#",
        "next_segment_permalink": "#",
    }


def cmd_tree(args):
    ctx = _ctx(args)
    trees = ctx.trees()
    if not trees:
        print("nothing tracked yet")
        return 0

    depth = _parse_depth(args.depth)
    start = None
    locked = set()
    if args.ref:
        proj_id, nid = _resolve(ctx, args.ref)
        start = (proj_id, nid)
        locked = _locked_nodes(ctx, proj_id, trees[proj_id])
        if depth == "unset":
            depth = None  # named something -> expand
    else:
        for proj_id, tree in trees.items():
            locked |= _locked_nodes(ctx, proj_id, tree)
        if depth == "unset":
            depth = 0  # the daily dashboard

    for line in treeview.render(trees, start=start, depth=depth, locked=locked):
        print(line)
    return 0


def _parse_depth(raw):
    if raw is None:
        return "unset"
    if str(raw).lower() == "all":
        return None
    return int(raw)


def cmd_status(args):
    return cmd_tree(args)


def cmd_untrack(args):
    """Stop watching. Not final: `track <ref>` puts it back."""
    return _status_cmd(args, "untracked")


def _status_cmd(args, status):
    ctx = _ctx(args)
    proj_id, nid = _resolve(ctx, args.ref)
    result = ops.set_status(ctx, proj_id, nid, status,
                            reason=getattr(args, "reason", "") or "")
    print("%s -> %s%s" % (nid, result["status"], _cron_note(ctx)))
    return 0


def _cron_note(ctx):
    """Keep the cron entry in step with whether anything is still watched."""
    outcome = schedule.sync(ctx.dh, ctx.cfg)
    if outcome == schedule.REMOVED:
        return "  (没有活跃节点了,cron 也撤了)"
    if outcome == schedule.INSTALLED:
        return "  (cron 装回来了)"
    return ""


def cmd_rename(args):
    ctx = _ctx(args)
    proj_id, nid = _resolve(ctx, args.ref)
    result = ops.rename(ctx, proj_id, nid, args.title)
    print("%s -> %s" % (nid, result["title"]))
    return 0


def cmd_recalibrate(args):
    ctx = _ctx(args)
    proj_id, nid = _resolve(ctx, args.ref)
    result = worker.recalibrate(ctx, proj_id, nid)
    print("rebuilt %d checkpoints across %d segment(s)"
          % (result["checkpoints"], result["segments"]))
    return 0


def cmd_map(args):
    """Re-post/refresh the tree message(s) and print where they are."""
    ctx = _ctx(args)
    trees = ctx.trees()
    if not trees:
        print("nothing tracked yet")
        return 0
    if args.ref:
        proj_id, _nid = _resolve(ctx, args.ref)
        trees = {proj_id: trees[proj_id]}
    for proj_id, tree in sorted(trees.items()):
        segments = ops.sync_treemap(ctx, tree)
        for seg in segments:
            print("%-24s 第 %d 段  %s" % (proj_id, seg["index"],
                                          treemap_mod.permalink(tree, ctx.cfg,
                                                                seg["root"])))
    return 0


def cmd_reply(args):
    """The dedicated reply tool: how a worker posts without the user's identity."""
    ctx = _ctx(args)
    proj_id, nid = _resolve(ctx, args.ref)
    body = args.text if args.text is not None else sys.stdin.read()
    ts = ops.reply(ctx, proj_id, nid, body.strip(), agent=args.agent)
    print(ts)
    return 0


def cmd_tick(args):
    """One tick. Always leaves a line in tick.log — including when it dies.

    cron mails a traceback to a local mailbox nobody reads; the first real bug
    here (slackcli missing from cron's PATH) sat in that mailbox through several
    ticks. A log next to the state is where someone will actually look.
    """
    ctx = _ctx(args)
    try:
        results = tick_mod.tick(ctx)
        # An `@canopy untrack` typed in Slack can retire the last active node;
        # the entry that just woke us should go with it.
        schedule.sync(ctx.dh, ctx.cfg)
    except Exception as exc:
        _log_tick(ctx.dh, "ERROR %s: %s" % (type(exc).__name__, exc))
        raise
    summary = {}
    for row in results:
        summary[row.get("verdict")] = summary.get(row.get("verdict"), 0) + 1
    _log_tick(ctx.dh, " ".join("%s=%d" % kv for kv in sorted(summary.items()))
              or "nothing tracked")
    if args.json:
        print(json.dumps(results, ensure_ascii=False))
    else:
        for row in results:
            print("%-20s %-28s %s" % (row.get("project"), row.get("node"),
                                      row.get("verdict")))
    return 0


def _log_tick(dh, message):
    import time as _time
    line = "%s %s\n" % (_time.strftime("%Y-%m-%d %H:%M:%S"), message)
    with (Path(dh) / "tick.log").open("a", encoding="utf-8") as fh:
        fh.write(line)


def cmd_config(args):
    ctx = _ctx(args)
    if args.set:
        values = {}
        for pair in args.set:
            key, _, value = pair.partition("=")
            values[key.strip()] = _coerce(value.strip())
        cfg = config_mod.set_values(ctx.dh, **values)
    else:
        cfg = ctx.cfg
    print(json.dumps(cfg, ensure_ascii=False, indent=2))
    return 0


def _coerce(value):
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none", ""):
        return None
    try:
        return int(value)
    except ValueError:
        return value


# -- wiring -------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(prog="canopy")
    parser.add_argument("--data-dir", help="override $CANOPY_DATA_HOME")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("track", help="adopt a Slack thread, or re-open a node")
    p.add_argument("link", metavar="link|node")
    p.add_argument("--reason", default="")
    p.add_argument("--title")
    p.add_argument("--owner")
    p.add_argument("--locale")
    p.add_argument("--project")
    p.add_argument("--no-cron", action="store_true")
    p.set_defaults(func=cmd_track)

    p = sub.add_parser("agents", help="list / create / delete agent profiles")
    p.add_argument("--create")
    p.add_argument("--delete")
    p.set_defaults(func=cmd_agents)

    p = sub.add_parser("messages", help="review message templates")
    p.add_argument("name", nargs="?")
    p.add_argument("--preview", action="store_true")
    p.add_argument("--refresh", action="store_true",
                   help="re-copy shipped templates you have not edited")
    p.add_argument("--force", action="store_true",
                   help="with --refresh: overwrite edited ones too")
    p.add_argument("--node")
    p.add_argument("--project")
    p.add_argument("--locale")
    p.set_defaults(func=cmd_messages)

    for name in ("tree", "status"):
        p = sub.add_parser(name, help="print the tree")
        p.add_argument("ref", nargs="?")
        p.add_argument("--depth")
        p.set_defaults(func=cmd_tree)

    p = sub.add_parser("untrack", help="stop watching a node (reversible)")
    p.add_argument("ref")
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_untrack)

    p = sub.add_parser("rename", help="retitle a node (tree, state, feed headers)")
    p.add_argument("ref")
    p.add_argument("title")
    p.set_defaults(func=cmd_rename)

    p = sub.add_parser("recalibrate", help="rebuild a node's feed")
    p.add_argument("ref")
    p.set_defaults(func=cmd_recalibrate)

    p = sub.add_parser("map", help="refresh the tree message(s) in Slack")
    p.add_argument("ref", nargs="?")
    p.set_defaults(func=cmd_map)

    p = sub.add_parser("reply", help="post into a node's thread as an agent")
    p.add_argument("ref")
    p.add_argument("--text")
    p.add_argument("--agent")
    p.set_defaults(func=cmd_reply)

    p = sub.add_parser("tick", help="one cron tick")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_tick)

    p = sub.add_parser("serve", help="serve the local ops page")
    p.add_argument("--port", type=int)
    p.add_argument("--background", action="store_true")
    p.add_argument("--stop", action="store_true")
    p.add_argument("--status", action="store_true")
    p.add_argument("--no-open", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("config", help="show or set config values")
    p.add_argument("--set", action="append")
    p.set_defaults(func=cmd_config)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args) or 0
    except CanopyError as exc:
        sys.stderr.write("canopy: %s\n" % (exc,))
        return 1
