"""pip user-install adapter (pip install --user)."""
from __future__ import annotations

import json
from typing import Sequence, Set

from .base import Adapter, InstallResult


class PipAdapter(Adapter):
    name = "pip"

    def list_installed(self) -> Set[str]:
        r = self._run(["python3", "-m", "pip", "list", "--user", "--format=json"])
        if not r.stdout:
            return set()
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            return set()
        return {pkg["name"].lower() for pkg in data if isinstance(pkg, dict) and "name" in pkg}

    def install(self, items: Sequence[str]) -> InstallResult:
        if not items:
            return InstallResult(ok=True, cmd="(no items)")
        return self._run(
            ["python3", "-m", "pip", "install", "--user", *items], timeout=900.0
        )

    def uninstall(self, items: Sequence[str]) -> InstallResult:
        if not items:
            return InstallResult(ok=True, cmd="(no items)")
        return self._run(
            ["python3", "-m", "pip", "uninstall", "-y", *items], timeout=300.0
        )
