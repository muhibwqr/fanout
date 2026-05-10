"""npm global package adapter."""
from __future__ import annotations

import json
from typing import Sequence, Set

from .base import Adapter, InstallResult


class NpmAdapter(Adapter):
    name = "npm_global"

    def list_installed(self) -> Set[str]:
        r = self._run(["npm", "list", "-g", "--depth=0", "--json"])
        # npm exits non-zero on extraneous warnings but still returns valid JSON; tolerate.
        if not r.stdout:
            return set()
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            return set()
        deps = data.get("dependencies") or {}
        return set(deps.keys())

    def install(self, items: Sequence[str]) -> InstallResult:
        if not items:
            return InstallResult(ok=True, cmd="(no items)")
        return self._run(["npm", "install", "-g", *items], timeout=900.0)

    def uninstall(self, items: Sequence[str]) -> InstallResult:
        if not items:
            return InstallResult(ok=True, cmd="(no items)")
        return self._run(["npm", "uninstall", "-g", *items], timeout=300.0)
