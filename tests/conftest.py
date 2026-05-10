"""Shared pytest fixtures for the fanout v3 test suite."""
from __future__ import annotations

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

MANIFEST_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "manifests"
