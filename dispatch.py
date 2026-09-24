"""Run a plan: one worker per assignment, wave by wave, in tmux panes or headless.

Every run lives in ~/.cache/fanout/runs/<id>/:
  plan.json      the routed plan (input)
  context.txt    shared preamble (repo dump / paste)
  W<i>.sh        the exact command each worker ran
  W<i>.out       worker output
  W<i>.done      exit code
  status.json    live state, readable by `fanout status` / the MCP server
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shlex
import shutil
import signal
import subprocess
import time
import uuid
from typing import Dict, List, Optional

import boost as boostmod
from agents import Agent, detect
from context import wrap_with_context
from router import SIZE_TIMEOUT

RUNS_DIR = pathlib.Path(os.environ.get("FANOUT_RUNS_DIR", "~/.cache/fanout/runs")).expanduser()
UPSTREAM_CHARS = 6000
_PROMPT = "\x00PROMPT\x00"


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


def new_run_dir() -> pathlib.Path:
    rid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    d = RUNS_DIR / rid
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_run(run_id: Optional[str]) -> pathlib.Path:
    if run_id:
        p = pathlib.Path(run_id).expanduser()
        if p.is_dir():
            return p
        p = RUNS_DIR / run_id
        if p.is_dir():
            return p
        raise FileNotFoundError(f"no run {run_id!r} in {RUNS_DIR}")
    runs = sorted(RUNS_DIR.glob("*/plan.json"))
    if not runs:
        raise FileNotFoundError(f"no runs in {RUNS_DIR}")
    return runs[-1].parent


def save_plan(run_dir: pathlib.Path, plan: dict, preamble: str = "") -> None:
    (run_dir / "plan.json").write_text(json.dumps(plan, indent=2))
    (run_dir / "context.txt").write_text(preamble)


def load_plan(run_dir: pathlib.Path) -> dict:
    return json.loads((run_dir / "plan.json").read_text())


def read_status(run_dir: pathlib.Path) -> dict:
    p = run_dir / "status.json"
    if not p.exists():
        return {"state": "pending", "workers": {}}
    return json.loads(p.read_text())


def _write_status(run_dir: pathlib.Path, st: dict) -> None:
    tmp = run_dir / "status.json.tmp"
    tmp.write_text(json.dumps(st, indent=2))
    tmp.replace(run_dir / "status.json")


def worker_script(argv: List[str], *, prompt_file: pathlib.Path, out_file: pathlib.Path,
                  done_file: pathlib.Path, cwd: str, env: Dict[str, str], label: str,
                  hold: bool) -> str:
    cmd = " ".join('"$PROMPT"' if a == _PROMPT else shlex.quote(a) for a in argv)
    lines = ["#!/usr/bin/env bash", f"cd {shlex.quote(cwd)} || exit 1"]
    lines += [f"export {k}={shlex.quote(v)}" for k, v in sorted(env.items())]
    lines += [
        f"PROMPT=\"$(cat {shlex.quote(str(prompt_file))})\"",
        f"echo {shlex.quote('=== ' + label + ' ===')}",
        f"{cmd} < /dev/null 2>&1 | tee {shlex.quote(str(out_file))}",
        f"echo \"${{PIPESTATUS[0]}}\" > {shlex.quote(str(done_file))}",
    ]
    if hold:
        lines += ["echo", f"echo {shlex.quote('=== ' + label + ' done. ctrl-b d to detach ===')}", "exec sleep 86400"]
    return "\n".join(lines) + "\n"


def _upstream_block(deps: List[int], outputs: Dict[int, str], titles: Dict[int, str]) -> str:
    parts = []
    for d in deps:
        if d in outputs:
            out = outputs[d][-UPSTREAM_CHARS:]
            parts.append(f"<upstream id=\"{d}\" title=\"{titles.get(d, '')}\">\n{out}\n</upstream>")
    return "\n".join(parts)


def build_workers(plan: dict, wave: int, *, run_dir: pathlib.Path, agents: Dict[str, Agent],
                  outputs: Dict[int, str], preamble: str, hold: bool,
                  headroom_up: bool) -> List[dict]:
    titles = {a["id"]: a["title"] for a in plan["assignments"]}
    rtk_ok = shutil.which("rtk") is not None
    workers = []
    for a in plan["assignments"]:
        if a["wave"] != wave:
            continue
        agent = agents.get(a["agent"])
        if agent is None:
            workers.append({"id": a["id"], "agent": a["agent"], "error": f"agent {a['agent']} not installed"})
            continue
        up = _upstream_block(a.get("depends_on") or [], outputs, titles)
        body = a["prompt"] + (f"\n\nOutputs from the subtasks you depend on:\n{up}" if up else "")
        prompt = boostmod.decorate(wrap_with_context(body, preamble), a.get("boost") or {}, rtk_available=rtk_ok)
        env = {"FANOUT_RUN": run_dir.name, "FANOUT_WORKER": str(a["id"])}
        if (a.get("boost") or {}).get("headroom") and headroom_up:
            env.update(boostmod.headroom_env(agent.api))
        i = a["id"]
        pf, of, df, sf = (run_dir / f"W{i}.{ext}" for ext in ("prompt", "out", "done", "sh"))
        pf.write_text(prompt)
        sf.write_text(worker_script(
            agent.argv(_PROMPT, yolo=bool(plan.get("yolo"))), prompt_file=pf, out_file=of,
            done_file=df, cwd=plan.get("cwd") or os.getcwd(), env=env,
            label=f"W{i} {agent.name}: {a['title'][:60]}", hold=hold,
        ))
        sf.chmod(0o755)
        workers.append({"id": i, "agent": agent.name, "script": str(sf), "out": str(of), "done": str(df),
                        "timeout": plan.get("timeout") or SIZE_TIMEOUT.get(a.get("size", "medium"), 1200)})
    return workers


def _tmux(*args: str, check: bool = True) -> None:
    subprocess.run(["tmux", *args], check=check, stdout=subprocess.DEVNULL)


async def _run_tmux(session: str, wave: int, workers: List[dict], first: bool) -> None:
    runnable = [w for w in workers if "script" in w]
    for j, w in enumerate(runnable):
        cmd = ["bash", w["script"]]
        if j == 0:
            if first:
                _tmux("new-session", "-d", "-s", session, "-n", f"wave{wave}", *cmd)
            else:
                _tmux("new-window", "-t", session, "-n", f"wave{wave}", *cmd)
        else:
            _tmux("split-window", "-t", f"{session}:wave{wave}", *cmd)
            _tmux("select-layout", "-t", f"{session}:wave{wave}", "tiled", check=False)
    deadline = time.monotonic() + max((w["timeout"] for w in runnable), default=0)
    while time.monotonic() < deadline and not all(pathlib.Path(w["done"]).exists() for w in runnable):
        await asyncio.sleep(1.0)


async def _run_headless(workers: List[dict]) -> None:
    async def one(w: dict) -> None:
        proc = await asyncio.create_subprocess_exec(
            "bash", w["script"], stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=w["timeout"])
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            await proc.wait()
    await asyncio.gather(*(one(w) for w in workers if "script" in w))


def _collect(w: dict) -> dict:
    if "script" not in w:
        return {"id": w["id"], "agent": w["agent"], "state": "error", "exit": None, "output": w["error"]}
    done = pathlib.Path(w["done"])
    out = pathlib.Path(w["out"])
    text = out.read_text(errors="ignore") if out.exists() else ""
    if not done.exists():
        return {"id": w["id"], "agent": w["agent"], "state": "timeout", "exit": None, "output": text}
    code_s = done.read_text().strip()
    code = int(code_s) if code_s.lstrip("-").isdigit() else None
    return {"id": w["id"], "agent": w["agent"], "state": "ok" if code == 0 else "failed", "exit": code, "output": text}


async def execute(run_dir: pathlib.Path, *, use_tmux: bool = True, keep_tmux: bool = False,
                  log=lambda *_: None) -> dict:
    plan = load_plan(run_dir)
    preamble = (run_dir / "context.txt").read_text() if (run_dir / "context.txt").exists() else ""
    agents = detect()
    use_tmux = use_tmux and tmux_available()
    session = f"fanout_{run_dir.name[-6:]}"
    needs_headroom = any((a.get("boost") or {}).get("headroom") for a in plan["assignments"])
    headroom_up = needs_headroom and boostmod.ensure_headroom_proxy(run_dir)

    st = {"state": "running", "run_id": run_dir.name, "tmux": session if use_tmux else None,
          "started": time.time(), "headroom_proxy": headroom_up,
          "workers": {str(a["id"]): {"agent": a["agent"], "title": a["title"], "wave": a["wave"], "state": "queued"}
                      for a in plan["assignments"]}}
    _write_status(run_dir, st)
    if use_tmux:
        log(f"[fanout] tmux session: {session}   attach: tmux attach -t {session}")

    outputs: Dict[int, str] = {}
    waves = sorted({a["wave"] for a in plan["assignments"]})
    for k, wave in enumerate(waves):
        workers = build_workers(plan, wave, run_dir=run_dir, agents=agents, outputs=outputs,
                                preamble=preamble, hold=use_tmux, headroom_up=headroom_up)
        for w in workers:
            st["workers"][str(w["id"])]["state"] = "running"
        _write_status(run_dir, st)
        log(f"[fanout] wave {wave}: " + ", ".join(f"W{w['id']}->{w['agent']}" for w in workers))
        if use_tmux:
            await _run_tmux(session, wave, workers, first=(k == 0))
        else:
            await _run_headless(workers)
        for w in workers:
            r = _collect(w)
            outputs[r["id"]] = r["output"]
            st["workers"][str(r["id"])].update({"state": r["state"], "exit": r["exit"], "out": w.get("out")})
        _write_status(run_dir, st)

    if use_tmux and not keep_tmux:
        _tmux("kill-session", "-t", session, check=False)
    failed = [k for k, v in st["workers"].items() if v["state"] != "ok"]
    st.update({"state": "failed" if failed else "done", "finished": time.time()})
    _write_status(run_dir, st)
    return st


def outputs_for(run_dir: pathlib.Path, tail: Optional[int] = None) -> Dict[str, str]:
    res = {}
    for p in sorted(run_dir.glob("W*.out"), key=lambda p: int(p.stem[1:])):
        text = p.read_text(errors="ignore")
        res[p.stem] = text[-tail:] if tail else text
    return res
