"""Test package marker for the e2e suite.

``tests/integration/test_cli_system.py`` and ``tests/e2e/test_cli_system.py``
must share a basename, which collides under pytest's default ``prepend`` import
mode unless one directory is a package. Making this directory a package renames
its modules to ``e2e.*``; re-adding the directory to ``sys.path`` keeps the
suite's existing bare sibling imports (``journal_support``, ``position_support``)
working exactly as before.
"""

from __future__ import annotations

import sys
from pathlib import Path

_DIRECTORY = str(Path(__file__).resolve().parent)
if _DIRECTORY not in sys.path:
    sys.path.insert(0, _DIRECTORY)
