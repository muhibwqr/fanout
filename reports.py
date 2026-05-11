"""HTML reports for every fanout subcommand.

Inspired by Thariq's "Unreasonable Effectiveness of HTML" — each command emits
a self-contained HTML artifact, written to ~/.fanout/reports/, optionally opened
in the browser. Markdown is for diffs; HTML is for humans.
"""
from __future__ import annotations

import datetime as _dt
import html
import json
import os
import pathlib
import subprocess
import sys
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from manifest import ADAPTER_BUCKETS, CurlItem


REPORTS_DIR = pathlib.Path(os.path.expanduser("~/.fanout/reports"))


# ---------------------------------------------------------------------------
# Common shell — embedded CSS, header, footer.
# ---------------------------------------------------------------------------


_CSS = """
:root {
  --bg: #f4ecd8;
  --bg-soft: #ede4cd;
  --bg-card: #faf3e0;
  --ink: #1a1a1a;
  --ink-soft: #2a2722;
  --muted: #5a5650;
  --dim: #8a857c;
  --rule: #d8cfb8;
  --rule-strong: #b8ad94;
  --accent: #7a3b2e;
  --accent2: #2c4a6b;
  --success: #3d6b3a;
  --danger: #963a2c;
  --warn: #8a6b2a;
}
* { box-sizing: border-box; }
html, body { background: var(--bg); color: var(--ink); margin: 0; }
body {
  font-family: "Apple Garamond", "EB Garamond", Garamond, "Times New Roman", Arial, serif;
  font-size: 17px;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}
main { max-width: 840px; margin: 0 auto; padding: 56px 28px 96px; }

header.hero { padding: 0 0 28px; border-bottom: 1px solid var(--rule); margin-bottom: 36px; }
h1 { font-weight: 600; font-size: 34px; line-height: 1.15; letter-spacing: -0.01em; margin: 0 0 12px; }
h2 { font-weight: 600; font-size: 22px; margin: 44px 0 12px; }
h3 { font-weight: 600; font-size: 17px; margin: 24px 0 8px; }
.byline { font-family: Arial, sans-serif; font-size: 11px; letter-spacing: 0.08em; color: var(--muted); text-transform: uppercase; margin: 6px 0 0; }
.tags { margin: 12px 0 0; font-family: Arial, sans-serif; font-size: 12px; color: var(--muted); letter-spacing: 0.04em; }
.tags span { margin-right: 14px; }
.tags span::before { content: "·"; margin-right: 6px; color: var(--rule-strong); }
.tags span:first-child::before { content: ""; margin: 0; }

p, li { color: var(--ink-soft); }
hr { border: none; border-top: 1px solid var(--rule); margin: 36px 0; }
.footnote { color: var(--muted); font-size: 13px; font-style: italic; margin-top: 32px; }

code, pre, kbd, samp { font-family: "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace; }
pre {
  background: var(--bg-card);
  border: 1px solid var(--rule);
  border-radius: 4px;
  padding: 12px 14px;
  overflow-x: auto;
  font-size: 12.5px;
  line-height: 1.55;
}
code.inline {
  background: var(--bg-card);
  padding: 1px 6px;
  border: 1px solid var(--rule);
  border-radius: 3px;
  font-size: 13.5px;
  color: var(--accent);
}

table { width: 100%; border-collapse: collapse; margin: 12px 0; font-family: Arial, sans-serif; font-size: 13.5px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--rule); vertical-align: top; }
thead th { border-bottom: 1px solid var(--ink); font-weight: 700; font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--ink); }

.bucket {
  background: var(--bg-card);
  border: 1px solid var(--rule);
  border-radius: 4px;
  padding: 14px 18px;
  margin: 14px 0;
}
.bucket h3 { margin-top: 0; color: var(--accent); font-family: "SF Mono", Menlo, monospace; font-size: 14px; letter-spacing: 0.04em; }
.bucket .count { color: var(--muted); font-family: "SF Mono", Menlo, monospace; font-size: 11px; margin-left: 8px; font-weight: 400; letter-spacing: 0.02em; }

.items { list-style: none; padding: 0; margin: 6px 0 0; }
.items li {
  padding: 4px 0;
  font-family: "SF Mono", "JetBrains Mono", Menlo, monospace;
  font-size: 13px;
  color: var(--ink);
  border-bottom: 1px dotted var(--rule);
}
.items li:last-child { border-bottom: none; }
.items li.install::before  { content: "+ "; color: var(--success); font-weight: 700; }
.items li.remove::before   { content: "− "; color: var(--danger);  font-weight: 700; }
.items li.unchanged::before{ content: "= "; color: var(--muted);   font-weight: 700; }
.items li.warn::before     { content: "! "; color: var(--warn);    font-weight: 700; }
.items li.ok::before       { content: "✓ "; color: var(--success); font-weight: 700; }
.items li.fail::before     { content: "✗ "; color: var(--danger);  font-weight: 700; }

.summary {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 14px;
  margin: 20px 0;
}
.summary .card {
  background: var(--bg-card);
  border: 1px solid var(--rule);
  border-radius: 4px;
  padding: 14px 16px;
}
.summary .card .label { font-family: Arial, sans-serif; font-size: 10px; letter-spacing: 0.1em; color: var(--muted); text-transform: uppercase; }
.summary .card .value { font-family: "SF Mono", Menlo, monospace; font-size: 22px; font-weight: 600; margin-top: 4px; }
.summary .card.good .value { color: var(--success); }
.summary .card.bad .value  { color: var(--danger); }
.summary .card.warn .value { color: var(--warn); }

.log { background: var(--bg-card); border: 1px solid var(--rule); border-left: 2px solid var(--ink); padding: 10px 14px; margin: 10px 0; font-family: "SF Mono", Menlo, monospace; font-size: 12px; white-space: pre-wrap; color: var(--ink-soft); }
.log.fail { border-left-color: var(--danger); }

.pull { border-left: 2px solid var(--accent); padding: 4px 0 4px 14px; margin: 18px 0; color: var(--ink); font-style: italic; }

.empty { color: var(--muted); font-style: italic; padding: 12px 0; }
"""


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ts_slug() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")


