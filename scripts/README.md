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
| `cli.py` | argv → command. `track`, `agents`, `messages`, `tree`/`status`, `pause`/`resume`, `recalibrate`, `map`, `untrack`, plus `tick`, `reply`, `config` |
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
| `shortid.py` | asking the runner for the project's short semantic id |
| `store.py` | `tree.json` / `state.json`, atomic writes |
| `locks.py` | one lock file per node, staleness and dead-pid handling |
| `prompts.py` | what a worker is actually told |
| `mentions.py` | reading `@agent <cmd>` out of a Slack message |
| `cron.py` | one crontab line, installed and removed cleanly |
| `config.py`, `paths.py`, `errors.py` | config defaults, data-home layout, error types |

## Two things worth knowing before changing this

**Structural commands never reach the model.** `fork`, `done`, `guide:`,
`return`, `ack return` are parsed in `mentions.py` and executed in `ops.py`. A
fork writes an edge into `tree.json`, and a hallucinated edge is a corrupted
tree nobody notices for a week. The model is only asked for replies and
summaries.

**The gate runs before anything costs money.** `tick.gate()` asks Slack for the
latest ts and returns `no-new` without touching an LLM; the tests assert this by
passing a handler that raises if it is ever called.

## Tests

`python3 -m pytest` — 150-odd tests, no network, no Slack, no model. The fake
Slack in `tests/conftest.py` records every post, edit, and reaction, so tests
assert on the exact text that would have hit the channel. An autouse fixture
blocks the real runner, so a test that forgets to inject a fake fails loudly
instead of quietly shelling out to codex.
