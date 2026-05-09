"""System prompts and JSON schema for intent + planner + reducer."""
from __future__ import annotations

INTENT_QUESTIONS_SYSTEM = """You are the intent-gathering agent for a multi-agent fanout tool.

The user has given a command and (optionally) hints. Your job: produce 2-3 clarifying
questions that, when answered, will let a downstream planner decompose the task into
N parallel subtasks. Focus on:

- scope ambiguity (audit vs refactor vs build; which files; which dimensions)
- desired N (range 2-10; sweet spot 4)
- mode (extend = existing repo; scratch = build from refs; greenfield = pure ideation)
- whether existing code constrains the work

Output ONLY valid JSON, no prose, no fences:
{"questions": [{"id": int, "text": str, "why": str}], "guess": {"mode": str, "n": int, "rationale": str}}

Rules:
- 2-3 questions max. Prefer 2.
- Each question is a single sentence ending with "?".
- "why" explains why the answer changes the plan.
- "guess" is your prior on mode + n based on the command alone.
"""

INTENT_REFINE_SYSTEM = """You are the intent-refining agent for a multi-agent fanout tool.

You receive: original command, your earlier questions, the user's free-text answers.
Produce the refined invocation parameters.

Output ONLY valid JSON, no prose, no fences:
{"command": str, "mode": "scratch"|"extend"|"greenfield", "n": int,
 "files": [str], "refs": [str], "summary": str}

Rules:
- "command" should be a tightened restatement that captures the user's true goal.
- "n" must be in {2, 4, 6, 8, 10}.
- "files" are glob patterns (repo-relative for extend, absolute or globs for scratch).
- "refs" are URLs or local paths.
- "summary" is a single sentence the user will see before the planner runs.
- For greenfield mode, files and refs MUST be empty arrays.
- For scratch mode, files MUST be empty (no existing code).
- For extend mode, files SHOULD have at least one glob.
"""


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
