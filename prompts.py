"""System prompts and JSON schema for the planner + reducer."""
from __future__ import annotations

PLANNER_SYSTEM = """You decompose a user command into N independent subtasks.
You receive: command, N, mode, context_bundle (repo map + file digests + refs).

Modes:
- scratch:    inspiration refs only; no existing code constraints. Bias toward by_hypothesis or by_phase.
- extend:     existing repo + files + refs. Bias toward by_file or by_dimension. Each subtask must cite read_files from the repo map.
- greenfield: command only, no context. Default by_hypothesis.

Output ONLY valid JSON:
{"n": int, "mode": str, "strategy": str,
 "subtasks": [{"id": int, "title": str, "instructions": str,
               "read_files": [str], "refs": [str], "expected_output": str}],
 "merge_plan": "concat"|"vote"|"rank"|"synthesize"}

Rules:
- exactly N subtasks, ids 1..N
- subtasks independent: no worker reads another's output
- in extend mode, read_files must be a subset of the provided repo map; for by_file strategy no overlap > 1 file across subtasks
- in greenfield/scratch mode, read_files must be empty
- instructions self-contained: a fresh agent acts with only its read_files + refs
- pick the strategy that maximises parallelism for THIS command + mode

Respond with ONLY the JSON object. No prose, no markdown fences, no preamble.
"""

REDUCER_SYSTEM = """You are the reducer for a multi-agent fanout. You receive: the original command, the plan, and N worker outputs.

Synthesize a single coherent answer following the plan's merge_plan strategy:
- concat:    one section per worker, headed by the worker's title.
- rank:      rank workers' outputs by quality + relevance, explain the ranking, then present the top result with brief notes on the others.
- vote:      identify the consensus answer across workers; report disagreements explicitly.
- synthesize: integrate findings into a unified report. Cross-reference, deduplicate, resolve conflicts, surface tensions.

Cite worker IDs (W1, W2, ...) for traceability.
If a worker output begins with "[ERROR]", note it but continue with the partial result.

Output: clean markdown. No JSON envelope, no preamble.
"""


def planner_schema(n: int) -> dict:
    """JSON Schema for the planner output, parameterised by N."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["n", "mode", "strategy", "subtasks", "merge_plan"],
        "properties": {
            "n": {"type": "integer", "minimum": 1, "maximum": 10},
            "mode": {"type": "string", "enum": ["scratch", "extend", "greenfield"]},
            "strategy": {
                "type": "string",
                "enum": ["by_file", "by_dimension", "by_phase", "by_hypothesis"],
            },
            "merge_plan": {
                "type": "string",
                "enum": ["concat", "vote", "rank", "synthesize"],
            },
            "subtasks": {
                "type": "array",
                "minItems": n,
                "maxItems": n,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "id",
                        "title",
                        "instructions",
                        "read_files",
                        "refs",
                        "expected_output",
                    ],
                    "properties": {
                        "id": {"type": "integer", "minimum": 1},
                        "title": {"type": "string"},
                        "instructions": {"type": "string"},
                        "read_files": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "refs": {"type": "array", "items": {"type": "string"}},
                        "expected_output": {"type": "string"},
                    },
                },
            },
        },
    }
