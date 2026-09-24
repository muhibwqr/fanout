"""Jev-powered routing: how wide to fan out, and which agent gets which subtask.

Jev answers typed questions, it does not write text. So the split is:
  1. Jev scores how many parallel agents the task deserves (`route_width`).
  2. A planner (host LLM, OpenRouter chat model, or a local agent) writes the
     subtask prompts (see planner.py).
  3. Jev assigns every subtask: agent (choice), independent (noul), size (score),
     and which token savers to turn on — caveman level, rtk, headroom.
Without a key every step falls back to deterministic heuristics.
"""
from __future__ import annotations

import math
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

import jev
from agents import Agent

SIZE_LEVELS = [
    "trivial: a one-line or config change",
    "small: one file or one function",
    "medium: a few files or one feature",
    "large: many files, a new subsystem, or a broad audit",
]
SIZE_NAMES = ["trivial", "small", "medium", "large"]
SIZE_TIMEOUT = {"trivial": 300, "small": 600, "medium": 1200, "large": 2400}

CAVEMAN_LEVELS = [
    "lite: output is prose for humans (docs, copy, explanations) so wording matters",
    "full: normal engineering work mixing code and short explanations",
    "ultra: purely mechanical code or data work where only the diff matters",
]
CAVEMAN_NAMES = ["lite", "full", "ultra"]

PREFERENCE = ["claude", "codex", "gemini", "cursor", "amp", "droid", "opencode",
              "copilot", "goose", "qwen", "crush", "aider"]


@dataclass
class Assignment:
    id: int
    title: str
    prompt: str
    agent: str
    confidence: float = 0.0
    probabilities: Dict[str, float] = field(default_factory=dict)
    independent: bool = True
    depends_on: List[int] = field(default_factory=list)
    wave: int = 1
    size: str = "medium"
    boost: Dict[str, object] = field(default_factory=dict)
    source: str = "heuristic"

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Width
# ---------------------------------------------------------------------------


def width_questions(max_n: int) -> Dict[str, dict]:
    levels = ["1 agent: the task is atomic or strictly sequential"] + [
        f"{k} agents: {k} genuinely independent workstreams" for k in range(2, max_n + 1)
    ]
    return {
        "width": jev.score(
            "How many coding agents should work on this task in parallel without "
            "stepping on each other? Prefer fewer agents when pieces share files.",
            levels,
        ),
    }


def heuristic_width(task: str, max_n: int) -> int:
    parts = _split_candidates(task)
    n = len(parts) if len(parts) > 1 else 1
    if n == 1:
        words = len(task.split())
        n = 1 if words < 12 else 2 if words < 40 else 3
        if re.search(r"\b(audit|review|brainstorm|compare|research)\b", task, re.I):
            n = max(n, 3)
    return max(1, min(max_n, n))


def route_width(task: str, *, context: str = "", max_n: int = 6,
                hint: Optional[int] = None, use_jev: bool = True) -> Tuple[int, str]:
    """Returns (n, source)."""
    if hint:
        return max(1, min(max_n, hint)), "hint"
    if use_jev and jev.api_key():
        try:
            ans = jev.decide({"task": task, "context_excerpt": context[:8000]}, width_questions(max_n))
            s = float(ans["width"]["score"])
            return max(1, min(max_n, int(round(s)) + 1)), "jev"
        except (jev.JevError, KeyError, TypeError, ValueError):
            pass
    return heuristic_width(task, max_n), "heuristic"


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


def assign_questions(agents: Dict[str, Agent]) -> Dict[str, dict]:
    return {
        "agent": jev.choice(
            "Which available coding agent is the best fit to own this subtask?",
            {name: a.strengths for name, a in agents.items()},
        ),
        "independent": jev.noul(
            "Can this subtask start right now, without waiting for any sibling subtask's output?",
            "It only needs the original task and repo; it can run in parallel.",
            "It consumes, integrates, or tests what another sibling subtask produces.",
        ),
        "size": jev.score("How much work is this subtask?", SIZE_LEVELS),
        "caveman": jev.score(
            "How terse can the agent's replies be without hurting the deliverable?",
            CAVEMAN_LEVELS,
        ),
        "shell_heavy": jev.noul(
            "Will this subtask run many shell commands (git, tests, builds, linters, package managers)?",
            "Yes, lots of command output will be read.",
            "No, mostly reading/writing code or prose.",
        ),
        "context_heavy": jev.noul(
            "Will this subtask read large inputs: big files, logs, JSON, search results, or a whole repo?",
            "Yes, large tool outputs or files will flow into context.",
            "No, inputs are small and focused.",
        ),
    }


