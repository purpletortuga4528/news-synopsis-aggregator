"""Test package.

Puts the repository root on sys.path so `boolean_search` and `summarizer`
import the same way under `python -m unittest discover` and under pytest,
regardless of which directory the runner was started from.
"""
import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
