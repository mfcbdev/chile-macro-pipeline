"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

# Make function modules importable in tests
ROOT = Path(__file__).resolve().parents[1]
for pkg in ("functions/ingest", "functions/transform_load", "scripts"):
    sys.path.insert(0, str(ROOT / pkg))
