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
  cron plus a headless coding CLI — `codex exec` by default, `claude -p` also
  supported; it never keeps a long-lived process.
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
headless CLI workers (`codex exec` by default), keeps a curated checkpoint feed
per node, and keeps a clickable map of the whole tree posted in the channel.

## Core architecture: two-tier cron, nothing long-lived

Canopy has **no daemon**. All state lives on disk. A cron job fires every N
minutes and runs a cheap gate; only when there is real work does it spend tokens
on a worker. This is deliberate — a cold-start-from-disk worker is crash-safe
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
      → has mention:  spawn FULL agent worker  (runner, profile + node state)
      → no mention:   spawn LIGHT summarizer   (runner, small prompt, feed only)
```

The gate matters: **nodes with no new messages never touch an LLM.** Idle trees
cost almost nothing. A node that only saw chatter (no `@agent`) gets the cheap
summarizer, not the full agent. Every worker, on wake, does its job, advances
`cursor`, releases its lock, and exits.

**Lock is mandatory.** Each node has a `lock` file (holds pid + start ts). If a
worker overruns one tick, the next tick sees the lock and skips that node this
round — new messages just wait and get picked up next tick (accepted: no queue,
cursor re-pull covers it). Guard against dead locks with a staleness timeout.

## The runner — which CLI actually runs a worker

Every worker (full agent, light summarizer, `recalibrate`) is one headless run of
a coding CLI. Which CLI is a config value, not something baked into the scripts:

```jsonc
// $CANOPY_DATA_HOME/config.json
{ "runner": "codex" }        // default
{ "runner": "claude" }
{ "runner": { "cmd": ["my-wrapper", "--flag"] } }   // escape hatch
```

**Default is `codex`.** Both supported runners read the prompt from stdin, print
to stdout, and exit — that is all Canopy needs from them, so the two paths differ
only in the argv they build:

```
codex   codex exec --skip-git-repo-check --ephemeral \
          --dangerously-bypass-approvals-and-sandbox \
          -C <node dir> \
          -o <node dir>/last-message.txt \
          -                                 # `-` == read the prompt from stdin

claude  claude -p --output-format text \
          --dangerously-skip-permissions    # no prompt arg == read from stdin
                                            # cwd is the node dir
