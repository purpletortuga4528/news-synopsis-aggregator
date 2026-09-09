"""Test package.

Puts the repository root on sys.path so `boolean_search`, `summarizer` and the
rest import the same way under `python -m unittest discover` and under pytest,
regardless of which directory the runner was started from.

Also redirects the database away from the real sites.db. app.py calls init_db()
at import time, so this has to happen before any test module imports app. Doing
it here, in the package __init__, is the only placement that does not depend on
test discovery order.
"""
import sys
import tempfile
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import db  # noqa: E402  (must follow the sys.path insertion above)

TEST_DB_PATH = Path(tempfile.gettempdir()) / "news_synopsis_tests.db"
db.DB_PATH = TEST_DB_PATH
