# canopy templates

Seed profiles, Slack message templates, the default summarizer prompt, and the
Canvas template live here. Copied into $CANOPY_DATA_HOME on first use; never
written to at runtime.

- `profiles/` — seed agent profiles
- `messages/<locale>/` — every message Canopy posts to Slack, one file per
  moment, one directory per language (`en`, `zh`). Front matter declares which
  variables that moment provides; the body is what gets posted (Slack mrkdwn).

Adding a locale means copying a directory and translating the bodies — keep the
`moment` and `vars` front matter identical across languages, or `--preview` will
pass in one language and fail in another.

Lead with a verb and keep it to a couple of lines. These land in a channel people
are already working in; the checkpoint entries carry the content, the frame
around them should get out of the way.

Preview before anything reaches a channel: `/canopy messages <name> --preview`.
