"""Write the subtask prompts. Jev can't generate text, so a planner does.

Planner sources, in `auto` order:
  1. subtasks supplied by the caller (an MCP host like Claude/Cursor is itself the planner)
  2. OpenRouter chat model (FANOUT_PLANNER_MODEL, default openai/gpt-4o-mini)
  3. a local agent CLI run headlessly (claude, codex, gemini, ...)
  4. naive split of the task text
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

import jev
from agents import Agent
from router import _split_candidates as split_candidates

PLANNER_MODEL = os.environ.get("FANOUT_PLANNER_MODEL", "openai/gpt-4o-mini")
CHAT_URL = os.environ.get("FANOUT_CHAT_URL", "https://openrouter.ai/api/v1/chat/completions")

PLANNER_SYSTEM = """You are the planner for fanout, which runs several coding agents in parallel.
Split the user's task into exactly N subtasks. Output ONLY JSON, no prose, no fences:

{"rationale": "<one sentence>",
 "tasks": [{"title": "<3-8 words>", "prompt": "<self-contained prompt>", "depends_on": [<1-based ids>]}]}

Rules:
- Exactly N tasks. If the work can't be split, still produce N tasks by adding
  review/test/docs work, or explain in rationale and return fewer.
- Each prompt is read cold by an agent that never sees the others: restate the
  goal, the stack, file/dir ownership, and interfaces it must honour.
- Give each task clear file ownership so parallel agents don't edit the same files.
- depends_on lists tasks whose output this one needs; keep it empty whenever possible.
"""

LOCAL_PLANNER_ORDER = ["claude", "codex", "gemini", "cursor", "opencode", "qwen", "amp", "droid", "goose", "crush"]


class PlannerError(RuntimeError):
    pass


def extract_json(raw: str):
    s = (raw or "").strip()
    if not s:
        raise ValueError("empty planner output")
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    for i, c in enumerate(s):
        if c not in "{[":
            continue
        sub = _balanced(s, i)
        if sub is None:
            continue
        try:
            return json.loads(sub)
        except json.JSONDecodeError:
            continue
    raise ValueError("no parseable JSON")


def _balanced(s: str, start: int) -> Optional[str]:
    o = s[start]
    c = "}" if o == "{" else "]"
    depth, in_str, esc = 0, False, False
    for j in range(start, len(s)):
        ch = s[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == o:
                depth += 1
            elif ch == c:
                depth -= 1
                if depth == 0:
                    return s[start: j + 1]
    return None


def normalize_tasks(obj) -> Tuple[List[dict], str]:
    if isinstance(obj, list):
        obj = {"tasks": obj}
    if not isinstance(obj, dict) or not isinstance(obj.get("tasks"), list):
        raise ValueError(f"bad planner shape: {str(obj)[:200]}")
    tasks: List[dict] = []
    for t in obj["tasks"]:
        if isinstance(t, str):
            t = {"prompt": t}
        if not isinstance(t, dict) or not str(t.get("prompt", "")).strip():
            continue
        tasks.append({
            "title": str(t.get("title") or t["prompt"][:60]).strip(),
            "prompt": str(t["prompt"]).strip(),
            "depends_on": [int(d) for d in t.get("depends_on") or [] if str(d).isdigit()],
        })
    if not tasks:
        raise ValueError("planner returned no tasks")
    return tasks, str(obj.get("rationale") or "")


def planner_input(task: str, n: int, context: str, agents: Dict[str, Agent]) -> str:
    return json.dumps({
        "N": n,
        "task": task,
        "available_agents": {k: a.strengths for k, a in agents.items()},
        "context": context[:60_000] or None,
    }, indent=2)


def _plan_openrouter(task: str, n: int, context: str, agents: Dict[str, Agent], timeout: float) -> str:
    key = jev.api_key()
    if not key:
        raise PlannerError("no OpenRouter key")
    body = json.dumps({
        "model": PLANNER_MODEL,
        "messages": [
            {"role": "system", "content": PLANNER_SYSTEM},
            {"role": "user", "content": planner_input(task, n, context, agents)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }).encode()
    req = urllib.request.Request(CHAT_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "X-Title": "fanout",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
        return payload["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        raise PlannerError(f"OpenRouter HTTP {e.code}: {e.read().decode(errors='ignore')[:200]}") from e
    except (urllib.error.URLError, OSError, KeyError, IndexError, json.JSONDecodeError) as e:
        raise PlannerError(f"OpenRouter planner failed: {e}") from e


def local_planner_argv(agent: Agent, prompt: str) -> List[str]:
    exe = agent.binary or agent.bins[0]
    if agent.name == "claude":
        return [exe, "-p", prompt, "--tools", ""]
    if agent.name == "codex":
        return [exe, "exec", "--skip-git-repo-check", "--sandbox", "read-only", prompt]
    return agent.argv(prompt)


def _plan_local(agent: Agent, task: str, n: int, context: str, agents: Dict[str, Agent], timeout: float) -> str:
    prompt = PLANNER_SYSTEM + "\n\nINPUT:\n" + planner_input(task, n, context, agents)
    try:
        proc = subprocess.run(local_planner_argv(agent, prompt), capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PlannerError(f"{agent.name} planner failed: {e}") from e
    if proc.returncode != 0:
        raise PlannerError(f"{agent.name} planner exited {proc.returncode}: {proc.stderr[-300:]}")
    return proc.stdout


def naive_split(task: str, n: int) -> List[dict]:
    parts = split_candidates(task)[:max(1, n)]
    return [{"title": p[:60], "prompt": p if len(parts) > 1 else task, "depends_on": []} for p in parts]


def plan(
    task: str,
    n: int,
    *,
    context: str = "",
    agents: Dict[str, Agent],
    planner: str = "auto",
    host: Optional[str] = None,
    timeout: float = 180.0,
) -> Tuple[List[dict], str, str]:
    """Returns (tasks, rationale, source)."""
    if n <= 1 and planner == "auto":
        return [{"title": task[:60], "prompt": task, "depends_on": []}], "single agent", "direct"
    errors: List[str] = []
    order: List[str]
    if planner == "auto":
        local = [a for a in LOCAL_PLANNER_ORDER if a in agents]
        if host in local:
            local.remove(host)
            local.insert(0, host)
        order = ["openrouter"] + local + ["naive"]
    else:
        order = [planner]
    for src in order:
        if src == "naive":
            return naive_split(task, n), "naive split (no planner available)", "naive"
        try:
            if src == "openrouter":
                raw = _plan_openrouter(task, n, context, agents, timeout)
            elif src in agents:
                raw = _plan_local(agents[src], task, n, context, agents, timeout)
            else:
                raise PlannerError(f"unknown planner {src!r}")
            tasks, rationale = normalize_tasks(extract_json(raw))
            return tasks[: max(n, 1)], rationale, src
        except (PlannerError, ValueError) as e:
            errors.append(f"{src}: {e}")
    raise PlannerError("; ".join(errors) or "no planner")
