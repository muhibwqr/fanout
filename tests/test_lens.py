"""Tests for lens.py — pure-python parts (no LLM calls)."""
from __future__ import annotations

import pathlib

import pytest

from lens import (
    Claim,
    bucket_claims,
    count_recurrence,
    ground_check,
    parse_lens_block,
    score_claim,
    score_worker,
    strip_lens_block,
)

FIXTURES = pathlib.Path(__file__).parent / "sample_outputs"


def make_bundle():
    """Bundle that mirrors the fixture sample_repo, with line counts ~ realistic."""
    return {
        "files": [
            {
                "path": "src/auth/middleware.py",
                "abs_path": "/x/src/auth/middleware.py",
                "symbols": ["def authenticate", "def verify_token", "class AuthMiddleware"],
                "excerpt": "\n".join(f"line{i}" for i in range(1, 21)),  # 20 lines
            },
            {
                "path": "src/auth/tokens.py",
                "abs_path": "/x/src/auth/tokens.py",
                "symbols": ["def issue_token", "def revoke_token", "class TokenStore"],
                "excerpt": "\n".join(f"line{i}" for i in range(1, 16)),  # 15 lines
            },
            {
                "path": "tests/auth/test_middleware.py",
                "abs_path": "/x/tests/auth/test_middleware.py",
                "symbols": ["def test_authenticate_no_header"],
                "excerpt": "\n".join(f"line{i}" for i in range(1, 8)),  # 7 lines
            },
        ],
        "refs": [],
    }


def make_claim(
    cid="W1-C1",
    wid=1,
    text="thematic claim",
    spec="theme",
    files=None,
    lines=None,
):
    return Claim(
        id=cid,
        worker_id=wid,
        text=text,
        specificity=spec,
        cited_files=list(files or []),
        cited_lines=list(lines or []),
        raw_span=text,
    )


# ---------- ground_check ----------


def test_ground_check_path_in_bundle():
    b = make_bundle()
    c = make_claim(spec="entity", files=["src/auth/tokens.py"])
    assert ground_check(c, b) == "partial"


def test_ground_check_path_not_in_bundle():
    b = make_bundle()
    c = make_claim(spec="entity", files=["src/auth/oauth.py"])
    assert ground_check(c, b) == "no"


def test_ground_check_line_out_of_range():
    b = make_bundle()
    c = make_claim(
        spec="detail",
        files=["src/auth/tokens.py"],
        lines=[("src/auth/tokens.py", 9999)],
    )
    assert ground_check(c, b) == "no"


def test_ground_check_line_in_range():
    b = make_bundle()
    c = make_claim(
        spec="detail",
        files=["src/auth/tokens.py"],
        lines=[("src/auth/tokens.py", 6)],
    )
    assert ground_check(c, b) == "yes"


def test_ground_check_theme_no_files_partial():
    b = make_bundle()
    c = make_claim(spec="theme", text="auth needs better error handling")
    assert ground_check(c, b) == "partial"


def test_ground_check_line_path_inferred_from_lines_only():
    b = make_bundle()
    c = make_claim(
        spec="detail",
        files=[],
        lines=[("src/auth/oauth.py", 42)],
    )
    assert ground_check(c, b) == "no"


# ---------- recurrence ----------


def test_recurrence_within_worker():
    a = make_claim(cid="W1-C1", wid=1, text="parameterized queries prevent injection")
    b = make_claim(cid="W1-C2", wid=1, text="parameterized queries prevent SQL")
    c = make_claim(cid="W1-C3", wid=1, text="parameterized queries injection prevention")
    inw, across = count_recurrence(a, [a, b, c])
    assert inw == 2
    assert across == 0


def test_recurrence_across_workers():
    a = make_claim(cid="W1-C1", wid=1, text="parameterized queries prevent injection")
    b = make_claim(cid="W2-C1", wid=2, text="parameterized queries are recommended")
    c = make_claim(cid="W3-C1", wid=3, text="parameterized queries are recommended")
    inw, across = count_recurrence(a, [a, b, c])
    assert inw == 0
    assert across == 2


def test_recurrence_singleton():
    a = make_claim(cid="W1-C1", wid=1, text="aardvarks are mammals")
    b = make_claim(cid="W2-C1", wid=2, text="cars need fuel")
    inw, across = count_recurrence(a, [a, b])
    assert inw == 0
    assert across == 0


def test_recurrence_jaccard_threshold():
    """Different surface forms should still match if token overlap >= 0.6."""
    a = make_claim(cid="W1-C1", wid=1, text="token revocation is missing entirely")
    b = make_claim(cid="W2-C1", wid=2, text="missing token revocation")
    inw, across = count_recurrence(a, [a, b])
    assert across == 1


# ---------- score_claim ----------


def test_score_theme_supported_high():
    c = make_claim(spec="theme")
    s = score_claim(c, "partial", 0, 0, "supported")
    assert s.trust == "high"


def test_score_entity_grounded_supported_high():
    c = make_claim(spec="entity", files=["src/auth/tokens.py"])
    s = score_claim(c, "partial", 0, 0, "supported")
    assert s.trust == "high"


def test_score_detail_grounded_supported_recurring_high():
    c = make_claim(spec="detail")
    s = score_claim(c, "yes", 1, 0, "supported")
    assert s.trust == "high"


def test_score_detail_grounded_supported_singleton_med():
    c = make_claim(spec="detail")
    s = score_claim(c, "yes", 0, 0, "supported")
    assert s.trust == "med"


