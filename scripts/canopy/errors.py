"""Every failure Canopy raises on purpose.

A tick that dies loudly in the cron log beats one that posts half a message
into the channel your VP is reading, so nothing here is caught-and-ignored by
default.
"""


class CanopyError(Exception):
    """Base for everything below."""


class ConfigError(CanopyError):
    pass


class RenderError(CanopyError):
    """A message template referenced a variable it never declared."""


class NodeRefError(CanopyError):
    """A `<node>` argument matched nothing."""


class AmbiguousRefError(NodeRefError):
    """A `<node>` argument matched more than one node.

    Carries the candidates so the CLI can print them qualified instead of
    guessing — acting on the wrong node silently corrupts the tree.
    """

    def __init__(self, ref, candidates):
        self.ref = ref
        self.candidates = list(candidates)
        joined = ", ".join(self.candidates)
        super().__init__("ambiguous node ref %r; candidates: %s" % (ref, joined))


class LockedError(CanopyError):
    """Another worker holds this node's lock."""


class RunnerError(CanopyError):
    pass


class SlackError(CanopyError):
    pass
