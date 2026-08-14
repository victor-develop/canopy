# canopy

Turn a sprawling Slack discussion into a navigable **tree of sub-problems**, each
with its own curated **checkpoint feed** — a Claude skill that runs off-line via
`cron` + `claude -p`, with no long-lived process.

## The problem

Real work in Slack forks like a tree. Someone discusses problem 1 in a thread;
mid-way a side-problem 1.a appears, gets its own owner and its own sub-thread,
pulls in different people, and its conclusion feeds back up. This nests
arbitrarily (1.a.i, 1.a.ii…), but Slack only gives you a channel plus one level
of thread. Three pains follow:

1. **No infinite nesting** — the tree is real, Slack can't represent it.
2. **Observers drown** — a stakeholder wants *key progress*, not the raw stream.
3. **The backstop owner can't navigate** — no fast interface across the whole tree.

Canopy maintains the tree off to the side, watches each active node for new
messages, dispatches `@agent` mentions to `claude -p` workers, keeps a curated
checkpoint feed per node, and renders a clickable Canvas of the whole tree.

## How it works (one paragraph)

No daemon. All state lives on disk under `$CANOPY_DATA_HOME` (default `~/.canopy/`).
A cron job fires every N minutes and runs a **cheap, zero-LLM gate**: for each
active node it asks Slack for the latest ts and skips the node unless there are
new messages. Only when there is real work does it spend tokens — a **full agent
worker** if a message `@`-mentions an agent, otherwise a **light summarizer** that
only updates the feed. Workers cold-start from disk, do their job, advance the
cursor, release a per-node lock, and exit. Crash-safe by construction.

See [`SKILL.md`](./SKILL.md) for the full design: the three loops, the two-tier
cron, code/data separation, the command set, and the state schemas.

## Commands (preview)

Local CLI (`/canopy …`): `track`, `agents`, `tree`/`status`, `pause`/`resume`,
`recalibrate`, `canvas`, `untrack`.

In-thread (`@<agent> …`): `fork`, `return`, `ack return`, `guide:`,
`recalibrate`, `done`.

## The journey — one problem, tracked end to end

A `#pay` thread about payment timeouts, from "this thread is getting long" to
"the whole tree is closed". Everything on the left runs in A君's terminal;
everything on the right is a message someone posts in Slack.

### 0 · once per machine

```
$ /canopy agents                     write profiles/arch.md, profiles/qa.md
$ /canopy messages                   list templates + which layer each came from
                                       feed-root.md        user   (edited)
                                       track-announce.md   shipped
                                       ...
$ /canopy messages feed-root --preview
                                     renders the exact Slack text. Posts nothing.
```

### 1 · `track` — adopt a live thread

```
$ /canopy track https://…/archives/C0PAY/p1699000001 --locale zh

  #pay ────────────────────────────────────────────────────────────────
   🧵 1699.0001  “支付超时”            ← the raw argument, left untouched
      └ [arch] 我开始盯这条 thread 了…            track-announce.md
   📌 1699.0002  🌳 支付超时 · `1`                feed-root.md
   🗂  Canvas “pay-timeout”                       canvas.tmpl
  ──────────────────────────────────────────────────────────────────────
   + cron job registered      + ~/.canopy/projects/pay-timeout/tree.json
```

The announce is the point: the people already arguing in that thread learn it
is watched, where the feed lives, and that `fork` / `guide:` exist.

### 2 · every N minutes — the tick nobody sees

```
cron ──► for each active node
           │
           ├ latest_ts <= cursor ? ──yes──► skip                    0 tokens
           ├ lock file present ?   ──yes──► skip, retry next tick
           │
           └ new messages mention @agent ?
                ├ no  ──► light summarizer ──► maybe append feed-entry.md
                │                              into the live feed segment
                └ yes ──► full worker      ──► reply.md in the thread
                                               advance cursor, drop lock
```

### 3 · `guide:` — steer what gets recorded

```
🧵 1  @arch guide: 只记 DB 侧结论，排期讨论跳过
      → appended to the node's guide.md, effective next tick
      → ✅ reaction only — no message, the thread stays readable
```

### 4 · `fork` — a side-problem gets its own owner

```
🧵 1  @arch fork 慢查询定位

  #pay ────────────────────────────────────────────────────────────────
   🧵 1699.0001  “支付超时”
      └ [arch] 拆出 `1.a` — 慢查询定位 …           fork-announce.md
   🧵 1701.0500  “慢查询定位”         ← new thread, E/F pulled in here
   📌 1701.0501  🌳 慢查询定位 · `1.a`             feed-fork.md
  ──────────────────────────────────────────────────────────────────────
   tree.json: 1 ──► 1.a          the edge is written now, never inferred later
```

Fork again inside `1.a` and you get `1.a.i` — the nesting Slack can't hold.

### 5 · `tree` — navigate at whatever zoom you need

```
$ /canopy tree                       # no arg → every root, depth 0
  pay-timeout  支付超时     active   4 active / 1 paused / 2 done   🔒1

$ /canopy tree pay-timeout           # named a root → depth all
  1        支付超时         active   @arch
  ├ 1.a    慢查询定位       active   @arch
  │ └ 1.a.i  索引方案       active   @qa     🔒 worker running
  └ 1.b    重试风暴         paused

$ /canopy tree 1.a --depth 1         # start anywhere, cap the depth
  ↑ pay-timeout / 1                  # breadcrumb, so you keep your place
  1.a      慢查询定位       active   @arch
  └ 1.a.i  索引方案         active   @qa     ▸ 2 done

$ /canopy pause 1.b                  # stop watching, keep the feed
$ /canopy resume 1.b
$ /canopy canvas                     # re-render, print the link
```

### 6 · `return` / `ack return` / `done` — feed the answer back up

```
🧵 1.a  @arch return           draft posted as a NEW message — A君 only
        @arch ack return   ──► posted into 🧵 1               return-post.md
        @arch done         ──► status-change.md in 1.a's feed, ✔ in the Canvas
```

Nothing goes up to the parent thread until A君 has read it.

### 7 · when the feed drifts — `recalibrate`

```
$ /canopy recalibrate 1        (or in-thread: @arch recalibrate)
   reads the WHOLE history in chunks → rebuilds every feed segment
   heavy escape hatch; the per-tick segment update is the cheap common path
```

### 8 · `untrack` — close the tree

```
$ /canopy untrack 1            archive, unregister cron, grey it in the Canvas
```

## Status

Design frozen; `SKILL.md` is the source of truth. `scripts/` and `templates/`
are scaffolded and being filled in.

## License

MIT — see [`LICENSE`](./LICENSE).
