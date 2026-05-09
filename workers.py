"""Backend wrappers — `claude -p` headless and tmux-dispatched."""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import time
import uuid
from typing import List, Optional


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


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


def dispatch_tmux(
    prompts: List[str],
    *,
    run_dir: Optional[str] = None,
    session_name: Optional[str] = None,
    cwd: Optional[str] = None,
) -> dict:
    """Spawn one tmux session with N panes, one claude -p per pane.

    Each pane writes to <run_dir>/W<i>.out and touches <run_dir>/W<i>.done on completion.
    Returns {session, run_dir, pane_files}. Caller must `await wait_for_done(...)`.
    """
    if not tmux_available():
        raise RuntimeError("tmux not on PATH")
    rid = uuid.uuid4().hex[:8]
    session = session_name or f"fanout_{rid}"
    rd = pathlib.Path(run_dir or f"/tmp/fanout_{rid}")
    rd.mkdir(parents=True, exist_ok=True)

    pane_files: List[dict] = []
    for i, prompt in enumerate(prompts, start=1):
        prompt_file = rd / f"W{i}.prompt"
        out_file = rd / f"W{i}.out"
        done_file = rd / f"W{i}.done"
        prompt_file.write_text(prompt)
        # Each pane: read prompt from file, run claude -p, write stdout to .out, touch .done.
        # Trailing read keeps pane open for the user to inspect after worker exits.
        cmd = (
            f"echo '=== W{i} starting ==='; "
            f"claude -p \"$(cat {shlex.quote(str(prompt_file))})\" --tools '' "
            f"| tee {shlex.quote(str(out_file))}; "
            f"touch {shlex.quote(str(done_file))}; "
            f"echo; echo '=== W{i} done. ctrl-b d to detach. ==='; "
            f"sleep 86400"
        )
        pane_files.append(
            {"id": i, "prompt": str(prompt_file), "out": str(out_file), "done": str(done_file)}
        )
        if i == 1:
            new_session = [
                "tmux",
                "new-session",
                "-d",
                "-s",
                session,
                "-n",
                "fanout",
                "bash",
                "-c",
                cmd,
            ]
            if cwd:
                new_session = new_session[:3] + ["-c", cwd] + new_session[3:]
            subprocess.run(new_session, check=True)
        else:
            split = ["tmux", "split-window", "-t", session, "bash", "-c", cmd]
            subprocess.run(split, check=True)
            subprocess.run(["tmux", "select-layout", "-t", session, "tiled"], check=True)

    subprocess.run(["tmux", "select-layout", "-t", session, "tiled"], check=False)
    return {"session": session, "run_dir": str(rd), "pane_files": pane_files}


async def wait_for_done(
    pane_files: List[dict],
    *,
    poll_interval: float = 1.0,
    timeout: float = 1800.0,
) -> List[dict]:
    """Poll the .done sentinels. Returns list of {id, output, error} dicts."""
    pending = {p["id"]: p for p in pane_files}
    deadline = time.monotonic() + timeout
    results: List[dict] = []
    while pending and time.monotonic() < deadline:
        for wid, p in list(pending.items()):
            if pathlib.Path(p["done"]).exists():
                try:
                    out = pathlib.Path(p["out"]).read_text(errors="ignore")
                except OSError as e:
                    out = f"[ERROR] could not read {p['out']}: {e}"
                results.append({"id": wid, "output": out, "error": False})
                del pending[wid]
        if pending:
            await asyncio.sleep(poll_interval)
    for wid, p in pending.items():
        results.append(
            {
                "id": wid,
                "output": f"[ERROR] worker {wid} exceeded {timeout}s; pane still in tmux session",
                "error": True,
            }
        )
    results.sort(key=lambda r: r["id"])
    return results


def kill_tmux_session(session: str) -> None:
    subprocess.run(["tmux", "kill-session", "-t", session], check=False)


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
