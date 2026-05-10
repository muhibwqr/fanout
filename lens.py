"""Lens layer — NLA-grounded quality filter for fanout worker outputs.

Applies the Anthropic NLA paper's heuristics at the API level (no real activations):
  - Themes faithful, specifics drift  -> bucket by specificity, verify specifics.
  - True claims recur                  -> count cross-token / cross-worker recurrence.
  - AR partially distinguishes T/F     -> Claude reconstruction-as-verifier.
  - Read for themes, corroborate       -> auditor-reducer report structure.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass, field
from typing import List, Literal, Optional, Tuple

# Imports of LLM-call helpers are deferred (used only by async functions).

Specificity = Literal["theme", "entity", "detail"]
Grounded = Literal["yes", "partial", "no"]
Reconstruction = Literal["supported", "partial", "unsupported"]
Trust = Literal["high", "med", "low"]


@dataclass
class Claim:
    id: str
    worker_id: int
    text: str
    specificity: Specificity
    cited_files: List[str] = field(default_factory=list)
    cited_lines: List[Tuple[str, int]] = field(default_factory=list)
    raw_span: str = ""


@dataclass
class ClaimScore:
    claim_id: str
    grounded: Grounded
    recurrence_in_worker: int
    recurrence_across_workers: int
    reconstruction: Reconstruction
    trust: Trust
    rationale: str


@dataclass
class WorkerLens:
    worker_id: int
    title: str
    fake_success_score: float
    grounded_specific_density: float
    theme_only_ratio: float
    high_count: int
    med_count: int
    low_count: int
    self_report: str
    flagged_for_retry: bool


@dataclass
class LensReport:
    plan: dict
    bundle: dict
    workers: List[WorkerLens]
    claims: List[Claim]
    scores: dict
    high_trust: List[Claim]
    med_trust: List[Claim]
    low_trust: List[Claim]
    suspect: List[Claim]


# ---------------------------------------------------------------------------
# Ground-truth check
# ---------------------------------------------------------------------------


def _excerpt_line_count(excerpt: str) -> int:
    return excerpt.count("\n") + (1 if excerpt else 0)


def ground_check(claim: Claim, bundle: dict) -> Grounded:
    """Verify the claim's path/line citations against the bundle."""
    if claim.specificity == "theme" and not claim.cited_files and not claim.cited_lines:
        return "partial"  # themes don't need grounding; treat as soft pass

    bundle_files = {f["path"]: f for f in bundle.get("files", [])}
    if not bundle_files and (claim.cited_files or claim.cited_lines):
        return "no"

    paths_to_check = list(claim.cited_files)
    for p, _ in claim.cited_lines:
        if p not in paths_to_check:
            paths_to_check.append(p)

    if not paths_to_check:
        return "partial"

    missing = [p for p in paths_to_check if p not in bundle_files]
    if missing:
        return "no"

    for path, line in claim.cited_lines:
        excerpt = bundle_files[path].get("excerpt", "")
        if line < 1 or line > _excerpt_line_count(excerpt):
            return "no"

    if claim.cited_lines:
        return "yes"
    return "partial"


# ---------------------------------------------------------------------------
# Recurrence (cross-token / cross-worker)
# ---------------------------------------------------------------------------


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "but", "is", "are", "was", "were",
        "of", "in", "on", "at", "to", "for", "with", "by", "from", "as",
        "this", "that", "these", "those", "it", "its", "be", "been",
        "has", "have", "had", "not", "no", "so", "if", "then", "than",
        "do", "does", "did", "can", "could", "should", "would", "may",
        "will", "shall", "must", "ought", "i", "you", "we", "they",
        "very", "just", "any", "some", "all", "each", "more", "less",
    }
)


def _tokens(text: str) -> set:
    return {
        t.lower()
        for t in _TOKEN_RE.findall(text)
        if t.lower() not in _STOPWORDS and len(t) > 2
    }


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = a & b
    union = a | b
    return len(inter) / len(union)


def _same_claim(a: Claim, b: Claim, threshold: float = 0.4) -> bool:
    return _jaccard(_tokens(a.text), _tokens(b.text)) >= threshold