def _jev_assign(task: str, sub: dict, siblings: List[str], agents: Dict[str, Agent]) -> dict:
    state = {
        "overall_task": task,
        "subtask_title": sub.get("title", ""),
        "subtask": sub["prompt"],
        "sibling_subtasks": siblings,
        "available_agents": {n: a.strengths for n, a in agents.items()},
    }
    return jev.decide(state, assign_questions(agents))


_DEP_RE = re.compile(
    r"\b(after|once|using the (output|result)s?|depends on|integrat\w*|wire (it|them|up)|"
    r"tie (it|them)? ?together|end-to-end|e2e|combine|merge the)\b",
    re.I,
)
_LARGE_RE = re.compile(r"\b(entire|whole|all (files|modules)|codebase|audit|migrat\w*|subsystem|from scratch)\b", re.I)
_SMALL_RE = re.compile(r"\b(rename|typo|bump|one-line|config|readme|comment)\b", re.I)
_SHELL_RE = re.compile(r"\b(test|tests|build|lint|ci|git|deploy|docker|install|script|bench\w*|run)\b", re.I)
_CTX_RE = re.compile(r"\b(log|logs|json|repo|codebase|audit|review|read|analy[sz]e|search)\b", re.I)
_PROSE_RE = re.compile(r"\b(docs?|readme|copy|blog|explain|write-?up|report|email|summar\w*)\b", re.I)
_MECH_RE = re.compile(r"\b(rename|format|boilerplate|migrat\w*|bulk|codemod|convert|translate)\b", re.I)

_AGENT_HINTS = {
    "claude": r"refactor|architect|multi-file|tests?|api|backend|feature|careful",
    "codex": r"backend|algorithm|script|shell|devops|debug|fix|failing|cli|database|sql|schema",
    "gemini": r"research|summar|large|docs|audit|read|frontend|ui\b|design|image",
    "cursor": r"frontend|ui\b|react|component|css|tailwind|typescript|page",
    "amp": r"search|review|cross-cutting|codebase",
    "copilot": r"github|pr\b|pull request|issue|actions|workflow",
    "aider": r"small|surgical|single file|tweak",
    "qwen": r"boilerplate|bulk|translate",
    "goose": r"automat|setup|environment|mcp",
    "droid": r"migration|end-to-end|autonomous",
}


def _heuristic_agent(text: str, agents: Dict[str, Agent]) -> Tuple[str, Dict[str, float]]:
    scores: Dict[str, float] = {}
    words = set(re.findall(r"[a-z]{4,}", text.lower()))
    for name, a in agents.items():
        s = 0.0
        pat = _AGENT_HINTS.get(name)
        if pat:
            s += 2.0 * len(re.findall(r"\b(?:" + pat + ")", text, re.I))
        s += 0.5 * len(words & set(re.findall(r"[a-z]{4,}", a.strengths.lower())))
        rank = PREFERENCE.index(name) if name in PREFERENCE else len(PREFERENCE)
        s += 0.3 * (len(PREFERENCE) - rank) / len(PREFERENCE)
        scores[name] = s
    total = sum(scores.values()) or 1.0
    probs = {k: round(v / total, 3) for k, v in scores.items()}
    best = max(scores, key=lambda k: scores[k])
    return best, probs


def _heuristic_answers(sub: dict, agents: Dict[str, Agent]) -> dict:
    text = f"{sub.get('title', '')}\n{sub['prompt']}"
    best, probs = _heuristic_agent(text, agents)
    size = 3 if _LARGE_RE.search(text) else 0 if _SMALL_RE.search(text) and len(text) < 300 else 2 if len(text) > 600 else 1
    cave = 0 if _PROSE_RE.search(text) else 2 if _MECH_RE.search(text) else 1
    return {
        "agent": {"type": "choice", "choice": best, "probabilities": probs,
                  "confidence": max(probs.values()) if probs else 0.0},
        "independent": {"type": "noul", "noul": 0.2 if _DEP_RE.search(text) else 0.85},
        "size": {"type": "score", "score": float(size)},
        "caveman": {"type": "score", "score": float(cave)},
        "shell_heavy": {"type": "noul", "noul": 0.8 if _SHELL_RE.search(text) else 0.3},
        "context_heavy": {"type": "noul", "noul": 0.8 if _CTX_RE.search(text) else 0.3},
    }


def _level(ans: dict, names: List[str], default: int) -> str:
    try:
        idx = int(round(float(ans["score"])))
    except (KeyError, TypeError, ValueError):
        idx = default
    return names[max(0, min(len(names) - 1, idx))]