```

**Workers run with no sandbox and no approval prompts.** Deliberate: a worker
woken by cron has no TTY, so any approval prompt is a hang, and a sandbox that
blocks network or writes outside the node dir turns a woken worker into one that
can neither post to Slack nor advance its own `cursor` — it fails silently and
the tick log says nothing useful. Full access is the cost of running unattended.

What that means in practice, so nobody discovers it by accident: **the model gets
the same reach over this machine as the person who installed Canopy**, and it
gets it on a timer, triggered by whatever someone typed in a Slack thread. Treat
a tracked thread as an input channel into your shell. Two guardrails that are
worth keeping: the tick only wakes a full worker on an explicit `@agent` mention
(chatter gets the summarizer, which has no such reach), and profiles say what an
agent is for. Neither is a security boundary. If you need one, put the runner
behind your own `cmd` wrapper — a container, a separate user account, a remote
box — which is exactly what the `cmd` escape hatch is for.

The rest of the codex flags:

- `--skip-git-repo-check` — `$CANOPY_DATA_HOME` is not a git repo.
- `--ephemeral` — no session files; the node directory on disk is already the
  source of truth, and per-tick session rollouts would pile up forever.
- `-C <node dir>` — the worker's working root is the node it was woken for.
- `-o <file>` — the worker's last message, read back by the tick for logging.

A custom `cmd` gets the prompt on stdin too. Anything else — API keys in argv,
a runner that needs an interactive TTY — is out of scope; the whole point is that
cron can start it with no terminal attached.

**Resolve the runner to an absolute path.** cron runs with a minimal `PATH` and
no shell profile, so a version-manager install (mise, nvm, asdf — `codex` usually
lands in one) is invisible to it. `track` resolves the binary once
(`command -v <runner>`) and stores the absolute path in `config.json`. If it
can't resolve, `track` refuses to register the cron job — better than a tree that
looks watched and silently never ticks.

`runner` is global in `config.json`. It is not per node: a tree where 1.a thinks
with a different model than 1.b is a debugging problem nobody wants at 3am.
Model choice inside a runner is that runner's own config (`~/.codex/config.toml`,
`CLAUDE_*` env), which Canopy does not manage.

## Code / data separation — never write into the skill

The skill is a git repo that gets installed by many people and PR'd back.
**Runtime must never write a byte into the skill root**, or it can't be updated
or contributed back. All read/write goes to a per-user data home.

```
skill-root/                      # git, READ-ONLY at runtime, PR-able
  SKILL.md
  scripts/                       # cron-tick, runner wrapper, reply tool, summarizer, entrypoints
  templates/
    profiles/*.md                # seed profiles — canopy.md is the default agent
    messages/<locale>/*.md       # seed Slack message templates, per language (zh, en)
    default-summarizer.md        # default feed prompt

$CANOPY_DATA_HOME  (default ~/.canopy/)   # per-user, NOT in the repo
  config.json                    # cron interval, slack token ref, data path,
                                 #   locale, default_agent, runner
  profiles/<agent>.md            # the user's real, editable global agents
  messages/<locale>/*.md         # the user's real, editable message templates
  projects/<projId>/             # one per `track`
    tree.json
    messages/*.md                # optional per-project overrides
    nodes/<channel>-<thread_ts>/
      state.json
      guide.md                   # summarizer guidance, append-only
      transcript.jsonl           # raw messages, incrementally persisted
      lock                       # present == in use (pid + start ts)
```

Scripts resolve data via `$CANOPY_DATA_HOME` (default `~/.canopy/`), overridable
per-project with `--data-dir` (e.g. to sync/back up via a git-ignored folder).
On first `track`/`agents`/`messages`, if `profiles/` or `messages/` is empty,
**copy the seeds** from `templates/profiles/` and `templates/messages/` into the
data home. From then on the user edits their copy; skill updates refresh the
templates without clobbering the user's profiles or wording.

Profiles are **global** (shared across all projects). Reply identity is chosen
**per node** (`reply_as`), so 1.a can answer as `@arch` while 1.b answers as
`@qa`, all drawing from the one profile pool.

One profile ships: **`canopy.md`, the default agent.** A node with no `reply_as`
replies as whatever `config.json`'s `default_agent` names (`canopy` out of the
box), so `@canopy fork …` works in the first thread you track, before the user
has written a single profile. Without a shipped default, `track` posts an
announce telling people to `@<agent>` and there is no agent to `@` — the whole
in-thread command set would be unreachable until someone ran `agents`. The file
name is the handle (`profiles/arch.md` ⇒ `@arch`), so users add their own by
dropping files next to it.

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
`track`/`fork` time, linking the raw thread permalink and the tree map. `feed_ts`
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

## Message templates — every byte Canopy posts to Slack

Observers never read `tree.json`; they read the messages
Canopy posts. Those messages are the product, so **none of their wording is
hardcoded**. Every posting moment renders a template, and every template is
user-editable and PR-able.

| Moment | Template | Posted where |
|---|---|---|
| `track` | `feed-root.md` | channel — root checkpoint message |
| …then | `track-announce.md` | the **raw thread** — tells the people already arguing there that it is now watched, and where the feed lives |
| `fork` | `fork-thread.md` | channel — the **new top-level message** that opens the child thread people will actually argue in |
| …then | `feed-fork.md` | channel — child checkpoint message |
| …then | `fork-announce.md` | the **parent thread** — points everyone at the new thread so the sub-problem does not silently vanish |
| summarizer appends a checkpoint | `feed-entry.md` | one entry inside the live segment |
| active segment fills | `feed-segment.md` | header of the new segment |
| …and the old one is sealed | `feed-sealed-footer.md` | pointer stamped onto the sealed segment |
| worker replies in-thread (Loop A) | `reply.md` | the node's thread |
| `return` | `return-draft.md` | new message, for A君's review only |
| `ack return` | `return-post.md` | the **parent** thread |
| `done` / `pause` / `untrack` | `status-change.md` | the node's feed |

`guide:` gets an emoji reaction, not a message — steering the summarizer should
not add noise to the thread everyone is reading.

Each template file is **front matter + body**: `moment` names the posting moment,
`vars` declares exactly which variables that moment provides, and the body is
Slack mrkdwn. Rendering is variable substitution only — no logic, no arithmetic
(hence `prev_segment_index` is passed in rather than computed).

A body referencing a variable not in its `vars` is a **hard error at render
time**, and the post is abandoned: better a failed tick in your log than
`{{parent_permalink}}` posted verbatim into the channel your VP is reading. A
declared variable that is legitimately empty (`reason`, or `entries` on a fresh
feed) renders as nothing.

### Wording style

These messages land in a channel people are already busy in, so they read like a
colleague, not a status system. **Lead with a verb, cut the nominalizations**:
"Pin this message" over "Pinning is recommended"; "Split off `1.a`" over
"A sub-problem has been created". Keep each message to a couple of lines — the
checkpoint entries carry the content, the frame around them should disappear.

### Locale

Templates ship per language under `messages/<locale>/`, currently `zh` and `en`.
`config.json` sets `"locale"` (**default `zh`**), overridable per project at
`track` time with `--locale`, because one person often tracks a Chinese product
thread and an English infra thread from the same machine.

Only the *frame* is localized. Checkpoint summaries come from the summarizer, so
they follow whatever language the thread is speaking — which is exactly why the
frame has to be switchable, otherwise every feed reads half-and-half.

### Resolution

Layered, first hit wins:

```
projects/<projId>/messages/<name>             # this project only (rare, e.g. a formal exec-facing tree)
$CANOPY_DATA_HOME/messages/<locale>/<name>    # the user's edits   (the normal place to customize)
skill-root/templates/messages/<locale>/<name> # shipped default    (read-only)
```

A project-level override wins regardless of locale — if you hand-wrote that
message for that tree, you meant it.

Same rule as profiles: seeds are **copied out on first `track`** (only the
locales you actually use), so a skill update refreshes the shipped defaults
without clobbering your wording.

## Commands

### How to name a node (`<node>` in every command below)

The canonical node id is `<channel>-<thread_ts>` — correct, stable, and
**unusable by hand**. So every command that takes a `<node>` resolves it from
any of these, in order:

1. **Path alias** — `1`, `1.a`, `1.a.ii`, derived from each node's position
   among its siblings in `tree.json`. This is the primary human handle and the
   one `tree` prints. Aliases are *positional, not stored*: recomputed on every
   render.
2. **Unique title substring** — `tree 慢查询`.
3. **Full or prefix node id** — `C0PAY-1699.0042`, for scripts and logs.

A path alias is only meaningful **inside one project**, but the CLI is global —
you normally have a few roots tracked at once. So a node ref may be qualified
`<projId>:<alias>` (`pay-timeout:1.a`), and a bare alias resolves only if it hits
exactly one node across all tracked projects.

Ambiguous → refuse and print the candidates, qualified. Never guess which node
the user meant; acting on the wrong node silently corrupts the tree.

Path aliases are stable enough for a work session but **shift if a sibling is
inserted**, so anything durable (cron args, `tree.json`, logs) stores node ids.

### Local CLI — `/canopy <cmd>` (A君 → the agent)

- `track <slackThreadLink> [--locale <l>]` — **main entrypoint.** Create the root
  node + `tree.json`, post the root checkpoint message (linking raw thread +
  tree map), **announce into the raw thread itself** so the people already
  discussing there learn it is watched and where to follow, register the cron
  job, post the tree map.

  The projId is not a slug of the title: `track` asks the runner for a short
  semantic id (`figma-free-design`) because that id gets typed in every later
  command, and falls back to the mechanical slug when the call fails. One small
  model call, once per tree.

  The announce is not optional politeness: without it, a feed exists that the
  actual participants never hear about, and A君 ends up pasting the link by hand
  to everyone. It also doubles as the in-thread hint for `fork` / `guide:`, which
  is how anyone but A君 discovers those commands exist.
- `agents` — enter profile-edit mode: create / edit / delete global
  `profiles/*.md`. Optional — `canopy` is already there. Deleting the profile
  named by `default_agent` would leave every node without a `reply_as` with
  nobody to answer as, so point `default_agent` at another profile first.
- `messages` — review and edit the message templates above. No arg → list every
  template with **which layer it resolved from** (shipped / user / project), so
  you can see at a glance what you have already customized. `messages <name>`
  opens that one for editing. `messages <name> --preview [<node>]` renders it
  against a real node's state (or a seed fixture when no node is given) and
  prints the exact text Slack would receive — **it posts nothing**. Preview
  before `track`, not after your VP has read it.
- `tree` / `status` — print status / owner / lock state. Two **orthogonal**
  parameters: the argument picks *where to start*, `--depth` picks *how far
  down*. A project has exactly one root, so **projId is just the human name for
  that root**.

  Where to start:
  - no arg → every tracked root
  - projId → that root
  - node ref → that node, plus a one-line breadcrumb of its ancestors so you
    never lose your place

  How deep — `--depth`, counted from wherever you started:
  - `0` → starting node(s) only, each as a single rollup line: title, its
    projId/alias, and counts of `active` / `paused` / `done` descendants plus how
    many currently hold a lock. **Default for the no-arg form** — a handful of
    roots, nothing expanded: the daily dashboard.
  - `N` → expand N levels; anything deeper collapses into a rollup on its
    deepest visible ancestor.
  - `all` → no cap. **Default once you name a projId or node** — you already
    zoomed in, so expansion is what you asked for.

  A collapsed line always carries its rollup counts, so a truncated branch is
  visibly truncated and you can re-run one level deeper. Deep trees are the
  normal case; the depth cap is what stops Canopy from re-creating the drowning
  problem it exists to solve.
- `pause <node>` / `resume <node>` — stop / restart watching a node.
- `recalibrate <node>` — CLI form of Loop C.
- `map` — refresh the tree message(s) and print their links.
- `untrack <node>` — archive the node, stop watching, grey it in the tree map.

### In-thread — `@<agent> <cmd>` (posted in Slack)

- `fork <title>` — open a sub-problem. The **current thread_ts becomes parent**;
  add the edge to `tree.json`, post a new root checkpoint message in the same
  channel (linking the parent message + tree map), and reply in the parent thread
  pointing at the new thread. The edge is written at fork time — never inferred
  later.
- `return` — draft a summary to a **new message** for A君 to review before it
  goes up.
- `ack return` — confirm; post the summary back into the **parent thread** as
  `[$agentName]: …`, linking the child's feed permalink.
- `guide: <text>` — append to this node's `guide.md`; effective next tick.
- `recalibrate` — rebuild this node's feed (Loop C).
- `done` — mark the node complete; tick it in the tree map (pairs with `return`).

## State schemas

### tree.json (per project)
```json
{
  "root": "C123-1699.0001",
  "tree_msgs": [{"index": 1, "channel": "C123", "ts": "1699.0002",
                 "root": "C123-1699.0001"}],
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
  "tree_permalink": "https://.../p1699000002",
  "reply_as": "arch"
}
```
`status`: `active` | `paused` | `done`. `feed_ts`: sealed segments first, last
entry is the live one. `reply_as`: reply identity for this node — omit it and the
node replies as `canopy`, the shipped default agent.

## The tree map — the whole tree, as messages

A君's navigation surface is a message in the same channel, updated in place
whenever structure or status changes: every node as a row with its alias, title,
status mark, owner, and links to its raw thread and its feed. `done` ticked,
`paused`/`untracked` marked.

**Not a Slack Canvas.** `slackcli` can read canvases but not write one, and a
Canvas link that only opens on the machine that rendered it is worse than no
link — so the map is an ordinary message anyone in the channel can click.

**Segmented every 4 levels.** One message can't hold a deep tree. A node at the
cut line stops being a row and becomes a pointer into the message that continues
from it; that message links back to the one above. The tree stays walkable by
clicking, which is the only reason to have a map at all. `tree_msgs` in
`tree.json` records each segment's ts, and `tree_permalink` on a node points at
the segment its row actually lives in — not always segment 1.

## Implementation

`scripts/` is the runtime: Python 3, stdlib only, `python3 -m pytest` for the
tests. `scripts/README.md` maps module to responsibility. Two rules there are
design decisions, not implementation details:

- **Structural commands never reach the model.** `fork`, `done`, `guide:`,
  `return`, `ack return` are parsed from the message and executed as code. A
  fork writes an edge into `tree.json`; a hallucinated edge is a corrupted tree
  nobody notices for a week. The model is asked for replies and summaries only.
- **The gate is testable and tested.** The tick's zero-LLM path is asserted by
  handing it a worker that raises if it is ever called, so "idle trees are free"
  can't silently regress into "idle trees cost a summarizer each".

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
