# canopy templates

Seed profiles, Slack message templates, the default summarizer prompt, and the
Canvas template live here. Copied into $CANOPY_DATA_HOME on first use; never
written to at runtime.

- `profiles/` — seed agent profiles
- `messages/` — every message Canopy posts to Slack, one file per moment.
  Front matter declares which variables that moment provides; the body is what
  gets posted (Slack mrkdwn). Preview any of them with
  `/canopy messages <name> --preview` before they reach a channel.
