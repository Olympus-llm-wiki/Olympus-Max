#!/usr/bin/env python3
"""Run distribution tests with the internal package ahead of Lite's olympus.py."""
from pathlib import Path
import sys
import os
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
os.environ['PYTHONPATH']=str(ROOT/'src')+os.pathsep+str(ROOT)
result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.discover(str(ROOT/'tests')))
raise SystemExit(not result.wasSuccessful())
