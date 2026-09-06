#!/usr/bin/env python3
"""Checkout entry point with a separate starter identity and local state."""
import sys
from pathlib import Path

if sys.version_info < (3, 12):
    raise SystemExit("Olympus Max requires Python 3.12 or newer.")
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from olympus.starter import main

if __name__ == "__main__":
    raise SystemExit(main())
