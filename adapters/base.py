"""Adapter base class + result type."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Set


@dataclass
class InstallResult:
    ok: bool
    cmd: str
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


class Adapter:
    """Subclass per package manager."""

    name: str = "base"

    def list_installed(self) -> Set[str]:
        raise NotImplementedError

    def install(self, items: Sequence[str]) -> InstallResult:
        """Install one or more items in one shell invocation when possible."""
        raise NotImplementedError

    def uninstall(self, items: Sequence[str]) -> InstallResult:
        raise NotImplementedError

    def check(self, item: str) -> bool:
        """Optional: confirm the install actually works (smoke command)."""
        return True

    # Helpers ----------------------------------------------------------------

    @staticmethod
    def _run(cmd: List[str], *, timeout: float = 300.0) -> InstallResult:
        joined = " ".join(cmd)
        try:
            cp = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except FileNotFoundError as e:
            return InstallResult(
                ok=False, cmd=joined, stderr=str(e), returncode=127
            )
        except subprocess.TimeoutExpired as e:
            return InstallResult(
                ok=False,
                cmd=joined,
                stderr=f"timeout after {timeout}s",
                returncode=124,
            )
        return InstallResult(
            ok=cp.returncode == 0,
            cmd=joined,
            stdout=cp.stdout or "",
            stderr=cp.stderr or "",
            returncode=cp.returncode,
        )