def _balance(assignments: List[Assignment], agents: Dict[str, Agent]) -> None:
    """Keep one agent from hogging the fan-out when others are nearly as good."""
    if len(agents) < 2 or len(assignments) < 3:
        return
    cap = math.ceil(len(assignments) / len(agents))
    load: Dict[str, int] = {}
    for a in sorted(assignments, key=lambda x: -x.confidence):
        if load.get(a.agent, 0) < cap:
            load[a.agent] = load.get(a.agent, 0) + 1
            continue
        ranked = sorted(a.probabilities.items(), key=lambda kv: -kv[1])
        floor = 0.5 * a.probabilities.get(a.agent, 0.0)
        for name, p in ranked:
            if name in agents and load.get(name, 0) < cap and p > 0 and p >= floor:
                a.agent = name
                break
        load[a.agent] = load.get(a.agent, 0) + 1


def _assign_waves(assignments: List[Assignment]) -> None:
    by_id = {a.id: a for a in assignments}
    for a in assignments:
        if not a.depends_on and not a.independent:
            a.depends_on = [b.id for b in assignments if b.independent and b.id < a.id]
    for _ in range(len(assignments)):
        changed = False
        for a in assignments:
            deps = [by_id[d] for d in a.depends_on if d in by_id and d != a.id]
            want = 1 + max((d.wave for d in deps), default=0)
            if want != a.wave and want <= len(assignments):
                a.wave = want
                changed = True
        if not changed:
            break


def assign(
    task: str,
    subtasks: List[dict],
    agents: Dict[str, Agent],
    *,
    boosts: Optional[Dict[str, bool]] = None,
    use_jev: bool = True,
    balance: bool = True,
    pin: Optional[str] = None,
) -> List[Assignment]:
    """Route each subtask to an agent and decide its boost mix."""
    if not agents:
        raise ValueError("no agents available")
    boosts = boosts or {}
    titles = [s.get("title") or s["prompt"][:80] for s in subtasks]
    live = use_jev and jev.api_key() is not None and not pin

    def one(i: int) -> Tuple[dict, str]:
        sub = subtasks[i]
        if live:
            try:
                sibs = [t for j, t in enumerate(titles) if j != i]
                return _jev_assign(task, sub, sibs, agents), "jev"
            except jev.JevError:
                pass
        return _heuristic_answers(sub, agents), "heuristic"

    with ThreadPoolExecutor(max_workers=min(8, len(subtasks)) or 1) as ex:
        results = list(ex.map(one, range(len(subtasks))))

    out: List[Assignment] = []
    for i, (ans, source) in enumerate(results):
        sub = subtasks[i]
        agent_ans = ans.get("agent", {})
        probs = {k: float(v) for k, v in (agent_ans.get("probabilities") or {}).items()}
        chosen = pin or agent_ans.get("choice")
        if chosen not in agents:
            chosen = max(probs, key=lambda k: probs[k]) if probs else next(iter(agents))
            if chosen not in agents:
                chosen = next(iter(agents))
        indep = float(ans.get("independent", {}).get("noul", 1.0)) >= 0.5
        deps = [int(d) for d in sub.get("depends_on") or [] if str(d).isdigit()]
        cave = _level(ans.get("caveman", {}), CAVEMAN_NAMES, 1)
        out.append(Assignment(
            id=i + 1,
            title=titles[i],
            prompt=sub["prompt"],
            agent=chosen,
            confidence=float(agent_ans.get("confidence") or (probs.get(chosen, 0.0))),
            probabilities=probs,
            independent=indep and not deps,
            depends_on=deps,
            size=_level(ans.get("size", {}), SIZE_NAMES, 2),
            boost={
                "caveman": cave if boosts.get("caveman", True) else None,
                "rtk": bool(boosts.get("rtk", True)) and float(ans.get("shell_heavy", {}).get("noul", 1.0)) >= 0.35,
                "headroom": bool(boosts.get("headroom", True)) and float(ans.get("context_heavy", {}).get("noul", 1.0)) >= 0.35,
            },
            source=source,
        ))
    if balance and not pin:
        _balance(out, agents)
    _assign_waves(out)
    return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _split_candidates(task: str) -> List[str]:
    lines = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", ln).strip() for ln in task.splitlines()]
    lines = [ln for ln in lines if ln]
    if len(lines) > 1:
        return lines
    parts = re.split(r";|\bthen\b|,\s*and\b|,(?=\s*(?:a|an|the)?\s*\w+\s+\w+)", task)
    parts = [p.strip(" .") for p in parts if p and len(p.strip().split()) >= 2]
    return parts if len(parts) > 1 else [task.strip()]