def count_recurrence(claim: Claim, all_claims: List[Claim]) -> Tuple[int, int]:
    """Return (within_worker, across_workers) recurrence counts.

    within_worker = number of *other* same-worker claims that match (>=0).
                    Caller can add 1 if they want to include the claim itself.
    across_workers = number of distinct other workers raising the same claim.
    """
    within = 0
    across_ids = set()
    for other in all_claims:
        if other.id == claim.id:
            continue
        if not _same_claim(claim, other):
            continue
        if other.worker_id == claim.worker_id:
            within += 1
        else:
            across_ids.add(other.worker_id)
    return within, len(across_ids)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_claim(
    claim: Claim,
    grounded: Grounded,
    in_worker: int,
    across_workers: int,
    reconstruction: Reconstruction,
) -> ClaimScore:
    """Pure rule table mapping to High/Med/Low trust."""
    spec = claim.specificity
    recurs = in_worker >= 1 or across_workers >= 1

    # Hard rejects.
    if grounded == "no" and reconstruction == "unsupported":
        trust: Trust = "low"
        rationale = "ungrounded specific with no support in cited content"
    elif grounded == "no" and spec != "theme":
        trust = "low"
        rationale = "specific cites paths/lines absent from bundle"
    elif reconstruction == "unsupported" and spec != "theme":
        trust = "low"
        rationale = "claim contradicts or has no support in cited content"
    # High-trust paths.
    elif spec == "theme" and reconstruction in ("supported", "partial"):
        trust = "high"
        rationale = "thematic claim consistent with content"
    elif spec == "entity" and grounded in ("yes", "partial") and reconstruction == "supported":
        trust = "high"
        rationale = "named entity grounded and supported"
    elif (
        spec == "detail"
        and grounded == "yes"
        and reconstruction == "supported"
        and recurs
    ):
        trust = "high"
        rationale = "specific detail grounded, supported, and recurring"
    elif (
        spec == "detail"
        and grounded == "yes"
        and reconstruction == "supported"
    ):
        trust = "med"
        rationale = "specific detail grounded and supported but singleton"
    else:
        trust = "med"
        rationale = "partial support — verify before acting"

    return ClaimScore(
        claim_id=claim.id,
        grounded=grounded,
        recurrence_in_worker=in_worker,
        recurrence_across_workers=across_workers,
        reconstruction=reconstruction,
        trust=trust,
        rationale=rationale,
    )


def _is_specific(claim: Claim) -> bool:
    return claim.specificity in ("entity", "detail")


def score_worker(
    worker_id: int,
    title: str,
    self_report: str,
    claims: List[Claim],
    scores: dict,
) -> WorkerLens:
    """Aggregate per-worker stats; flag fake-success."""
    total = len(claims)
    if total == 0:
        return WorkerLens(
            worker_id=worker_id,
            title=title,
            fake_success_score=1.0,
            grounded_specific_density=0.0,
            theme_only_ratio=1.0,
            high_count=0,
            med_count=0,
            low_count=0,
            self_report=self_report,
            flagged_for_retry=True,
        )

    specifics = [c for c in claims if _is_specific(c)]
    grounded_specifics = [
        c for c in specifics if scores[c.id].grounded in ("yes", "partial")
    ]
    grounded_specific_density = (
        len(grounded_specifics) / len(specifics) if specifics else 0.0
    )
    themes = [c for c in claims if c.specificity == "theme"]
    theme_only_ratio = len(themes) / total

    high_count = sum(1 for c in claims if scores[c.id].trust == "high")
    med_count = sum(1 for c in claims if scores[c.id].trust == "med")
    low_count = sum(1 for c in claims if scores[c.id].trust == "low")

    high_specifics = sum(
        1 for c in specifics if scores[c.id].trust == "high"
    )
    fake_success_score = 1.0 - (high_specifics / total)
    fake_success_score = max(0.0, min(1.0, fake_success_score))

    flagged = (
        fake_success_score > 0.7
        and total < 5
        and theme_only_ratio > 0.6
    ) or (
        len(specifics) >= 3 and grounded_specific_density < 0.4
    )

    return WorkerLens(
        worker_id=worker_id,
        title=title,
        fake_success_score=round(fake_success_score, 3),
        grounded_specific_density=round(grounded_specific_density, 3),
        theme_only_ratio=round(theme_only_ratio, 3),
        high_count=high_count,
        med_count=med_count,
        low_count=low_count,
        self_report=self_report,
        flagged_for_retry=flagged,
    )


