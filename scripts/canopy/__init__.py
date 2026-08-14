"""Canopy runtime: cron tick, workers, and the local CLI.

Stdlib only, on purpose — the tick runs from cron, and a missing dependency
there is a tree that silently stops being watched.
"""

__all__ = ["cli"]
