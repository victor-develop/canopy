# canopy scripts

The runtime. Python 3 (3.9+), **stdlib only** — the tick runs from cron, and a
missing dependency there is a tree that silently stops being watched. `pytest`
is needed for the tests, nothing else.

```bash
scripts/canopy-cli tree              # or: python3 scripts/canopy_main.py tree
python3 -m pytest                    # from the repo root
```

`canopy-cli` is a shim over `canopy_main.py`, which inserts `scripts/` on
`sys.path` and calls `canopy.cli.main`. cron gets the `canopy_main.py` path
directly, so ticking never depends on the repo being installed or on a shell
profile having run.

## Layout

| Module | What it owns |
|---|---|
| `cli.py` | argv → command. `track`, `agents`, `messages`, `tree`/`status`, `rename`, `recalibrate`, `map`, `untrack`, plus `tick`, `reply`, `config` |
| `ops.py` | every operation that changes a tree, shared by the CLI and the in-thread commands so both produce identically-shaped state |
| `tick.py` | the cron tick: the zero-LLM gate, then dispatch |
| `worker.py` | what a woken node gets: structural command, light summarizer, or full agent |
| `runner.py` | building and running the worker CLI (`codex` by default) |
| `feed.py` | checkpoint segments: append, seal, open the next, rebuild |
| `templates.py` | front matter + strict `{{var}}` rendering, and the layer resolution |
| `noderef.py` | path aliases (`1.a.ii`) and resolving any ref to one node |
| `treeview.py` | `tree` output: start point × `--depth`, rollups, breadcrumb |
| `slack.py` | the only place that talks to Slack, via `slackcli` as a subprocess |
| `treemap.py` | the tree message: rows, depth segmentation, which segment holds a node |
| `shortid.py` | asking the runner for the project's short id and headline, and the fallback shortener |
| `store.py` | `tree.json` / `state.json`, atomic writes |
| `locks.py` | one lock file per node, staleness and dead-pid handling |
| `prompts.py` | what a worker is actually told |
| `mentions.py` | reading `@agent <cmd>` out of a Slack message |
| `cron.py` | one crontab line, installed and removed cleanly |
| `webserve.py` | the ops page server: loopback, read-only, exits when idle |
| `opsview.py` | what that page shows, as data |
| `events.py` | append-only log of ticks and workers |
| `schedule.py` | the invariant: that line exists exactly when a node is active |
| `effects.py` | the one door for subprocesses, crontab and opening a URL |
| `config.py`, `paths.py`, `errors.py` | config defaults, data-home layout, error types |

## What four rounds of review found here

An independent architect reviewed this runtime four times. Every round found real
defects; the list below is what they had in common, because the same shapes will
show up in the next change too.

**Side effects with no seam.** The first two incidents were the same bug wearing
different clothes: the test suite installed a cron entry into a developer's real
crontab and left it there, and a later run scattered 59 detached HTTP servers
that outlived it. Both were first "fixed" by stubbing the symbol involved — which
only guards the hole somebody already fell into. `effects.py` exists so there is
one door: subprocess, spawn, crontab, open-a-URL. If you add a fifth kind of
effect, put it there, or a test will reach the machine.

**Fixing the example instead of the class.** Twice in a row: command execution
was made idempotent for `fork` while the reply path stayed re-entrant (the same
answer posted three times, three full workers paid for); the identity-prefix
guard was added to `reply` and `post_notice` while `fork-announce` and
`return-post` — which land in a *parent* thread, before that node's cursor — kept
posting unchecked. When you fix one call site, grep for the others.

**Fixes that introduce quieter bugs.** Catching `Exception` in the tick stopped a
crash and started swallowing the test guard rail. Holding the cursor on failure
stopped losing messages and started replaying a poison one forever. A global tick
lock stopped stacked ticks and made the ops page report "the tick crashed"
whenever a long `recalibrate` held the lock. Each of these was a correct fix with
a new failure mode attached; the second-order effect is the part to look for.

**A test double that cannot express the failure.** The most valuable change in
four rounds was three lines in `tests/conftest.py`: `FakeSlack.post` now appends
into `threads`, and its timestamps are monotonic. Before that, nothing Canopy
said could be read back, so "does Canopy recognise its own writing" — the thing
standing between it and an infinite self-reply loop — was untestable, and two
defects lived in that blind spot. If a class of bug keeps escaping, suspect the
double before the code.

## Two things worth knowing before changing this

**Structural commands never reach the model.** `fork`, `untrack`, `guide:`,
`return`, `ack return` are parsed in `mentions.py` and executed in `ops.py`. A
fork writes an edge into `tree.json`, and a hallucinated edge is a corrupted
tree nobody notices for a week. The model is only asked for replies and
summaries.

**The gate runs before anything costs money.** `tick.gate()` asks Slack for the
latest ts and returns `no-new` without touching an LLM; the tests assert this by
passing a handler that raises if it is ever called.

## Tests

`python3 -m pytest` — 231 tests, no network, no Slack, no model. The fake
Slack in `tests/conftest.py` records every post, edit, and reaction, so tests
assert on the exact text that would have hit the channel. An autouse fixture
installs `effects.Recording()`, which refuses to spawn and raises an
`EffectEscaped` (derived from `BaseException`, so the tick's own `except
Exception` cannot swallow the alarm).