def _shell(title: str, body: str, byline: str = "fanout report") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<main>
<header class="hero">
<p class="byline">{html.escape(byline)}</p>
<h1>{html.escape(title)}</h1>
<p class="tags"><span>{_now()}</span><span>github.com/muhibwqr/fanout</span></p>
</header>
{body}
<hr>
<p class="footnote">Generated by fanout. HTML reports inspired by Thariq's <em>Unreasonable Effectiveness of HTML</em>.</p>
</main>
</body>
</html>
"""


def _write_report(slug: str, html_text: str) -> pathlib.Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"{_ts_slug()}-{slug}.html"
    path.write_text(html_text)
    return path


def emit(slug: str, html_text: str, *, open_browser: bool = True) -> pathlib.Path:
    """Write the report; optionally open in browser. Returns the path."""
    path = _write_report(slug, html_text)
    if open_browser:
        try:
            subprocess.run(["open", str(path)], check=False, timeout=5)
        except Exception:
            pass
    return path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _name_of(item) -> str:
    """Extract display name from either a string or a CurlItem."""
    return item.name if isinstance(item, CurlItem) else str(item)


def _bucket_block(bucket: str, diff_entry: dict) -> str:
    installs = diff_entry.get("install", [])
    removes = diff_entry.get("remove", [])
    unchanged = diff_entry.get("unchanged", [])
    if not installs and not removes and not unchanged:
        return ""
    lis = []
    for it in installs:
        lis.append(f'<li class="install">{html.escape(_name_of(it))}</li>')
    for it in removes:
        lis.append(f'<li class="remove">{html.escape(_name_of(it))}</li>')
    for it in unchanged:
        lis.append(f'<li class="unchanged">{html.escape(_name_of(it))}</li>')
    count = (
        f'<span class="count">+{len(installs)} −{len(removes)} ={len(unchanged)}</span>'
    )
    return (
        f'<section class="bucket"><h3>[{html.escape(bucket)}]{count}</h3>'
        f'<ul class="items">{"".join(lis)}</ul></section>'
    )


# ---------------------------------------------------------------------------
# Renderers — one per subcommand.
# ---------------------------------------------------------------------------


def render_plan(profile: str, plan_obj) -> str:
    """plan_obj is engine.PlanResult."""
    parts = [f"<p class=\"byline\">profile: <code class=\"inline\">{html.escape(profile)}</code></p>"]
    if plan_obj.nothing_to_do:
        parts.append('<p class="empty">Nothing to do — manifest matches installed state.</p>')
    else:
        total_install = sum(len(plan_obj.bucket_diffs[b]["install"]) for b in ADAPTER_BUCKETS)
        total_remove = sum(len(plan_obj.bucket_diffs[b]["remove"]) for b in ADAPTER_BUCKETS)
        total_keep = sum(len(plan_obj.bucket_diffs[b]["unchanged"]) for b in ADAPTER_BUCKETS)
        parts.append(
            '<div class="summary">'
            f'<div class="card good"><div class="label">to install</div><div class="value">+{total_install}</div></div>'
            f'<div class="card bad"><div class="label">to remove</div><div class="value">−{total_remove}</div></div>'
            f'<div class="card"><div class="label">unchanged</div><div class="value">={total_keep}</div></div>'
            '</div>'
        )
        for bucket in ADAPTER_BUCKETS:
            parts.append(_bucket_block(bucket, plan_obj.bucket_diffs[bucket]))
    parts.append(
        '<p class="footnote">Run <code class="inline">fanout apply</code> to converge. Append <code class="inline">--dry-run</code> to preview the install commands without running them.</p>'
    )
    return _shell(f"plan — {profile}", "".join(parts), byline="fanout plan")


def render_state(state_obj) -> str:
    """state_obj is state.State."""
    cards = '<div class="summary">'
    for bucket in ADAPTER_BUCKETS:
        n = len(state_obj.owned.get(bucket, []))
        cards += (
            f'<div class="card"><div class="label">{html.escape(bucket)}</div>'
            f'<div class="value">{n}</div></div>'
        )
    cards += '</div>'
    blocks = ""
    for bucket in ADAPTER_BUCKETS:
        items = state_obj.owned.get(bucket, [])
        if not items:
            continue
        lis = "".join(f'<li class="unchanged">{html.escape(str(x))}</li>' for x in items)
        blocks += (
            f'<section class="bucket"><h3>[{html.escape(bucket)}]'
            f'<span class="count">{len(items)} owned</span></h3>'
            f'<ul class="items">{lis}</ul></section>'
        )
    last = html.escape(state_obj.last_apply or "(never applied)")
    snaps = state_obj.snapshots or []
    snap_html = ""
    if snaps:
        rows = "".join(
            f"<tr><td>{html.escape(s.get('id', ''))}</td>"
            f"<td>{html.escape(s.get('label') or '')}</td></tr>"
            for s in snaps
        )
        snap_html = (
            "<h2>Snapshots</h2>"
            "<table><thead><tr><th>id</th><th>label</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    empty_html = '<p class="empty">No owned items yet. Run <code class="inline">fanout apply</code>.</p>'
    body = (
        f'<p class="byline">last apply: <code class="inline">{last}</code></p>'
        f"{cards}{blocks or empty_html}{snap_html}"
    )
    return _shell("state", body, byline="fanout state")


def render_state_diff(drift_reports: list) -> str:
    if not drift_reports:
        body = '<p class="empty">No drift. Owned set matches reality.</p>'
        return _shell("state diff", body, byline="fanout state diff")
    parts = []
    total_added = sum(len(r.added) for r in drift_reports)
    total_removed = sum(len(r.removed) for r in drift_reports)
    parts.append(
        '<div class="summary">'
        f'<div class="card warn"><div class="label">off-manifest installs</div><div class="value">+{total_added}</div></div>'
        f'<div class="card warn"><div class="label">missing</div><div class="value">−{total_removed}</div></div>'
        '</div>'
    )
    for r in drift_reports:
        lis = []
        for a in r.added:
            lis.append(f'<li class="warn">{html.escape(a)} <span style="color: var(--muted); font-size: 11px;">— installed off-manifest</span></li>')
        for rm in r.removed:
            lis.append(f'<li class="warn">{html.escape(rm)} <span style="color: var(--muted); font-size: 11px;">— in state but no longer installed</span></li>')
        parts.append(
            f'<section class="bucket"><h3>[{html.escape(r.bucket)}]'
            f'<span class="count">+{len(r.added)} −{len(r.removed)}</span></h3>'
            f'<ul class="items">{"".join(lis)}</ul></section>'
        )
    parts.append(
        '<p class="footnote">'
        "To reconcile: either <code class=\"inline\">fanout edit</code> to add the off-manifest items to your manifest, "
        "or <code class=\"inline\">fanout apply</code> to remove them (snapshot taken first)."
        "</p>"
    )
    return _shell("state diff — drift detected", "".join(parts), byline="fanout state diff")


def render_apply(profile: str, plan_obj, apply_result, *, dry_run: bool = False) -> str:
    """apply_result is engine.ApplyResult."""
    parts = [f"<p class=\"byline\">profile: <code class=\"inline\">{html.escape(profile)}</code> · dry-run: {dry_run}</p>"]
    total_installed = sum(len(v) for v in apply_result.installed.values())
    total_removed = sum(len(v) for v in apply_result.removed.values())
    total_failed = sum(len(v) for v in apply_result.failures.values())
    parts.append(
        '<div class="summary">'
        f'<div class="card good"><div class="label">installed</div><div class="value">+{total_installed}</div></div>'
        f'<div class="card bad"><div class="label">removed</div><div class="value">−{total_removed}</div></div>'
        f'<div class="card warn"><div class="label">failed</div><div class="value">{total_failed}</div></div>'
        '</div>'
    )
    if apply_result.snapshot_id:
        parts.append(
            f'<p>Snapshot taken before apply: <code class="inline">{html.escape(apply_result.snapshot_id)}</code>. '
            f'Run <code class="inline">fanout rollback</code> to restore.</p>'
        )
    if apply_result.installed:
        parts.append("<h2>Installed</h2>")
        for bucket, items in apply_result.installed.items():
            if not items:
                continue
            lis = "".join(f'<li class="ok">{html.escape(str(x))}</li>' for x in items)
            parts.append(
                f'<section class="bucket"><h3>[{html.escape(bucket)}]'
                f'<span class="count">{len(items)}</span></h3>'
                f'<ul class="items">{lis}</ul></section>'
            )
    if apply_result.removed:
        parts.append("<h2>Removed</h2>")
        for bucket, items in apply_result.removed.items():
            if not items:
                continue
            lis = "".join(f'<li class="remove">{html.escape(str(x))}</li>' for x in items)
            parts.append(
                f'<section class="bucket"><h3>[{html.escape(bucket)}]'
                f'<span class="count">{len(items)}</span></h3>'
                f'<ul class="items">{lis}</ul></section>'
            )
    if apply_result.failures:
        parts.append("<h2>Failures</h2>")
        for bucket, items in apply_result.failures.items():
            if not items:
                continue
            lis = "".join(f'<li class="fail">{html.escape(str(x))}</li>' for x in items)
            parts.append(
                f'<section class="bucket"><h3>[{html.escape(bucket)}]'
                f'<span class="count">{len(items)}</span></h3>'
                f'<ul class="items">{lis}</ul></section>'
            )
    if apply_result.logs:
        parts.append("<h2>Logs</h2>")
        for line in apply_result.logs:
            cls = "log fail" if line.lower().startswith(("[", "error")) and "error" in line.lower() else "log"
            parts.append(f'<div class="{cls}">{html.escape(line)}</div>')
    return _shell(
        f"apply — {profile}" + (" (dry-run)" if dry_run else ""),
        "".join(parts),
        byline="fanout apply",
    )


def render_verify(verify_result) -> str:
    """verify_result is engine.VerifyResult."""
    parts = []
    total = len(verify_result.checks)
    passed = sum(1 for c in verify_result.checks if c["ok"])
    failed = total - passed
    parts.append(
        '<div class="summary">'
        f'<div class="card good"><div class="label">passed</div><div class="value">{passed}</div></div>'
        f'<div class="card bad"><div class="label">failed</div><div class="value">{failed}</div></div>'
        f'<div class="card"><div class="label">total</div><div class="value">{total}</div></div>'
        '</div>'
    )
    if not verify_result.checks:
        parts.append('<p class="empty">No verify checks configured. Add <code class="inline">settings.verify.checks</code> to your manifest.</p>')
    else:
        lis = []
        for c in verify_result.checks:
            cls = "ok" if c["ok"] else "fail"
            cmd = html.escape(c["cmd"])
            extra = ""
            if not c["ok"] and c.get("stderr"):
                extra = f'<div class="log fail">{html.escape(c["stderr"])}</div>'
            lis.append(f'<li class="{cls}"><code>{cmd}</code>{extra}</li>')
        parts.append(f'<section class="bucket"><ul class="items">{"".join(lis)}</ul></section>')
    return _shell("verify", "".join(parts), byline="fanout verify")


def render_rollback(apply_result) -> str:
    parts = []
    total_installed = sum(len(v) for v in apply_result.installed.values())
    total_removed = sum(len(v) for v in apply_result.removed.values())
    parts.append(
        '<div class="summary">'
        f'<div class="card good"><div class="label">re-installed</div><div class="value">+{total_installed}</div></div>'
        f'<div class="card bad"><div class="label">removed</div><div class="value">−{total_removed}</div></div>'
        '</div>'
    )
    if apply_result.installed:
        parts.append("<h2>Re-installed</h2>")
        for bucket, items in apply_result.installed.items():
            if not items:
                continue
            lis = "".join(f'<li class="ok">{html.escape(str(x))}</li>' for x in items)
            parts.append(
                f'<section class="bucket"><h3>[{html.escape(bucket)}]'
                f'<span class="count">{len(items)}</span></h3>'
                f'<ul class="items">{lis}</ul></section>'
            )
    if apply_result.removed:
        parts.append("<h2>Removed</h2>")
        for bucket, items in apply_result.removed.items():
            if not items:
                continue
            lis = "".join(f'<li class="remove">{html.escape(str(x))}</li>' for x in items)
            parts.append(
                f'<section class="bucket"><h3>[{html.escape(bucket)}]'
                f'<span class="count">{len(items)}</span></h3>'
                f'<ul class="items">{lis}</ul></section>'
            )
    if apply_result.logs:
        parts.append("<h2>Logs</h2>")
        for line in apply_result.logs:
            parts.append(f'<div class="log">{html.escape(line)}</div>')
    return _shell("rollback", "".join(parts), byline="fanout rollback")


def render_ai(description: str, yaml_text: str, *, accepted: bool) -> str:
    body = (
        f'<p class="byline">description: <code class="inline">{html.escape(description)}</code></p>'
        f'<p>Status: <b>{"accepted, written to manifest" if accepted else "preview only"}</b></p>'
        '<h2>Generated manifest</h2>'
        f'<pre>{html.escape(yaml_text)}</pre>'
    )
    return _shell("ai — generated manifest", body, byline="fanout ai")


def render_claude(task_outputs: List[dict]) -> str:
    parts = [
        '<div class="summary">'
        f'<div class="card"><div class="label">tasks</div><div class="value">{len(task_outputs)}</div></div>'
        '</div>'
    ]
    for r in task_outputs:
        title = f"W{r.get('id', '?')}"
        out = html.escape(r.get("output", ""))
        parts.append(
            f'<section class="bucket"><h3>{html.escape(title)}</h3>'
            f'<div class="log">{out}</div></section>'
        )
    return _shell("claude — task outputs", "".join(parts), byline="fanout claude")