def test_score_unsupported_low():
    c = make_claim(spec="entity", files=["src/auth/oauth.py"])
    s = score_claim(c, "no", 0, 0, "unsupported")
    assert s.trust == "low"


def test_score_ungrounded_specific_low():
    c = make_claim(spec="entity", files=["src/auth/oauth.py"])
    s = score_claim(c, "no", 0, 0, "supported")
    assert s.trust == "low"


# ---------- score_worker / fake-success ----------


def _build_scored(claims, score_table):
    """score_table: dict claim_id -> (grounded, recon, in_w, across)."""
    scores = {}
    for c in claims:
        g, r, inw, across = score_table[c.id]
        scores[c.id] = score_claim(c, g, inw, across, r)
    return scores


def test_score_worker_flags_fake_success_themes_only():
    claims = [
        make_claim(cid=f"W1-C{i}", wid=1, text=f"theme {i}", spec="theme")
        for i in range(3)
    ]
    scores = _build_scored(
        claims, {c.id: ("partial", "supported", 0, 0) for c in claims}
    )
    w = score_worker(1, "fake worker", "", claims, scores)
    assert w.flagged_for_retry is True
    assert w.fake_success_score >= 0.7
    assert w.theme_only_ratio == 1.0


def test_score_worker_does_not_flag_grounded_dense():
    claims = [
        make_claim(cid="W1-C1", wid=1, spec="theme"),
        make_claim(
            cid="W1-C2",
            wid=1,
            spec="detail",
            files=["src/auth/tokens.py"],
            lines=[("src/auth/tokens.py", 6)],
        ),
        make_claim(
            cid="W1-C3",
            wid=1,
            spec="entity",
            files=["src/auth/middleware.py"],
        ),
        make_claim(
            cid="W1-C4",
            wid=1,
            spec="detail",
            files=["src/auth/middleware.py"],
            lines=[("src/auth/middleware.py", 5)],
        ),
    ]
    scores = _build_scored(
        claims,
        {
            "W1-C1": ("partial", "supported", 0, 0),
            "W1-C2": ("yes", "supported", 1, 0),
            "W1-C3": ("partial", "supported", 0, 0),
            "W1-C4": ("yes", "supported", 0, 1),
        },
    )
    w = score_worker(1, "good worker", "", claims, scores)
    assert w.flagged_for_retry is False
    assert w.high_count >= 3
    assert w.fake_success_score < 0.7


def test_score_worker_flags_low_grounded_density():
    claims = [
        make_claim(
            cid=f"W1-C{i}",
            wid=1,
            spec="detail",
            files=[f"src/fake/file{i}.py"],
            lines=[(f"src/fake/file{i}.py", 99)],
        )
        for i in range(4)
    ]
    scores = _build_scored(
        claims, {c.id: ("no", "unsupported", 0, 0) for c in claims}
    )
    w = score_worker(1, "confab worker", "", claims, scores)
    assert w.flagged_for_retry is True
    assert w.grounded_specific_density < 0.4


def test_score_worker_zero_claims_flagged():
    w = score_worker(1, "empty", "", [], {})
    assert w.flagged_for_retry is True
    assert w.fake_success_score == 1.0


# ---------- bucketing ----------


def test_bucket_claims_partition():
    c1 = make_claim(cid="A", spec="theme")
    c2 = make_claim(cid="B", spec="detail", files=["src/auth/tokens.py"])
    c3 = make_claim(cid="C", spec="entity", files=["src/auth/oauth.py"])
    scores = {
        "A": score_claim(c1, "partial", 0, 0, "supported"),
        "B": score_claim(c2, "yes", 1, 0, "supported"),
        "C": score_claim(c3, "no", 0, 0, "unsupported"),
    }
    high, med, low, suspect = bucket_claims([c1, c2, c3], scores)
    assert c1 in high
    assert c2 in high
    assert c3 in low
    assert c3 in suspect


# ---------- lens block parser ----------


def test_parse_lens_block_present():
    out = "# Findings\n\nbody\n\n<lens>\nconfidence: H\nspeculative: none\n</lens>\n"
    parsed = parse_lens_block(out)
    assert "confidence: H" in parsed


def test_parse_lens_block_absent():
    out = "# Findings\nbody only"
    assert parse_lens_block(out) == ""


def test_strip_lens_block():
    out = "# Findings\nbody\n<lens>\nmeta\n</lens>\nmore"
    stripped = strip_lens_block(out)
    assert "<lens>" not in stripped
    assert "body" in stripped
    assert "more" in stripped


# ---------- fixture worker outputs ----------


def test_fixture_grounded_security_exists():
    p = FIXTURES / "grounded_security.md"
    assert p.exists()
    text = p.read_text()
    assert "src/auth/tokens.py" in text
    assert "src/auth/middleware.py" in text


def test_fixture_confabulated_oauth_cites_nonexistent_paths():
    text = (FIXTURES / "confabulated_oauth.md").read_text()
    # These paths don't exist in the fixture sample_repo bundle.
    assert "src/auth/oauth.py" in text
    assert "src/web/login.js" in text


def test_fixture_fake_success_themes_no_specifics():
    text = (FIXTURES / "fake_success_themes.md").read_text()
    # No file:line citations in the fake-success output.
    import re

    assert not re.search(r"\.py:\d+", text)
