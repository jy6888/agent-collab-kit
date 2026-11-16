#!/usr/bin/env python3
"""Stable CLI/hook entrypoint for Planning with Files v4."""

import sys


for stream in (sys.stdin, sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure:
        reconfigure(encoding="utf-8", errors="replace")

from planning.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
