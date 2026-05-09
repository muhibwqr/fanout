"""Backend wrappers — currently `claude -p` only."""
from __future__ import annotations

import asyncio
import json
from typing import Optional


class BackendError(RuntimeError):
    """Non-zero exit from a backend invocation."""


class BackendTimeout(BackendError):
    """Backend exceeded the wall-clock timeout."""


async def call_claude(
    prompt: str,
    system: str = "",
    *,
    schema: Optional[dict] = None,
    cwd: Optional[str] = None,
    allow_tools: bool = False,
    timeout: float = 600.0,
    model: Optional[str] = None,
) -> str:
    """Invoke `claude -p`. Returns raw stdout (text or JSON envelope per --output-format)."""
    cmd = ["claude", "-p", prompt]
    if system:
        cmd += ["--append-system-prompt", system]
    if schema is not None:
        cmd += ["--output-format", "json", "--json-schema", json.dumps(schema)]
    if not allow_tools:
        cmd += ["--tools", ""]
    if model:
        cmd += ["--model", model]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise BackendTimeout(f"claude -p exceeded {timeout}s")

    if proc.returncode != 0:
        raise BackendError(
            f"claude -p exited {proc.returncode}: {err_b.decode(errors='ignore')}"
        )
    return out_b.decode(errors="ignore")


def parse_claude_envelope(raw: str) -> str:
    """When --output-format json is used, claude returns an envelope. Extract `result`.

    Tolerant: if `raw` is not an envelope, return it unchanged.
    """
    s = raw.strip()
    if not s:
        return raw
    if not (s.startswith("{") and s.endswith("}")):
        return raw
    try:
        env = json.loads(s)
    except json.JSONDecodeError:
        return raw
    if isinstance(env, dict) and "result" in env and isinstance(env["result"], str):
        return env["result"]
    return raw
