# canopy scripts

Runtime scripts live here (cron tick, runner wrapper, reply tool, summarizer
wrappers, entrypoints). See ../SKILL.md for the design. TODO: implement.

The runner wrapper is the only place that knows how to spawn a worker CLI. It
reads `runner` from `config.json` (`codex` by default, `claude`, or a custom
`cmd`), feeds the prompt on stdin, and returns the worker's last message —
everything else calls it and stays runner-agnostic.
