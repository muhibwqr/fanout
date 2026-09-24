"""Minimal client for Jev (TypeSafe's decision model) via OpenRouter's Decisions API.

    POST https://openrouter.ai/api/alpha/decisions
    {"model": "typesafe/jev-1.13", "state": {...}, "questions": {...}}
    -> {"answers": {name: {"type": "choice"|"noul"|"score", ...}}, "usage": {...}}

Stdlib only. The key comes from FANOUT_OPENROUTER_API_KEY, OPENROUTER_API_KEY,
or ~/.config/fanout/openrouter_key.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Union

from agents import CONFIG_DIR

JEV_URL = os.environ.get("FANOUT_JEV_URL", "https://openrouter.ai/api/alpha/decisions")
JEV_MODEL = os.environ.get("FANOUT_JEV_MODEL", "typesafe/jev-1.13")
MAX_STATE_CHARS = 60_000


class JevError(RuntimeError):
    pass


def api_key() -> Optional[str]:
    for var in ("FANOUT_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"):
        if os.environ.get(var):
            return os.environ[var]
    p = CONFIG_DIR / "openrouter_key"
    if p.exists():
        key = p.read_text().strip()
        return key or None
    return None


def choice(instructions: str, options: Dict[str, str]) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": options}


def noul(instructions: str, yes: str, no: str) -> dict:
    return {"type": "noul", "instructions": instructions, "criteria": {"true": yes, "false": no}}


def score(instructions: str, levels: List[str]) -> dict:
    return {"type": "score", "instructions": instructions, "criteria": levels}


def _clip_state(state: Union[dict, str]) -> Union[dict, str]:
    raw = json.dumps(state)
    if len(raw) <= MAX_STATE_CHARS:
        return state
    if isinstance(state, str):
        return state[:MAX_STATE_CHARS]
    clipped = dict(state)
    budget = MAX_STATE_CHARS
    for k, v in sorted(clipped.items(), key=lambda kv: -len(json.dumps(kv[1]))):
        if isinstance(v, str) and len(raw) > budget:
            over = len(raw) - budget
            clipped[k] = v[: max(200, len(v) - over - 50)] + " [...]"
            raw = json.dumps(clipped)
    return clipped


def decide(
    state: Union[dict, str],
    questions: Dict[str, dict],
    *,
    key: Optional[str] = None,
    timeout: float = 30.0,
    model: Optional[str] = None,
) -> Dict[str, dict]:
    """Ask Jev typed questions about `state`. Returns the `answers` dict."""
    key = key or api_key()
    if not key:
        raise JevError("no OpenRouter API key (set OPENROUTER_API_KEY)")
    body = json.dumps(
        {"model": model or JEV_MODEL, "state": _clip_state(state), "questions": questions}
    ).encode()
    req = urllib.request.Request(
        JEV_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-Title": "fanout",
            "HTTP-Referer": "https://github.com/muhibwqr/fanout",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="ignore")[:300]
        raise JevError(f"Jev HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise JevError(f"Jev unreachable: {e}") from e
    except json.JSONDecodeError as e:
        raise JevError(f"Jev returned non-JSON: {e}") from e
    answers = payload.get("answers") if isinstance(payload, dict) else None
    if not isinstance(answers, dict):
        raise JevError(f"Jev response missing answers: {str(payload)[:300]}")
    return answers


def ping(key: Optional[str] = None) -> str:
    """Cheap health check. Returns 'ok' or raises JevError."""
    decide(
        {"text": "fanout health check"},
        {"ok": noul("Is this a health check?", "It is a health check.", "It is not.")},
        key=key,
        timeout=15.0,
    )
    return "ok"
