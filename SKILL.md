---
name: canopy
description: >-
  Turn a sprawling Slack discussion into a navigable tree of sub-problems, each
  with its own checkpoint feed. Use this whenever a Slack thread keeps forking
  into side-problems that need their own owners and stakeholders, when someone
  needs to "keep track of the whole problem tree" across a project, or when a
  stakeholder wants a curated progress feed instead of raw chat history. Trigger
  on: /canopy, "track this thread", "fork a sub-problem", "checkpoint feed",
  "problem tree", "who's the owner of this sub-thread", and any request to watch
  a Slack thread and summarize progress for observers. Canopy runs off-line via
  cron + `claude -p`; it never keeps a long-lived process.
---

# Canopy

## What problem this solves

Real work in Slack forks like a tree. A君 discusses problem 1 in a thread with
B/C. Mid-way a side-problem 1.a appears; A君 owns it, opens a sub-thread, pulls
in E/F. When 1.a concludes, its result is fed back up to the thread for problem
1. This nests arbitrarily — 1.a.i, 1.a.ii — but Slack only gives you channel +
one level of thread. Three pains fall out:

1. **No infinite nesting.** The tree is real but Slack can't represent it.
2. **Observers drown.** Stakeholder R君 cares about problem 1's *key progress*,
   not its raw message stream. Every node may have observers like R君 who want a
   checkpoint feed, not overwhelming chat history.
3. **The backstop owner can't navigate.** A君 owns the whole tree and must chase
   every sub/subsub node to closure, but has no fast interface across it.

Canopy is a skill A君's local agent runs. It maintains the tree off to the side,
watches each active node for new messages, dispatches `@agent` mentions to
`claude -p` workers, keeps a curated checkpoint feed per node, and renders a
clickable Canvas of the whole tree.

## Core architecture: two-tier cron, nothing long-lived

Canopy has **no daemon**. All state lives on disk. A cron job fires every N
minutes and runs a cheap gate; only when there is real work does it spend tokens
on `claude -p`. This is deliberate — a cold-start-from-disk worker is crash-safe
(if it dies, next tick re-pulls from the cursor), naturally serializes via a lock
file, and never leaks a zombie process when the desktop sleeps or reboots.

Two tiers per tick:

```
cron tick (plain shell + slackcli, ZERO LLM):
  load tree.json → for each node with status == active:
    latest_ts = slackcli latest ts of this thread     # one cheap call
    if latest_ts <= state.cursor:  continue            # no new msgs → skip, no LLM
    if node lock exists:           continue            # previous tick still running
    grep new messages for `@<agent>` mentions
      → has mention:  spawn FULL agent worker  (claude -p, profile + node state)
      → no mention:   spawn LIGHT summarizer   (claude -p, small prompt, feed only)
```

The gate matters: **nodes with no new messages never touch an LLM.** Idle trees
cost almost nothing. A node that only saw chatter (no `@agent`) gets the cheap
summarizer, not the full agent. Every worker, on wake, does its job, advances
`cursor`, releases its lock, and exits.

**Lock is mandatory.** Each node has a `lock` file (holds pid + start ts). If a
worker overruns one tick, the next tick sees the lock and skips that node this
round — new messages just wait and get picked up next tick (accepted: no queue,
cursor re-pull covers it). Guard against dead locks with a staleness timeout.

## Code / data separation — never write into the skill

The skill is a git repo that gets installed by many people and PR'd back.
**Runtime must never write a byte into the skill root**, or it can't be updated
or contributed back. All read/write goes to a per-user data home.

```
skill-root/                      # git, READ-ONLY at runtime, PR-able
  SKILL.md
  scripts/                       # cron-tick, reply tool, summarizer wrappers, entrypoints
  templates/
    profiles/*.md                # seed profiles (copied out on first use)
    default-summarizer.md        # default feed prompt
    canvas.tmpl

$CANOPY_DATA_HOME  (default ~/.canopy/)   # per-user, NOT in the repo
  config.json                    # cron interval, slack token ref, data path
  profiles/<agent>.md            # the user's real, editable global agents
  projects/<projId>/             # one per `track`
    tree.json
    nodes/<channel>-<thread_ts>/
      state.json
      guide.md                   # summarizer guidance, append-only
      transcript.jsonl           # raw messages, incrementally persisted
      lock                       # present == in use (pid + start ts)
```

Scripts resolve data via `$CANOPY_DATA_HOME` (default `~/.canopy/`), overridable
per-project with `--data-dir` (e.g. to sync/back up via a git-ignored folder).
On first `track`/`agents`, if `profiles/` is empty, **copy the seeds** from
`templates/profiles/` into the data home. From then on the user edits their copy;
skill updates refresh the templates without clobbering user profiles.

Profiles are **global** (shared across all projects). Reply identity is chosen
**per node** (`reply_as`), so 1.a can answer as `@arch` while 1.b answers as
`@qa`, all drawing from the one profile pool.

## The three loops

### Loop A — watch & dispatch
Driven by the tick above. When a full worker wakes, it loads the node's
`state.json` + the matched global `profiles/<agent>.md` + only the new messages
(never full history), acts, and posts back to the thread through the **dedicated
reply tool**. Never post with the user's own token — that impersonates A君.
Every reply is prepended with the agent's identity: `[$agentName]: …`.