# ---------------------------------------------------------------------------
# Self-report parsing
# ---------------------------------------------------------------------------


_LENS_BLOCK_RE = re.compile(r"<lens>(.*?)</lens>", re.S | re.I)


def parse_lens_block(worker_output: str) -> str:
    """Extract the worker's <lens>...</lens> meta-report. Returns '' if absent."""
    m = _LENS_BLOCK_RE.search(worker_output)
    return m.group(1).strip() if m else ""


def strip_lens_block(worker_output: str) -> str:
    """Return worker output with the <lens> block removed (so the reducer doesn't see it twice)."""
    return _LENS_BLOCK_RE.sub("", worker_output).rstrip()


# ---------------------------------------------------------------------------
# Bucketing + report assembly (sync part)
# ---------------------------------------------------------------------------


def bucket_claims(
    claims: List[Claim], scores: dict
) -> Tuple[List[Claim], List[Claim], List[Claim], List[Claim]]:
    high, med, low, suspect = [], [], [], []
    for c in claims:
        s = scores[c.id]
        if s.trust == "high":
            high.append(c)
        elif s.trust == "med":
            med.append(c)
        else:
            low.append(c)
            if s.grounded == "no":
                suspect.append(c)
    return high, med, low, suspect


def report_to_dict(report: LensReport) -> dict:
    """Serialize LensReport (handle non-trivial dataclass nesting + dict claim_id keys)."""
    return {
        "plan": report.plan,
        "workers": [asdict(w) for w in report.workers],
        "claims": [asdict(c) for c in report.claims],
        "scores": {k: asdict(v) for k, v in report.scores.items()},
        "high_trust": [asdict(c) for c in report.high_trust],
        "med_trust": [asdict(c) for c in report.med_trust],
        "low_trust": [asdict(c) for c in report.low_trust],
        "suspect": [asdict(c) for c in report.suspect],
    }


# ---------------------------------------------------------------------------
# Async parts (extraction, reconstruction, top-level lens_pass)
# ---------------------------------------------------------------------------


async def extract_claims(worker_output: str, worker_id: int) -> List[Claim]:
    """Run the LENS_EXTRACTOR_SYSTEM Claude call; parse JSON array of claims."""
    from prompts import LENS_EXTRACTOR_SYSTEM
    from workers import call_claude, parse_claude_envelope

    body = strip_lens_block(worker_output)
    if not body.strip():
        return []
    raw = await call_claude(body, system=LENS_EXTRACTOR_SYSTEM)
    inner = parse_claude_envelope(raw)
    from fanout import extract_json  # local import to avoid cycle

    obj = extract_json(inner)
    if not isinstance(obj, list):
        return []

    out: List[Claim] = []
    for idx, item in enumerate(obj):
        if not isinstance(item, dict):
            continue
        spec = item.get("specificity")
        if spec not in ("theme", "entity", "detail"):
            continue
        cited_lines_raw = item.get("cited_lines", []) or []
        cited_lines = []
        for entry in cited_lines_raw:
            if (
                isinstance(entry, list)
                and len(entry) == 2
                and isinstance(entry[0], str)
                and isinstance(entry[1], int)
            ):
                cited_lines.append((entry[0], entry[1]))
        out.append(
            Claim(
                id=f"W{worker_id}-C{idx + 1}",
                worker_id=worker_id,
                text=str(item.get("text", "")).strip(),
                specificity=spec,
                cited_files=[
                    p for p in (item.get("cited_files", []) or []) if isinstance(p, str)
                ],
                cited_lines=cited_lines,
                raw_span=str(item.get("raw_span", ""))[:500],
            )
        )
    return out


