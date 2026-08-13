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

## Status

Design frozen; `SKILL.md` is the source of truth. `scripts/` and `templates/`
are scaffolded and being filled in.

## License

MIT — see [`LICENSE`](./LICENSE).
