"""Curl-script adapter — for NVM, Oh-My-Zsh, Docker Compose, etc.

These tools are not in a package manager; each defines its own install shell
command. Idempotence comes from a `marker` file/dir: if the marker exists, the
tool is already installed.
"""
from __future__ import annotations

import os
import pathlib
from typing import List, Optional, Sequence, Set

from .base import Adapter, InstallResult

# The CurlAdapter doesn't list_installed independently — the engine asks it
# per-item via check_present(). It returns marker-detected items only.


class CurlAdapter(Adapter):
    name = "curl"

    def __init__(self) -> None:
        # Items the engine has registered with this adapter for the current run.
        self._registered: dict = {}

    def register(self, items) -> None:
        """Register CurlItem instances so list_installed knows what to check.

        `items` is iterable of CurlItem (from manifest.py).
        """
        for it in items:
            self._registered[it.name] = it

    def list_installed(self) -> Set[str]:
        out = set()
        for name, it in self._registered.items():
            if _marker_exists(it.marker):
                out.add(name)
        return out

    def install(self, items: Sequence[str]) -> InstallResult:
        """`items` here is a sequence of CurlItem dataclasses (not strings)."""
        if not items:
            return InstallResult(ok=True, cmd="(no items)")
        results: List[InstallResult] = []
        for it in items:
            if _marker_exists(it.marker):
                results.append(
                    InstallResult(ok=True, cmd=f"# {it.name}: already installed (marker present)")
                )
                continue
            r = self._run(["bash", "-c", it.install], timeout=900.0)
            results.append(r)
            if not r.ok:
                break
        ok = all(r.ok for r in results)
        return InstallResult(
            ok=ok,
            cmd="\n".join(r.cmd for r in results),
            stdout="\n".join(r.stdout for r in results),
            stderr="\n".join(r.stderr for r in results),
            returncode=0 if ok else 1,
        )

    def uninstall(self, items: Sequence[str]) -> InstallResult:
        if not items:
            return InstallResult(ok=True, cmd="(no items)")
        results: List[InstallResult] = []
        for it in items:
            uninst = getattr(it, "uninstall", None) or f"rm -rf {it.marker}"
            r = self._run(["bash", "-c", uninst], timeout=300.0)
            results.append(r)
        ok = all(r.ok for r in results)
        return InstallResult(
            ok=ok,
            cmd="\n".join(r.cmd for r in results),
            stdout="\n".join(r.stdout for r in results),
            stderr="\n".join(r.stderr for r in results),
            returncode=0 if ok else 1,
        )


def _marker_exists(marker: str) -> bool:
    p = pathlib.Path(os.path.expanduser(marker))
    return p.exists()
