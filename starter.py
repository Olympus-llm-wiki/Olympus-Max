#!/usr/bin/env python3
"""First-run setup and explicit read-only connection diagnosis."""
import sys
from pathlib import Path

if sys.version_info < (3, 12):
    raise SystemExit("Olympus Max requires Python 3.12 or newer.")
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from olympus.starter import setup_main

if __name__ == "__main__":
    raise SystemExit(setup_main())
