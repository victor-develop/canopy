#!/usr/bin/env python3
"""Entrypoint with no packaging: `python3 scripts/canopy_main.py <cmd>`.

cron gets this exact path, so it never depends on the repo being installed or
on a shell profile having run.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from canopy.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
