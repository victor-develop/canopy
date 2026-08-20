"""`config.json`: the handful of knobs that are not per node."""

from . import paths
from .store import read_json, write_json

DEFAULTS = {
    "cron_interval_minutes": 5,
    # Who wakes the tick. "cron" installs and removes a crontab line as the
    # tree changes; "none" hands that job to something outside canopy (see
    # `canopy loop`) and makes `schedule.sync` a no-op, so an external
    # scheduler's own ticks cannot quietly reinstall the crontab line.
    "schedule_backend": "cron",
    "locale": "zh",
    "default_agent": "canopy",
    "runner": "codex",
    # Filled in by `track`: cron's PATH does not include mise/nvm/asdf shims.
    "runner_path": None,
    # Also filled in by `track`, from $SHELL. The tick runs inside this shell as
    # a login shell so it inherits the profile your runner's auth lives in — see
    # cron.line. Set it to null if your runner needs nothing from a profile.
    "cron_login_shell": None,
    # Same reason as runner_path: cron's PATH has neither of these.
    "slack_cli": "slackcli",
    # Upstream slackcli through v0.9.0 escapes `<url|label>` on edit (it omits
    # parse=none on chat.update; fixed upstream in #120, unreleased as of
    # v0.9.0). Set false once your slackcli carries that fix.
    "slack_cli_escapes_on_edit": True,
    "slack_cli_path": None,
    "slack_workspace_url": None,
    "slack_backend": "slackcli",   # or "api", which needs $slack_token_env
    "slack_token_env": "CANOPY_SLACK_TOKEN",
    "runner_timeout_seconds": 900,
    "shortid_timeout_seconds": 90,
    "serve_port": 8787,
    "serve_idle_timeout": 1800,
    "serve_start_timeout": 3,
    "stale_days": 3,
    "lock_stale_seconds": 1800,
    "feed_segment_max_chars": 3500,
}


def load(dh):
    cfg = dict(DEFAULTS)
    on_disk = read_json(paths.config_path(dh), default={}) or {}
    cfg.update(on_disk)
    return cfg


def save(dh, cfg):
    return write_json(paths.config_path(dh), cfg)


def set_values(dh, **values):
    cfg = load(dh)
    cfg.update(values)
    save(dh, cfg)
    return cfg


def permalink(cfg, channel, ts):
    """Slack permalink, built locally — slackcli has no permalink command.

    Falls back to a `slack://`-less relative form when the workspace URL is not
    configured yet, so a missing setting degrades a link instead of crashing a
    tick mid-post.
    """
    compact = "p" + str(ts).replace(".", "")
    base = (cfg.get("slack_workspace_url") or "").rstrip("/")
    if not base:
        return "/archives/%s/%s" % (channel, compact)
    return "%s/archives/%s/%s" % (base, channel, compact)
