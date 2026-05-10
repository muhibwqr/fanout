"""Adapter registry — each package manager wrapped behind a uniform interface."""
from __future__ import annotations

from typing import Dict

from .base import Adapter, InstallResult
from .brew import BrewAdapter, CaskAdapter
from .curl_script import CurlAdapter
from .npm import NpmAdapter
from .pip import PipAdapter


def build_registry() -> Dict[str, Adapter]:
    """Return a fresh adapter registry keyed by bucket name."""
    return {
        "brew": BrewAdapter(),
        "cask": CaskAdapter(),
        "npm_global": NpmAdapter(),
        "pip": PipAdapter(),
        "curl": CurlAdapter(),
    }


__all__ = [
    "Adapter",
    "BrewAdapter",
    "CaskAdapter",
    "CurlAdapter",
    "InstallResult",
    "NpmAdapter",
    "PipAdapter",
    "build_registry",
]
