"""Homebrew formula + cask adapters."""
from __future__ import annotations

from typing import Sequence, Set

from .base import Adapter, InstallResult


class BrewAdapter(Adapter):
    name = "brew"

    def list_installed(self) -> Set[str]:
        r = self._run(["brew", "list", "--formula", "-1"])
        if not r.ok:
            return set()
        # Brew can return "tap/formula" style; strip tap prefix.
        out = set()
        for line in r.stdout.splitlines():
            name = line.strip()
            if not name:
                continue
            if "/" in name:
                name = name.split("/")[-1]
            out.add(name)
        return out

    def install(self, items: Sequence[str]) -> InstallResult:
        if not items:
            return InstallResult(ok=True, cmd="(no items)")
        return self._run(["brew", "install", *items], timeout=900.0)

    def uninstall(self, items: Sequence[str]) -> InstallResult:
        if not items:
            return InstallResult(ok=True, cmd="(no items)")
        return self._run(["brew", "uninstall", "--ignore-dependencies", *items], timeout=300.0)


class CaskAdapter(Adapter):
    name = "cask"

    def list_installed(self) -> Set[str]:
        r = self._run(["brew", "list", "--cask", "-1"])
        if not r.ok:
            return set()
        return {line.strip() for line in r.stdout.splitlines() if line.strip()}

    def install(self, items: Sequence[str]) -> InstallResult:
        if not items:
            return InstallResult(ok=True, cmd="(no items)")
        return self._run(["brew", "install", "--cask", *items], timeout=1800.0)

    def uninstall(self, items: Sequence[str]) -> InstallResult:
        if not items:
            return InstallResult(ok=True, cmd="(no items)")
        return self._run(["brew", "uninstall", "--cask", *items], timeout=600.0)
