"""v5: agent registry, Jev client, router, planner, boosts, dispatch, MCP, install."""
from __future__ import annotations

import io
import json
import pathlib
import urllib.request

import pytest

import agents
import boost
import dispatch
import fanout
import install
import jev
import mcp_server
import planner
import router


FAKE = """#!/usr/bin/env bash
echo "ran $(basename "$0") base=${ANTHROPIC_BASE_URL:-${OPENAI_BASE_URL:-none}}"
printf '%s\\n' "${@: -1}" | head -c 300
"""


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("fakeclaude", "fakecodex"):
        p = bin_dir / name
        p.write_text(FAKE)
        p.chmod(0o755)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "agents.json").write_text(json.dumps({
        "claude": {"bins": ["fakeclaude"]},
        "codex": {"bins": ["fakecodex"]},
    }))
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setattr(agents, "CONFIG_DIR", cfg)
    monkeypatch.setattr(jev, "CONFIG_DIR", cfg)
    monkeypatch.setattr(dispatch, "RUNS_DIR", tmp_path / "runs")
    for var in ("OPENROUTER_API_KEY", "FANOUT_OPENROUTER_API_KEY", "FANOUT_HOST", "CLAUDECODE",
                "CLAUDE_CODE_ENTRYPOINT", "CODEX_HOME", "CURSOR_TRACE_ID", "TERM_PROGRAM"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


# ---------- agents ----------


def test_detect_uses_overrides_and_path(fake_env):
    found = agents.detect()
    assert set(found) == {"claude", "codex"}
    assert found["claude"].binary.endswith("fakeclaude")


def test_detect_only(fake_env):
    assert set(agents.detect(["codex"])) == {"codex"}


def test_override_none_removes_agent(fake_env):
    (agents.CONFIG_DIR / "agents.json").write_text(json.dumps({"claude": None, "codex": {"bins": ["fakecodex"]}}))
    assert set(agents.detect()) == {"codex"}


def test_custom_agent(fake_env):
    (agents.CONFIG_DIR / "agents.json").write_text(json.dumps(
        {"mine": {"bins": ["fakeclaude"], "cmd": ["{bin}", "--go", "{prompt}"], "strengths": "x"}}))
    a = agents.detect(["mine"])["mine"]
    assert a.argv("hi") == [a.binary, "--go", "hi"]


def test_argv_safe_vs_yolo():
    a = agents.registry()["claude"]
    assert a.argv("p")[-2:] == ["--permission-mode", "acceptEdits"]
    assert a.argv("p", yolo=True)[-1] == "--dangerously-skip-permissions"
    assert "--permission-mode" not in a.argv("p", yolo=True)


def test_ambiguous_bin_rejected(tmp_path, monkeypatch):
    p = tmp_path / "agent"
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(agents, "CONFIG_DIR", tmp_path / "nocfg")
    assert "cursor" not in agents.detect()


@pytest.mark.parametrize("env,host", [
    ({"CLAUDECODE": "1"}, "claude"),
    ({"CODEX_HOME": "/x"}, "codex"),
    ({"CURSOR_TRACE_ID": "abc"}, "cursor"),
    ({"TERM_PROGRAM": "vscode"}, "vscode"),
    ({"FANOUT_HOST": "zed"}, "zed"),
    ({}, None),
])
def test_detect_host(env, host):
    assert agents.detect_host(env) == host


def test_detect_host_prefers_mcp_client():
    assert agents.detect_host({"CLAUDECODE": "1"}, mcp_client="cursor-vscode") == "cursor"
    assert agents.normalize_client("claude-code") == "claude"


# ---------- jev ----------


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_jev_payload_and_parse(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["body"] = json.loads(req.data)
        return _Resp(json.dumps({"answers": {"q": {"type": "noul", "noul": 0.9}}}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = jev.decide({"a": 1}, {"q": jev.noul("?", "y", "n")}, key="k")
    assert out["q"]["noul"] == 0.9
    assert seen["url"] == jev.JEV_URL
    assert seen["auth"] == "Bearer k"
    assert seen["body"]["model"] == jev.JEV_MODEL
    assert seen["body"]["questions"]["q"] == {"type": "noul", "instructions": "?",
                                              "criteria": {"true": "y", "false": "n"}}


def test_jev_missing_key_raises(fake_env):
    with pytest.raises(jev.JevError):
        jev.decide({}, {"q": jev.noul("?", "y", "n")})


def test_jev_missing_answers_raises(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: _Resp(b'{"oops": 1}'))
    with pytest.raises(jev.JevError):
        jev.decide({}, {"q": jev.noul("?", "y", "n")}, key="k")


def test_jev_key_from_config_file(fake_env):
    (jev.CONFIG_DIR / "openrouter_key").write_text("sk-file\n")
    assert jev.api_key() == "sk-file"


def test_question_builders():
    assert jev.choice("pick", {"a": "A"})["type"] == "choice"
    assert jev.score("how big", ["s", "l"])["criteria"] == ["s", "l"]


# ---------- router ----------


def test_width_hint_and_clamp():
    assert router.route_width("x", hint=10, max_n=4) == (4, "hint")


def test_width_heuristic_counts_bullets(fake_env):
    n, src = router.route_width("- a thing\n- b thing\n- c thing", use_jev=True)
    assert (n, src) == (3, "heuristic")


def test_width_single(fake_env):
    assert router.route_width("fix typo in README")[0] == 1


def test_width_uses_jev_when_key(fake_env, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setattr(jev, "decide", lambda *a, **k: {"width": {"score": 2}})
    assert router.route_width("anything") == (3, "jev")


def test_width_jev_failure_falls_back(fake_env, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")

    def boom(*a, **k):
        raise jev.JevError("401")
    monkeypatch.setattr(jev, "decide", boom)
    assert router.route_width("- a b\n- c d")[1] == "heuristic"


def _subs(*prompts):
    return [{"title": p, "prompt": p, "depends_on": []} for p in prompts]


def test_assign_heuristic_routes_by_strength(fake_env):
    ag = agents.detect()
    out = router.assign("t", _subs("refactor the architecture across modules",
                                   "fix the failing shell script"), ag, use_jev=False)
    assert [a.agent for a in out] == ["claude", "codex"]
    assert all(a.source == "heuristic" for a in out)


def test_assign_uses_jev_answers(fake_env, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    calls = []

    def fake_decide(state, questions, **kw):
        calls.append((state, questions))
        assert set(questions["agent"]["criteria"]) == {"claude", "codex"}
        return {
            "agent": {"choice": "codex", "probabilities": {"codex": 0.8, "claude": 0.2}, "confidence": 0.8},
            "independent": {"noul": 0.9}, "size": {"score": 3}, "caveman": {"score": 2},
            "shell_heavy": {"noul": 0.9}, "context_heavy": {"noul": 0.1},
        }
    monkeypatch.setattr(jev, "decide", fake_decide)
    [a] = router.assign("t", _subs("do x"), agents.detect())
    assert (a.agent, a.source, a.size) == ("codex", "jev", "large")
    assert a.boost == {"caveman": "ultra", "rtk": True, "headroom": False}
    assert calls


def test_assign_jev_invalid_choice_falls_back_to_available(fake_env, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setattr(jev, "decide", lambda *a, **k: {"agent": {"choice": "gemini", "probabilities": {}}})
    [a] = router.assign("t", _subs("do x"), agents.detect())
    assert a.agent in ("claude", "codex")


def test_assign_pin(fake_env):
    out = router.assign("t", _subs("fix shell", "fix cli"), agents.detect(), pin="claude")
    assert {a.agent for a in out} == {"claude"}


def test_assign_boost_toggles(fake_env):
    [a] = router.assign("t", _subs("run the tests and read the logs"), agents.detect(),
                        boosts={"caveman": False, "rtk": False, "headroom": True}, use_jev=False)
    assert a.boost == {"caveman": None, "rtk": False, "headroom": True}


def test_assign_heuristic_boosts(fake_env):
    doc, sh = router.assign("t", _subs("write the README docs", "run the build and lint in ci"),
                            agents.detect(), use_jev=False)
    assert doc.boost["caveman"] == "lite" and not doc.boost["rtk"]
    assert sh.boost["rtk"]


def test_waves_explicit_and_implicit_deps(fake_env):
    subs = _subs("build api", "build ui", "integrate api and ui end-to-end")
    subs.append({"title": "d", "prompt": "docs", "depends_on": [3]})
    out = router.assign("t", subs, agents.detect(), use_jev=False)
    waves = {a.id: a.wave for a in out}
    assert waves == {1: 1, 2: 1, 3: 2, 4: 3}
    assert out[2].depends_on == [1, 2]


def test_waves_ignore_cycles(fake_env):
    subs = [{"title": "a", "prompt": "a a", "depends_on": [2]}, {"title": "b", "prompt": "b b", "depends_on": [1]}]
    out = router.assign("t", subs, agents.detect(), use_jev=False)
    assert all(a.wave <= 2 for a in out)


def test_balance_spreads_close_calls(fake_env):
    ag = agents.detect()
    a = [router.Assignment(id=i, title="x", prompt="x", agent="claude", confidence=0.6,
                           probabilities={"claude": 0.6, "codex": 0.4}) for i in range(1, 5)]
    router._balance(a, ag)
    assert sorted(x.agent for x in a) == ["claude", "claude", "codex", "codex"]


def test_balance_keeps_clear_winner(fake_env):
    ag = agents.detect()
    a = [router.Assignment(id=i, title="x", prompt="x", agent="claude", confidence=0.95,
                           probabilities={"claude": 0.95, "codex": 0.05}) for i in range(1, 4)]
    router._balance(a, ag)
    assert {x.agent for x in a} == {"claude"}


def test_split_candidates():
    assert router._split_candidates("1. do a thing\n2) do b thing") == ["do a thing", "do b thing"]
    assert router._split_candidates("add login; add logout") == ["add login", "add logout"]
    assert router._split_candidates("one task") == ["one task"]


# ---------- planner ----------


def test_normalize_tasks_accepts_strings_and_dicts():
    tasks, why = planner.normalize_tasks({"rationale": "r", "tasks": ["a", {"prompt": "b", "depends_on": [1]}]})
    assert [t["prompt"] for t in tasks] == ["a", "b"]
    assert tasks[1]["depends_on"] == [1] and why == "r"


def test_normalize_tasks_rejects_empty():
    with pytest.raises(ValueError):
        planner.normalize_tasks({"tasks": [{"prompt": ""}]})


def test_plan_single_is_direct(fake_env):
    tasks, _, src = planner.plan("x", 1, agents=agents.detect())
    assert src == "direct" and tasks[0]["prompt"] == "x"


def test_plan_naive(fake_env):
    tasks, _, src = planner.plan("- a b\n- c d", 2, agents=agents.detect(), planner="naive")
    assert src == "naive" and [t["prompt"] for t in tasks] == ["a b", "c d"]


def test_plan_auto_uses_local_agent_then_naive(fake_env, monkeypatch):
    seen = []

    def fake_local(agent, *a, **k):
        seen.append(agent.name)
        if agent.name == "claude":
            raise planner.PlannerError("nope")
        return '```json\n{"rationale": "ok", "tasks": [{"title": "A", "prompt": "do A"}, "do B"]}\n```'
    monkeypatch.setattr(planner, "_plan_local", fake_local)
    tasks, why, src = planner.plan("big task", 2, agents=agents.detect(), host="codex")
    assert seen == ["codex"] and src == "codex" and len(tasks) == 2


def test_plan_auto_all_fail_goes_naive(fake_env, monkeypatch):
    def fail(*a, **k):
        raise planner.PlannerError("x")
    monkeypatch.setattr(planner, "_plan_local", fail)
    _, _, src = planner.plan("- a b\n- c d", 2, agents=agents.detect())
    assert src == "naive"


def test_local_planner_argv_is_read_only():
    reg = agents.registry()
    assert reg["claude"].name == "claude"
    assert planner.local_planner_argv(reg["claude"], "p")[-2:] == ["--tools", ""]
    assert "read-only" in planner.local_planner_argv(reg["codex"], "p")


# ---------- boost ----------


def test_decorate_adds_directives():
    out = boost.decorate("do it", {"caveman": "ultra", "rtk": True}, rtk_available=True)
    assert "ultra caveman" in out and "rtk" in out and out.startswith("do it")


def test_decorate_skips_rtk_when_missing():
    out = boost.decorate("do it", {"caveman": None, "rtk": True}, rtk_available=False)
    assert out == "do it"


def test_headroom_env():
    assert boost.headroom_env("anthropic", 9)["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9"
    assert boost.headroom_env("openai", 9)["OPENAI_BASE_URL"] == "http://127.0.0.1:9/v1"
    assert boost.headroom_env("google", 9) == {}


def test_plan_enable_steps(fake_env, monkeypatch):
    monkeypatch.setattr(boost.shutil, "which", lambda b: "/x/" + b if b in ("brew", "npx", "uv") else None)
    monkeypatch.setattr(boost, "_caveman_installed", lambda: None)
    steps = boost.plan_enable(agents.registry())
    labels = [s[0] for s in steps]
    assert "rtk: install" in labels and "headroom: install" in labels
    assert ["rtk", "init", "-g", "--codex"] in [s[1] for s in steps]
    assert any(s[1][0] == "npx" for s in steps)


def test_enable_dry_run_runs_nothing(fake_env, monkeypatch):
    monkeypatch.setattr(boost.subprocess, "run", lambda *a, **k: pytest.fail("ran"))
    res = boost.enable(agents.detect(), dry_run=True, log=lambda m: None)
    assert all(not r["ran"] for r in res)


# ---------- dispatch (real subprocesses, fake agents) ----------


def _plan(fake_env, *prompts, deps=None):
    subs = _subs(*prompts)
    for i, d in (deps or {}).items():
        subs[i]["depends_on"] = d
    return fanout.make_plan("t", subtasks=subs, use_jev=False, cwd=str(fake_env))


def test_make_plan_shape(fake_env):
    p = _plan(fake_env, "fix the shell script", "refactor architecture")
    assert p["width"] == {"n": 2, "source": "caller"}
    assert set(p["agents"]) == {"claude", "codex"}
    assert [a["agent"] for a in p["assignments"]] == ["codex", "claude"]


def test_make_plan_no_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(agents, "CONFIG_DIR", tmp_path)
    with pytest.raises(SystemExit):
        fanout.make_plan("t", subtasks=_subs("x"))


def test_make_plan_pin_missing(fake_env):
    with pytest.raises(SystemExit):
        fanout.make_plan("t", subtasks=_subs("x"), pin="gemini")


def test_execute_headless_waves_and_upstream(fake_env):
    p = _plan(fake_env, "fix the shell script", "refactor architecture", "combine both", deps={2: [1, 2]})
    st = fanout.launch(p, use_tmux=False, log=lambda *a: None)
    assert st["state"] == "done"
    assert {k: v["state"] for k, v in st["workers"].items()} == {"1": "ok", "2": "ok", "3": "ok"}
    run = pathlib.Path(st["run_dir"])
    assert "ran fakecodex" in (run / "W1.out").read_text()
    w3 = (run / "W3.prompt").read_text()
    assert '<upstream id="1"' in w3 and '<upstream id="2"' in w3
    assert "<fanout-boost>" in w3
    r = fanout.results(run.name)
    assert set(r["outputs"]) == {"W1", "W2", "W3"}


def test_execute_missing_agent_and_failure(fake_env):
    p = _plan(fake_env, "a b", "c d")
    p["assignments"][0]["agent"] = "gemini"
    (fake_env / "bin" / "fakecodex").write_text("#!/usr/bin/env bash\nexit 3\n")
    p["assignments"][1]["agent"] = "codex"
    st = fanout.launch(p, use_tmux=False, log=lambda *a: None)
    assert st["state"] == "failed"
    assert st["workers"]["1"]["state"] == "error"
    assert st["workers"]["2"] == {**st["workers"]["2"], "state": "failed", "exit": 3}


def test_execute_timeout(fake_env):
    (fake_env / "bin" / "fakeclaude").write_text("#!/usr/bin/env bash\nsleep 30\n")
    p = _plan(fake_env, "refactor architecture")
    p["timeout"] = 1
    st = fanout.launch(p, use_tmux=False, log=lambda *a: None)
    assert st["workers"]["1"]["state"] == "timeout"


def test_headroom_env_injected_when_proxy_up(fake_env, monkeypatch):
    monkeypatch.setattr(boost, "ensure_headroom_proxy", lambda run_dir: True)
    p = _plan(fake_env, "refactor architecture, read the logs")
    p["assignments"][0]["boost"]["headroom"] = True
    st = fanout.launch(p, use_tmux=False, log=lambda *a: None)
    out = (pathlib.Path(st["run_dir"]) / "W1.out").read_text()
    assert f"base=http://127.0.0.1:{boost.HEADROOM_PORT}" in out


def test_worker_script_quotes_prompt(tmp_path):
    s = dispatch.worker_script(["/bin/x", dispatch._PROMPT, "--f"], prompt_file=tmp_path / "p",
                               out_file=tmp_path / "o", done_file=tmp_path / "d", cwd="/tmp",
                               env={"A": "b c"}, label="W1", hold=False)
    assert '/bin/x "$PROMPT" --f' in s and "export A='b c'" in s and "sleep" not in s


def test_resolve_run_latest(fake_env):
    a = dispatch.new_run_dir()
    dispatch.save_plan(a, {"assignments": []})
    assert dispatch.resolve_run(None) == a
    with pytest.raises(FileNotFoundError):
        dispatch.resolve_run("nope")


# ---------- CLI ----------


def test_parse_any_agent_subcommand():
    a = fanout.parse_args(["codex", "3", "fix it", "--yolo"])
    assert (a.cmd, a.n, a.prompts, a.yolo) == ("codex", 3, ["fix it"], True)


def test_parse_plan_routing_flags():
    a = fanout.parse_args(["plan", "t", "--agents", "claude,codex", "--pin", "codex", "--planner", "naive",
                           "--no-jev", "--no-headroom"])
    assert fanout._only(a) == ["claude", "codex"]
    assert a.pin == "codex" and a.planner == "naive" and a.no_jev
    assert fanout._boost_flags(a) == {"caveman": True, "rtk": True, "headroom": False}


def test_prompts_to_subtasks_count():
    assert len(fanout._prompts_to_subtasks(["x"], 3)) == 3
    with pytest.raises(SystemExit):
        fanout._prompts_to_subtasks(["x", "y"], 3)


def test_cli_dry_run_json(fake_env, capsys):
    assert fanout.main(["run", "fix the shell script", "refactor architecture", "--dry-run", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert [a["agent"] for a in plan["assignments"]] == ["codex", "claude"]


def test_cli_agent_command_runs(fake_env, capsys):
    assert fanout.main(["claude", "2", "hello", "--no-tmux", "--cwd", str(fake_env)]) == 0
    out = capsys.readouterr().out
    assert out.count("ran fakeclaude") == 2


def test_exec_request_dry_run(fake_env):
    r = fanout.handle_request({"prompts": ["fix the shell script"], "dry_run": True})
    assert r["plan"]["assignments"][0]["agent"] == "codex"


def test_exec_request_wait(fake_env):
    r = fanout.handle_request({"subtasks": [{"prompt": "hello there"}], "wait": True, "cwd": str(fake_env)})
    assert r["status"]["state"] == "done" and "ran" in r["outputs"]["W1"]


# ---------- MCP ----------


def _rpc(lines):
    out = io.StringIO()
    mcp_server.serve(io.StringIO("\n".join(json.dumps(m) for m in lines) + "\n"), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_mcp_handshake_and_tools(fake_env):
    res = _rpc([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-03-26", "clientInfo": {"name": "claude-code"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "fanout_agents", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "nope"},
    ])
    assert [r["id"] for r in res] == [1, 2, 3, 4]
    assert res[0]["result"]["protocolVersion"] == "2025-03-26"
    names = {t["name"] for t in res[1]["result"]["tools"]}
    assert {"fanout_plan", "fanout_run", "fanout_status", "fanout_results"} <= names
    body = json.loads(res[2]["result"]["content"][0]["text"])
    assert body["host"] == "claude" and set(body["installed"]) == {"claude", "codex"}
    assert res[3]["error"]["code"] == -32601


def test_mcp_plan_and_errors(fake_env):
    res = _rpc([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "fanout_plan", "arguments": {"subtasks": [{"prompt": "fix the shell script"}]}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "fanout_plan", "arguments": {"subtasks": [{"prompt": "x"}], "pin": "gemini"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "missing", "arguments": {}}},
    ])
    plan = json.loads(res[0]["result"]["content"][0]["text"])["plan"]
    assert plan["assignments"][0]["agent"] == "codex"
    assert res[1]["result"]["isError"] and "not installed" in res[1]["result"]["content"][0]["text"]
    assert res[2]["result"]["isError"]


def test_mcp_tool_schemas_are_objects():
    for t in mcp_server.TOOLS:
        assert t["inputSchema"]["type"] == "object"


# ---------- install ----------


def test_merge_mcp_json_preserves_others(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    data = install.merge_mcp_json(p)
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["fanout"]["args"] == ["mcp"]


def test_plan_install_only_detected(fake_env, monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda b: "/x/codex" if b == "codex" else None)
    monkeypatch.setattr(install, "JSON_CLIENTS", {"cursor": fake_env / "nope" / "mcp.json"})
    monkeypatch.setattr(install, "SKILL_DIRS", [])
    labels = [label for label, _ in install.plan_install()]
    assert any(x.startswith("mcp codex") for x in labels)
    assert not any("claude" in x or "cursor" in x for x in labels)


def test_install_writes_cursor_json(fake_env, monkeypatch):
    target = fake_env / "cursor" / "mcp.json"
    target.parent.mkdir()
    monkeypatch.setattr(install.shutil, "which", lambda b: None)
    monkeypatch.setattr(install, "JSON_CLIENTS", {"cursor": target})
    monkeypatch.setattr(install, "SKILL_DIRS", [fake_env / "h" / ".claude" / "skills" / "fanout"])
    monkeypatch.setattr(install, "BIN_DIR", fake_env / "localbin")
    (fake_env / "h" / ".claude").mkdir(parents=True)
    assert install.run(boost=False) == 0
    assert json.loads(target.read_text())["mcpServers"]["fanout"]["command"] == install.ENTRY
    assert (fake_env / "localbin" / "fanout").is_symlink()
    assert (fake_env / "h" / ".claude" / "skills" / "fanout" / "SKILL.md").exists()