async def reconstruction_check(claim: Claim, bundle: dict) -> Reconstruction:
    """Run LENS_RECONSTRUCTOR_SYSTEM with cited file content. Default lenient on themes."""
    from prompts import LENS_RECONSTRUCTOR_SYSTEM
    from workers import call_claude, parse_claude_envelope

    bundle_files = {f["path"]: f for f in bundle.get("files", [])}
    cited_content = []
    for p in claim.cited_files:
        if p in bundle_files:
            cited_content.append(
                {"path": p, "excerpt": bundle_files[p].get("excerpt", "")[:4000]}
            )
    if not cited_content and claim.specificity == "theme":
        return "supported"  # theme with no specific path doesn't need reconstruction
    if not cited_content:
        return "unsupported"

    payload = json.dumps(
        {
            "claim": claim.text,
            "specificity": claim.specificity,
            "cited_lines": [list(t) for t in claim.cited_lines],
            "cited_content": cited_content,
        },
        indent=2,
    )
    try:
        raw = await call_claude(payload, system=LENS_RECONSTRUCTOR_SYSTEM)
    except Exception:
        return "partial"
    inner = parse_claude_envelope(raw)
    from fanout import extract_json

    try:
        obj = extract_json(inner)
    except Exception:
        return "partial"
    if not isinstance(obj, dict):
        return "partial"
    verdict = obj.get("verdict")
    if verdict in ("supported", "partial", "unsupported"):
        return verdict
    return "partial"


async def lens_pass(
    plan: dict,
    bundle: dict,
    results: List[dict],
    *,
    skip_reconstruction: bool = False,
) -> LensReport:
    """Top-level lens orchestrator.

    Steps:
      1. Extract claims per worker (parallel).
      2. Ground-check each claim (sync, cheap).
      3. Reconstruction-check each claim (parallel, optionally skipped for cheap pass).
      4. Compute recurrence.
      5. Score each claim.
      6. Aggregate per-worker stats; bucket claims.
    """
    extract_coros = [
        extract_claims(r["output"], r["id"])
        for r in results
        if not r.get("error")
    ]
    extracted_lists = await asyncio.gather(*extract_coros, return_exceptions=True)
    all_claims: List[Claim] = []
    for sub in extracted_lists:
        if isinstance(sub, Exception):
            continue
        all_claims.extend(sub)

    # Ground checks (sync).
    ground_map = {c.id: ground_check(c, bundle) for c in all_claims}

    # Reconstruction (async, batched).
    if skip_reconstruction:
        recon_map = {c.id: ("supported" if c.specificity == "theme" else "partial") for c in all_claims}
    else:
        recon_results = await asyncio.gather(
            *(reconstruction_check(c, bundle) for c in all_claims),
            return_exceptions=True,
        )
        recon_map = {}
        for c, r in zip(all_claims, recon_results):
            if isinstance(r, Exception):
                recon_map[c.id] = "partial"
            else:
                recon_map[c.id] = r

    # Score.
    scores: dict = {}
    for c in all_claims:
        in_w, across = count_recurrence(c, all_claims)
        scores[c.id] = score_claim(c, ground_map[c.id], in_w, across, recon_map[c.id])

    # Per-worker aggregation.
    workers: List[WorkerLens] = []
    for r in results:
        wid = r["id"]
        title = r.get("title", "")
        self_report = parse_lens_block(r.get("output", ""))
        worker_claims = [c for c in all_claims if c.worker_id == wid]
        if r.get("error") or not worker_claims:
            workers.append(
                WorkerLens(
                    worker_id=wid,
                    title=title,
                    fake_success_score=1.0,
                    grounded_specific_density=0.0,
                    theme_only_ratio=0.0,
                    high_count=0,
                    med_count=0,
                    low_count=0,
                    self_report=self_report,
                    flagged_for_retry=True,
                )
            )
            continue
        workers.append(
            score_worker(wid, title, self_report, worker_claims, scores)
        )

    # Bucket.
    high, med, low, suspect = bucket_claims(all_claims, scores)

    return LensReport(
        plan=plan,
        bundle={"files": [{"path": f["path"]} for f in bundle.get("files", [])]},
        workers=workers,
        claims=all_claims,
        scores=scores,
        high_trust=high,
        med_trust=med,
        low_trust=low,
        suspect=suspect,
    )
