"""fanout: split a task, let Jev route each piece to the best installed coding agent,
run them in parallel (tmux panes or headless), with caveman + rtk + headroom on.

  fanout plan "task"                 plan -> Jev routes -> run   (the main command)
  fanout run "p1" "p2" ...           Jev routes each prompt you give it -> run
  fanout <agent> N "prompt" ...      N copies of one agent (claude, codex, gemini, cursor, ...)
  fanout agents                      what's installed + who's calling
  fanout doctor                      agents, Jev, planner, boosts, tmux
  fanout boost                       install/wire caveman, rtk, headroom into every agent
  fanout status|logs [RUN]           inspect a run
  fanout exec  < request.json        JSON in / JSON out, for any tool that can shell out
  fanout mcp                         stdio MCP server (Claude, Codex, Cursor, Gemini, ...)
  fanout install                     register the MCP server + skill in every detected client
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Dict, List, Optional

import boost as boostmod
import dispatch
import install
import jev
import mcp_server
import planner as plannermod
import router
from agents import BUILTIN, detect, detect_host
from context import build_context_preamble, pack_repo, wrap_with_context  # noqa: F401  (public API)
from planner import extract_json as _extract_json  # noqa: F401  (public API)

HERE = pathlib.Path(__file__).resolve().parent


def eprint(*a) -> None:
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# core API (shared by CLI, `exec`, and the MCP server)
# ---------------------------------------------------------------------------


def make_plan(
    task: str,
    *,
    subtasks: Optional[List[dict]] = None,
    n_hint: Optional[int] = None,
    max_n: int = 6,
    only: Optional[List[str]] = None,
    pin: Optional[str] = None,
    planner: str = "auto",
    use_jev: bool = True,
    boosts: Optional[Dict[str, bool]] = None,
    preamble: str = "",
    host: Optional[str] = None,
    cwd: Optional[str] = None,
    yolo: bool = False,
    timeout: Optional[int] = None,
    balance: bool = True,
) -> dict:
    agents = detect(only)
    if pin:
        if pin not in agents:
            raise SystemExit(f"fanout: agent {pin!r} is not installed (found: {', '.join(agents) or 'none'})")
    if not agents:
        raise SystemExit("fanout: no coding agents found on PATH. Run `fanout agents` to see what's supported.")
    host = host or detect_host()

    if subtasks:
        width = {"n": len(subtasks), "source": "caller"}
        rationale, psrc = "subtasks supplied by caller", "caller"
    else:
        n, wsrc = router.route_width(task, context=preamble, max_n=max_n, hint=n_hint, use_jev=use_jev)
        width = {"n": n, "source": wsrc}
        subtasks, rationale, psrc = plannermod.plan(task, n, context=preamble, agents=agents,
                                                    planner=planner, host=host)
    assignments = router.assign(task, subtasks, agents, boosts=boosts, use_jev=use_jev,
                                balance=balance, pin=pin)
    return {
        "task": task,
        "created": time.time(),
        "host": host,
        "cwd": os.path.abspath(cwd or os.getcwd()),
        "yolo": yolo,
        "timeout": timeout,
        "width": width,
        "planner": {"source": psrc, "rationale": rationale},
        "jev": {"enabled": use_jev and jev.api_key() is not None, "model": jev.JEV_MODEL},
        "agents": {k: a.binary for k, a in agents.items()},
        "assignments": [a.to_dict() for a in assignments],
    }


def _spawn_detached(run_dir: pathlib.Path, *, use_tmux: bool, keep_tmux: bool) -> None:
    argv = [sys.executable, str(HERE / "fanout.py"), "_execute", str(run_dir)]
    if not use_tmux:
        argv.append("--no-tmux")
    if keep_tmux:
        argv.append("--keep-tmux")
    log = open(run_dir / "fanout.log", "ab")
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)


def launch(plan: dict, *, preamble: str = "", use_tmux: bool = True, keep_tmux: bool = False,
           detach: bool = False, log=eprint) -> dict:
    run_dir = dispatch.new_run_dir()
    dispatch.save_plan(run_dir, plan, preamble)
    if detach:
        _spawn_detached(run_dir, use_tmux=use_tmux, keep_tmux=keep_tmux)
        return {"run_id": run_dir.name, "run_dir": str(run_dir), "state": "started",
                "tmux": f"fanout_{run_dir.name[-6:]}" if use_tmux and dispatch.tmux_available() else None}
    st = asyncio.run(dispatch.execute(run_dir, use_tmux=use_tmux, keep_tmux=keep_tmux, log=log))
    st["run_dir"] = str(run_dir)
    return st


def results(run_id: Optional[str] = None, tail: Optional[int] = None) -> dict:
    run_dir = dispatch.resolve_run(run_id)
    return {"run_id": run_dir.name, "run_dir": str(run_dir), "status": dispatch.read_status(run_dir),
            "outputs": dispatch.outputs_for(run_dir, tail)}


def agents_report(host: Optional[str] = None) -> dict:
    found = detect()
    return {
        "host": host or detect_host(),
        "installed": {k: a.to_dict() for k, a in found.items()},
        "supported": sorted(BUILTIN),
    }


def doctor_report() -> dict:
    key = jev.api_key()
    jev_state = "no key (heuristic routing)"
    if key:
        try:
            jev_state = jev.ping(key)
        except jev.JevError as e:
            jev_state = f"error: {e} (heuristic routing)"
    return {
        "agents": agents_report(),
        "jev": {"model": jev.JEV_MODEL, "state": jev_state},
        "planner": {"openrouter_model": plannermod.PLANNER_MODEL, "key": bool(key)},
        "boost": boostmod.status(),
        "tmux": dispatch.tmux_available(),
        "runs_dir": str(dispatch.RUNS_DIR),
    }


# ---------------------------------------------------------------------------
# printing
# ---------------------------------------------------------------------------


def _boost_str(b: dict) -> str:
    parts = []
    if b.get("caveman"):
        parts.append(f"caveman:{b['caveman']}")
    if b.get("rtk"):
        parts.append("rtk")
    if b.get("headroom"):
        parts.append("headroom")
    return ",".join(parts) or "-"


def print_plan(plan: dict, out=sys.stdout) -> None:
    w, p = plan["width"], plan["planner"]
    print(f"[fanout] host={plan.get('host') or '?'}  agents={','.join(plan['agents'])}  "
          f"jev={'on' if plan['jev']['enabled'] else 'off'}", file=out, flush=True)
    print(f"[fanout] width={w['n']} ({w['source']})  planner={p['source']}  {p['rationale']}", file=out)
    for a in plan["assignments"]:
        conf = f"{a['confidence']:.2f}" if a.get("confidence") else "--"
        dep = f" after {','.join(map(str, a['depends_on']))}" if a.get("depends_on") else ""
        print(f"  W{a['id']} wave{a['wave']} {a['agent']:<9} conf={conf} size={a['size']:<7} "
              f"boost={_boost_str(a['boost'])}  [{a['source']}]{dep}  {a['title']}", file=out, flush=True)


def print_results(st: dict, run_dir: pathlib.Path, tail: int = 1500) -> None:
    outs = dispatch.outputs_for(run_dir, tail)
    for k, v in sorted(st["workers"].items(), key=lambda kv: int(kv[0])):
        print(f"\n===== W{k} {v['agent']} [{v['state']}] {v.get('title', '')} =====")
        print(outs.get(f"W{k}", "").rstrip() or "(no output)")
    print(f"\n[fanout] {st['state']}  run: {run_dir}")


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", default=None, help="pack repo/file contents into every prompt")
    p.add_argument("--paste", action="store_true", help="read extra context from stdin")
    p.add_argument("--no-tmux", action="store_true", help="run headless instead of tmux panes")
    p.add_argument("--keep-tmux", action="store_true", help="leave the tmux session open")
    p.add_argument("--timeout", type=int, default=None, help="per-worker timeout (default: by task size)")
    p.add_argument("--dry-run", action="store_true", help="print the routed plan, don't run")
    p.add_argument("--detach", action="store_true", help="start the run in the background and return")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--yolo", action="store_true", help="skip agent permission prompts")
    p.add_argument("--cwd", default=None, help="working dir for the agents (default: .)")
    p.add_argument("--no-jev", action="store_true", help="heuristic routing only")
    p.add_argument("--no-caveman", action="store_true")
    p.add_argument("--no-rtk", action="store_true")
    p.add_argument("--no-headroom", action="store_true")


def _add_routing(p: argparse.ArgumentParser) -> None:
    p.add_argument("--agents", default=None, help="comma list restricting which agents may be used")
    p.add_argument("--pin", default=None, help="send every subtask to this agent")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="fanout", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("plan", help="plan -> Jev routes subtasks to agents -> run")
    pp.add_argument("task")
    pp.add_argument("-n", dest="n_hint", type=int, default=None, help="force the number of subtasks")
    pp.add_argument("--max", dest="max_n", type=int, default=6)
    pp.add_argument("--planner", default="auto", help="auto | openrouter | naive | <agent name>")
    _add_routing(pp)
    _add_common(pp)

    rp = sub.add_parser("run", help="route each given prompt to the best agent -> run")
    rp.add_argument("prompts", nargs="+")
    _add_routing(rp)
    _add_common(rp)

    for name in BUILTIN:
        ag = sub.add_parser(name, help=f"N parallel {name} workers")
        ag.add_argument("n", type=int)
        ag.add_argument("prompts", nargs="+")
        _add_common(ag)

    a = sub.add_parser("agents", help="list installed agents + calling host")
    a.add_argument("--json", action="store_true")
    d = sub.add_parser("doctor", help="check agents, Jev, planner, boosts, tmux")
    d.add_argument("--json", action="store_true")

    b = sub.add_parser("boost", help="install/wire caveman, rtk, headroom into every detected agent")
    b.add_argument("--only", default=None, help="comma list: caveman,rtk,headroom")
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--status", action="store_true", help="show status only")

    for name in ("status", "logs"):
        s = sub.add_parser(name, help=f"{name} of a run (default: latest)")
        s.add_argument("run", nargs="?", default=None)
        s.add_argument("--json", action="store_true")
        s.add_argument("--tail", type=int, default=None)

    sub.add_parser("exec", help="JSON request on stdin -> JSON result on stdout")
    sub.add_parser("mcp", help="stdio MCP server")

    i = sub.add_parser("install", help="register fanout MCP + skill in every detected client")
    i.add_argument("--clients", default=None, help="comma list (default: all detected)")
    i.add_argument("--dry-run", action="store_true")
    i.add_argument("--no-boost", action="store_true", help="skip `fanout boost`")

    x = sub.add_parser("_execute")
    x.add_argument("run_dir")
    x.add_argument("--no-tmux", action="store_true")
    x.add_argument("--keep-tmux", action="store_true")
    return ap.parse_args(argv)


def _boost_flags(a: argparse.Namespace) -> Dict[str, bool]:
    return {"caveman": not a.no_caveman, "rtk": not a.no_rtk, "headroom": not a.no_headroom}


def _only(a: argparse.Namespace) -> Optional[List[str]]:
    v = a.agents
    return [s.strip() for s in v.split(",") if s.strip()] if v else None


def _prompts_to_subtasks(prompts: List[str], n: Optional[int] = None) -> List[dict]:
    if n is not None:
        if len(prompts) == 1:
            prompts = prompts * n
        elif len(prompts) != n:
            raise SystemExit(f"fanout: got {len(prompts)} prompts for N={n} (pass 1 prompt or exactly N)")
    return [{"title": p[:60], "prompt": p, "depends_on": []} for p in prompts]


def _run_cli(a: argparse.Namespace, plan: dict, preamble: str) -> int:
    if a.json and a.dry_run:
        print(json.dumps(plan, indent=2))
        return 0
    print_plan(plan, out=sys.stderr if a.json else sys.stdout)
    if a.dry_run:
        return 0
    st = launch(plan, preamble=preamble, use_tmux=not a.no_tmux, keep_tmux=a.keep_tmux, detach=a.detach)
    if a.json:
        print(json.dumps(st if a.detach else results(st["run_id"]), indent=2))
    elif a.detach:
        print(f"[fanout] started {st['run_id']}  (fanout status {st['run_id']})")
        if st.get("tmux"):
            print(f"[fanout] attach: tmux attach -t {st['tmux']}")
    else:
        print_results(st, pathlib.Path(st["run_dir"]))
    return 0 if st.get("state") in ("done", "started") else 1


def cmd_exec() -> int:
    req = json.loads(sys.stdin.read() or "{}")
    print(json.dumps(handle_request(req), indent=2))
    return 0


def handle_request(req: dict) -> dict:
    """One JSON entrypoint for `fanout exec` and the MCP server's fanout_run/fanout_plan."""
    preamble = build_context_preamble(req.get("repo"), False, req.get("context") or "")
    subtasks = req.get("subtasks")
    if not subtasks and req.get("prompts"):
        subtasks = _prompts_to_subtasks(req["prompts"])
    if subtasks:
        subtasks, _ = plannermod.normalize_tasks({"tasks": subtasks})
    plan = make_plan(
        req.get("task") or "\n".join(s["prompt"] for s in subtasks or []),
        subtasks=subtasks,
        n_hint=req.get("n"),
        max_n=int(req.get("max_n") or 6),
        only=req.get("agents"),
        pin=req.get("pin"),
        planner=req.get("planner") or "auto",
        use_jev=not req.get("no_jev"),
        boosts=req.get("boost"),
        preamble=preamble,
        host=req.get("host"),
        cwd=req.get("cwd"),
        yolo=bool(req.get("yolo")),
        timeout=req.get("timeout"),
    )
    if req.get("dry_run"):
        return {"plan": plan}
    wait = bool(req.get("wait"))
    st = launch(plan, preamble=preamble, use_tmux=bool(req.get("tmux", not wait)), detach=not wait,
                log=eprint)
    if wait:
        return {"plan": plan, **results(st["run_id"], req.get("tail"))}
    return {"plan": plan, **st}


