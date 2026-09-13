"""Claude Code integration: hooks, MCP server, briefs, installer, drain — against the demo store."""
import io
import json
import os
import subprocess
import sys

from kifu import brief, claude_code, db, drain, hooks, mcp


def test_session_start_shows_the_projects_open_ideas(store):
    out = hooks.session_start({"source": "startup", "cwd": "/home/ada/code/tidepool"})
    assert out["systemMessage"].startswith("kifu: 2 open ideas in tidepool")
    assert "Weekend low-tide push alerts" in out["systemMessage"]
    ctx = out["hookSpecificOutput"]
    assert ctx["hookEventName"] == "SessionStart" and "anchor " in ctx["additionalContext"]


def test_session_start_stays_quiet_where_it_has_nothing_to_say(store):
    assert hooks.session_start({"source": "resume", "cwd": "/home/ada/code/tidepool"}) is None
    assert hooks.session_start({"source": "startup", "cwd": "/home/ada/code/dotfiles"}) is None
    assert hooks.session_start({"source": "startup", "cwd": "/home/ada"}) is None


def test_the_hook_starts_fast_without_heavy_imports(store):
    cfg, _ = store
    code = ("import sys, io, json; sys.stdin = io.StringIO(json.dumps({'source': 'startup', 'cwd': '/home/ada/code/tidepool'}));"
            "from kifu.__main__ import main\ntry:\n    main(['hook', 'session-start'])\nexcept SystemExit:\n    pass\n"
            "print('HEAVY', [m for m in ('numpy', 'sklearn', 'httpx', 'fastapi') if m in sys.modules])")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                       env={**os.environ, "KIFU_CONFIG": cfg.path})
    assert "kifu: 2 open ideas in tidepool" in r.stdout
    assert "HEAVY []" in r.stdout, r.stdout


def test_a_broken_hook_never_breaks_the_session(store, monkeypatch, capsys):
    monkeypatch.setattr(hooks, "session_start", lambda payload: 1 / 0)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert hooks.main(["session-start"]) == 0
    assert capsys.readouterr().out == ""


def test_session_end_queues_and_starts_a_drain_without_a_service(store, monkeypatch):
    started = []
    monkeypatch.setattr(hooks.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda cmd, **kw: started.append(cmd))
    hooks.session_end({"session_id": "s1", "transcript_path": "/x.jsonl", "reason": "prompt_input_exit"})
    assert drain.pending() == 1 and started and started[0][-1] == "drain"


def test_mcp_handshake_and_tools(store, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/home/ada/code/inkwell")
    init = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["protocolVersion"] == "2025-06-18" and "tools" in init["result"]["capabilities"]
    assert mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = [t["name"] for t in mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
    assert names == ["kifu_ideas", "kifu_idea", "kifu_brief", "kifu_mark"]

    def tool(name, **args):
        r = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": name, "arguments": args}})
        return r["result"]

    here = json.loads(tool("kifu_ideas")["content"][0]["text"])
    assert here["project"] == "inkwell" and [i["title"] for i in here["ideas"]] == ["Import markdown vault into inkwell"]
    everywhere = json.loads(tool("kifu_ideas", scope="all")["content"][0]["text"])
    assert everywhere["total_open"] == 5
    b = json.loads(tool("kifu_brief", idea="vault importer")["content"][0]["text"])
    assert "Resolver for [[page]] wiki links" in b["brief"]
    missing = tool("kifu_idea", anchor="nope")
    assert missing["isError"] is True
    assert mcp.handle({"jsonrpc": "2.0", "id": 9, "method": "bogus"})["error"]["code"] == -32601


def test_mcp_over_stdio(store):
    cfg, _ = store
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kifu_ideas", "arguments": {"scope": "all"}}}]
    r = subprocess.run([sys.executable, "-m", "kifu", "mcp"], input="\n".join(json.dumps(x) for x in lines) + "\n",
                       capture_output=True, text=True, timeout=60, env={**os.environ, "KIFU_CONFIG": cfg.path})
    responses = [json.loads(x) for x in r.stdout.splitlines()]
    assert [x["id"] for x in responses] == [1, 2], r.stderr
    assert json.loads(responses[1]["result"]["content"][0]["text"])["total_open"] == 5


def test_a_brief_holds_what_a_new_session_needs(store):
    _, con = store
    data = brief.collect(con)
    line = brief.find_line(data, "offline mode")
    text = brief.build(con, line, data)
    assert text.startswith("We are picking up an idea")
    assert "Banner when cached predictions are older than a week" in text
    assert "claude --resume" in text and "tidepool/web/src/sw.ts" in text
    cwd, host, local = brief.workdir(line, data)
    assert cwd == "/home/ada/code/tidepool" and local


def test_installer_adds_hooks_once_and_removes_only_its_own(monkeypatch):
    monkeypatch.setattr(claude_code, "kifu_command", lambda: ["/opt/kifu/bin/kifu"])
    foreign = {"model": "opus", "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}
    installed, changes = claude_code.plan_settings(foreign)
    assert len(changes) == 2 and installed["model"] == "opus"
    start = installed["hooks"]["SessionStart"]
    assert start[0]["hooks"][0]["command"] == "echo hi"
    assert start[1] == {"matcher": "startup", "hooks": [{"type": "command", "timeout": 10,
                                                         "command": "/opt/kifu/bin/kifu hook session-start"}]}
    again, changes = claude_code.plan_settings(installed)
    assert changes == [] and again == installed
    removed, changes = claude_code.plan_settings(installed, uninstall=True)
    assert removed == foreign and len(changes) == 2


def test_drain_analyzes_queued_sessions_and_empties_the_queue(store, monkeypatch):
    cfg, con = store
    monkeypatch.setattr(drain.sources, "pull", lambda **kw: [])
    with open(hooks.queue_path(), "w") as fh:
        fh.write(json.dumps({"session_id": "x"}) + "\n")
    assert drain.drain(debounce=0, log=lambda m: None) == 1
    assert drain.pending() == 0
    assert db.connect(cfg.db_path).execute("SELECT COUNT(*) FROM lines").fetchone()[0] > 0