The profile is global, but the worker's prompt is wrapped with a "stay focused on
*this* node's problem; keep context clean" instruction so a general profile
doesn't wander. If it genuinely needs upstream context, it **asks A君 first**,
then may read the parent node's `transcript.jsonl`.

### Loop B — checkpoint feed
Each node owns a `feed` — a checkpoint message posted **in the same channel** at
`track`/`fork` time, linking the raw thread permalink and the Canvas. `feed_ts`
is an **array of segments** (see below). On each wake the summarizer reads
`templates/default-summarizer.md` + the node's `guide.md` + new messages, decides
for itself whether progress is "checkpoint-worthy," and if so `chat.update`s the
current active segment (append-style list, each entry linking the raw permalink).
An observer like R君 subscribes to nothing — they just pin/bookmark the feed
message they care about.

Steer the summarizer inline: `@<agent> guide: <text>` in the thread appends to
`guide.md` and takes effect next tick.

**Feed segmentation (solves the length cap + keeps recalibrate cheap):** a Slack
message has a length cap. When the active segment fills, **seal it (never edit it
again)** and post a new message as the next segment, appending its ts to the
`feed_ts` array. A normal `recalibrate` only rewrites the **last (active)**
segment, so it always reads just the increment and can never blow the context
window. Sealed segments are immutable history.

### Loop C — recalibrate (full rebuild)
`@<agent> recalibrate` (or `/canopy recalibrate <node>`) spawns a dedicated
summarizer that reads the **entire** history in chunks — each chunk compressed to
an intermediate note to avoid context blowup — then rebuilds every segment and
overwrites the whole feed in one pass. This is the heavy escape hatch; the
segmented per-tick update is the cheap common path.

## Commands

### Local CLI — `/canopy <cmd>` (A君 → the agent)

- `track <slackThreadLink>` — **main entrypoint.** Create the root node +
  `tree.json`, post the root checkpoint message (linking raw thread + Canvas),
  register the cron job, build the Canvas.
- `agents` — enter profile-edit mode: create / edit / delete global
  `profiles/*.md`.
- `tree` / `status` — print the tree with each node's status / owner / lock
  state. No arg → list all tracked projects; with a projId → one tree.
- `pause <node>` / `resume <node>` — stop / restart watching a node.
- `recalibrate <node>` — CLI form of Loop C.
- `canvas` — force-regenerate and print the Canvas link.
- `untrack <node>` — archive the node, stop watching, grey it in the Canvas.

### In-thread — `@<agent> <cmd>` (posted in Slack)

- `fork <title>` — open a sub-problem. The **current thread_ts becomes parent**;
  add the edge to `tree.json`, post a new root checkpoint message in the same
  channel (linking the parent message + Canvas). The edge is written at fork
  time — never inferred later.
- `return` — draft a summary to a **new message** for A君 to review before it
  goes up.
- `ack return` — confirm; post the summary back into the **parent thread** as
  `[$agentName]: …`, linking the child's feed permalink.
- `guide: <text>` — append to this node's `guide.md`; effective next tick.
- `recalibrate` — rebuild this node's feed (Loop C).
- `done` — mark the node complete; tick it in the Canvas (pairs with `return`).

## State schemas

### tree.json (per project)
```json
{
  "root": "C123-1699.0001",
  "canvas_id": "F0ABC",
  "nodes": {
    "C123-1699.0001": {
      "parent": null,
      "children": ["C123-1699.0042"],
      "title": "问题1",
      "status": "active"
    },
    "C123-1699.0042": {
      "parent": "C123-1699.0001",
      "children": [],
      "title": "问题1.a",
      "status": "active"
    }
  }
}
```

### state.json (per node)
```json
{
  "node_id": "C123-1699.0042",
  "channel": "C123",
  "thread_ts": "1699.0042",
  "parent": "C123-1699.0001",
  "title": "问题1.a",
  "owner": "A君",
  "status": "active",
  "cursor": "1699.5000",
  "feed_ts": ["1699.0043", "1701.9000"],
  "raw_permalink": "https://.../p1699000042",
  "canvas_permalink": "https://.../canvas",
  "reply_as": "arch"
}
```
`status`: `active` | `paused` | `done`. `feed_ts`: sealed segments first, last
entry is the live one. `reply_as`: default reply identity for this node.

## Canvas rendering

Whenever tree structure or a node's status changes, regenerate the Canvas from
`templates/canvas.tmpl`: a clickable tree/graph where each node links its raw
thread permalink and its feed message, and `done` nodes are ticked, `paused`/
`untracked` greyed. This is A君's fast navigation surface across the whole tree.

## Operating principles

- **Spend tokens only on real work.** The cheap gate exists so idle trees are
  free; the light-summarizer path exists so pure chatter never wakes a full
  agent. Preserve both tiers.
- **Disk is the source of truth.** Every worker cold-starts from disk and writes
  back cursor + lock. Never assume in-memory state survives between ticks.
- **Never impersonate the human.** Post through the reply tool, always identity-
  prefixed. The user's token is not a posting identity.
- **Keep worker context clean.** Feed a worker its node + increment, not the
  whole tree or full history. Cross-node reads happen only on explicit ask.