def main(argv: Optional[List[str]] = None) -> int:
    a = parse_args(argv)

    if a.cmd == "agents":
        rep = agents_report()
        if a.json:
            print(json.dumps(rep, indent=2))
            return 0
        print(f"host: {rep['host'] or 'unknown'}")
        for k, v in rep["installed"].items():
            print(f"  {k:<9} {v['binary']}\n            {v['strengths']}")
        missing = [s for s in rep["supported"] if s not in rep["installed"]]
        print(f"not installed: {', '.join(missing) or '-'}")
        return 0

    if a.cmd == "doctor":
        rep = doctor_report()
        if a.json:
            print(json.dumps(rep, indent=2))
            return 0
        ag = rep["agents"]
        print(f"host:     {ag['host'] or 'unknown'}")
        print(f"agents:   {', '.join(ag['installed']) or 'NONE'}")
        print(f"jev:      {rep['jev']['model']}: {rep['jev']['state']}")
        print(f"planner:  openrouter={'yes' if rep['planner']['key'] else 'no key'} -> local agent -> naive")
        for k, v in rep["boost"].items():
            extra = f" proxy={'up' if v.get('proxy_running') else 'down'}" if k == "headroom" else ""
            print(f"{k + ':':<10}{'installed' if v['installed'] else 'missing'}{extra}")
        print(f"tmux:     {'yes' if rep['tmux'] else 'no (headless)'}")
        return 0

    if a.cmd == "boost":
        if a.status:
            print(json.dumps(boostmod.status(), indent=2))
            return 0
        only = [s.strip() for s in a.only.split(",")] if a.only else None
        res = boostmod.enable(detect(), only=only, dry_run=a.dry_run)
        return 0 if all(r.get("ok", True) for r in res) else 1

    if a.cmd in ("status", "logs"):
        r = results(a.run, a.tail if a.cmd == "logs" else 0)
        if a.json:
            print(json.dumps(r if a.cmd == "logs" else r["status"], indent=2))
        elif a.cmd == "status":
            st = r["status"]
            print(f"{r['run_id']}  {st['state']}" + (f"  tmux={st['tmux']}" if st.get("tmux") else ""))
            for k, v in sorted(st["workers"].items(), key=lambda kv: int(kv[0])):
                print(f"  W{k} wave{v['wave']} {v['agent']:<9} {v['state']:<8} {v.get('title', '')}")
        else:
            for k, v in r["outputs"].items():
                print(f"===== {k} =====\n{v.rstrip()}\n")
        return 0

    if a.cmd == "exec":
        return cmd_exec()

    if a.cmd == "mcp":
        return mcp_server.serve()

    if a.cmd == "install":
        return install.run(clients=a.clients, dry_run=a.dry_run, boost=not a.no_boost)

    if a.cmd == "_execute":
        run_dir = pathlib.Path(a.run_dir)
        st = asyncio.run(dispatch.execute(run_dir, use_tmux=not a.no_tmux, keep_tmux=a.keep_tmux, log=eprint))
        return 0 if st["state"] == "done" else 1

    preamble = build_context_preamble(a.repo, a.paste)
    common = dict(boosts=_boost_flags(a), use_jev=not a.no_jev, preamble=preamble, cwd=a.cwd,
                  yolo=a.yolo, timeout=a.timeout)
    if a.cmd == "plan":
        plan = make_plan(a.task, n_hint=a.n_hint, max_n=a.max_n, only=_only(a), pin=a.pin,
                         planner=a.planner, **common)
    elif a.cmd == "run":
        plan = make_plan("\n".join(a.prompts), subtasks=_prompts_to_subtasks(a.prompts),
                         only=_only(a), pin=a.pin, **common)
    else:
        plan = make_plan(a.prompts[0] if len(a.prompts) == 1 else "\n".join(a.prompts),
                         subtasks=_prompts_to_subtasks(a.prompts, a.n), pin=a.cmd, balance=False, **common)
    return _run_cli(a, plan, preamble)


if __name__ == "__main__":
    sys.exit(main())
