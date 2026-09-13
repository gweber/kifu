"""Adapters for other coding agents, on synthetic histories shaped like the real ones."""
import hashlib
import json
import sqlite3

from kifu import agents, config, extract, sources


def _codex(tmp_path):
    root = tmp_path / "codex" / "2026" / "03" / "01"
    root.mkdir(parents=True)
    sid = "019c9286-0769-7cd1-966f-5c0f399aab45"
    lines = [
        {"timestamp": "2026-03-01T10:00:00.000Z", "type": "session_meta",
         "payload": {"id": sid, "cwd": "/home/ada/code/tidepool", "originator": "codex-tui", "git": {"branch": "main"}}},
        {"timestamp": "2026-03-01T10:00:01.000Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>…"}]}},
        {"timestamp": "2026-03-01T10:00:02.000Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "# Context from my IDE setup:\nopen files…\n## My request for Codex:\nadd a tide cache"}},
        {"timestamp": "2026-03-01T10:01:00.000Z", "type": "event_msg", "payload": {"type": "agent_message", "message": "Looking at the API first."}},
        {"timestamp": "2026-03-01T10:02:00.000Z", "type": "response_item",
         "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": "*** Begin Patch\n*** Update File: api/cache.py\n@@\n"}},
        {"timestamp": "2026-03-01T10:03:00.000Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": "git commit -m 'tide cache'"})}},
        {"timestamp": "2026-03-01T10:04:00.000Z", "type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "The cache is in."}},
        {"timestamp": "2026-03-01T10:10:00.000Z", "type": "event_msg", "payload": {"type": "user_message", "message": "and expire it after a day"}},
    ]
    path = root / f"rollout-2026-03-01T10-00-00-{sid}.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return tmp_path / "codex", sid


def test_codex(tmp_path):
    root, sid = _codex(tmp_path)
    items, parse, resume = agents.ADAPTERS["codex"]
    (key, where), = items(str(root)).values()
    sp = parse(sid, where)
    assert [t["prompt"] for t in sp.turns] == ["add a tide cache", "and expire it after a day"]
    assert sp.turns[0]["texts"] == ["Looking at the API first.", "The cache is in."]
    kinds = {(e[2], e[3]) for e in sp.evidence}
    assert ("write", "/home/ada/code/tidepool/api/cache.py") in kinds and ("commit", "tide cache") in kinds
    assert sp.id == f"codex-{sid}" and sp.tool == "codex"
    assert resume({"id": sp.id, "cwd": "/home/ada/code/tidepool"}) == f"cd /home/ada/code/tidepool && codex resume {sid}"


def test_gemini(tmp_path):
    chats = tmp_path / "gemini" / hashlib.sha256(b"/home/ada/code/inkwell").hexdigest() / "chats"
    chats.mkdir(parents=True)
    data = {"sessionId": "s-1", "projectHash": hashlib.sha256(b"/home/ada/code/inkwell").hexdigest(), "messages": [
        {"id": "m1", "timestamp": "2026-03-01T09:00:00Z", "type": "user",
         "content": [{"text": "import the vault\nCurrent File Path:\n```python\n/home/ada/code/inkwell/importer.py\n```"}]},
        {"id": "m2", "timestamp": "2026-03-01T09:00:05Z", "type": "user", "content": [{"text": "import the vault"}]},
        {"id": "m3", "timestamp": "2026-03-01T09:01:00Z", "type": "gemini", "content": "Done, see importer.py.",
         "toolCalls": [{"name": "write_file", "args": {"file_path": "importer.py"}},
                       {"name": "run_shell_command", "args": {"command": "git push"}}]},
        {"id": "m4", "timestamp": "2026-03-01T09:02:00Z", "type": "info", "content": "Request cancelled."},
    ]}
    (chats / "session-2026-03-01T09-00-s1.json").write_text(json.dumps(data))
    items, parse, _ = agents.ADAPTERS["gemini"]
    (key, where), = items(str(tmp_path / "gemini")).values()
    sp = parse("s-1", where, known_cwds=["/home/ada/code/inkwell"])
    assert [t["prompt"] for t in sp.turns] == ["import the vault"], "the IDE block is cut and the retry dropped"
    assert sp.cwds.most_common(1)[0][0] == "/home/ada/code/inkwell"
    assert {(e[2], e[3]) for e in sp.evidence} >= {("write", "/home/ada/code/inkwell/importer.py"), ("push", "")}


def test_gemini_finds_its_project_from_the_ide_block(tmp_path):
    chats = tmp_path / "gemini" / "unknownhash" / "chats"
    chats.mkdir(parents=True)
    data = {"sessionId": "s-2", "projectHash": "unknownhash", "messages": [
        {"id": "m1", "timestamp": "2026-03-01T09:00:00Z", "type": "user",
         "content": "fix it\nCurrent File Path:\n```python\n/Users/ada/code/foundry/backend/core.py\n```"}]}
    (chats / "session-x.json").write_text(json.dumps(data))
    sp = agents.gemini_parse("s-2", str(chats / "session-x.json"))
    assert sp.cwds.most_common(1)[0][0] == "/Users/ada/code/foundry"


def test_cline(tmp_path):
    root = tmp_path / "cline"
    (root / "tasks" / "1775" ).mkdir(parents=True)
    (root / "state").mkdir()
    (root / "state" / "taskHistory.json").write_text(json.dumps([{"id": "1775", "cwdOnTaskInitialization": "/home/ada/code/urls"}]))
    (root / "tasks" / "1775" / "ui_messages.json").write_text(json.dumps([
        {"ts": 1775248402570, "type": "say", "say": "task", "text": "collect the top urls per country"},
        {"ts": 1775248403410, "type": "say", "say": "api_req_started", "text": "{\"request\":\"<task>…\"}"},
        {"ts": 1775248404000, "type": "say", "say": "text", "text": "Starting with Germany."},
        {"ts": 1775248405000, "type": "say", "say": "tool", "text": json.dumps({"tool": "newFileCreated", "path": "de.csv"})},
        {"ts": 1775248406000, "type": "say", "say": "user_feedback", "text": "also add austria"},
        {"ts": 1775248407000, "type": "say", "say": "command", "text": "git commit -am 'urls'"},
    ]))
    items, parse, resume = agents.ADAPTERS["cline"]
    (key, where), = items(str(root)).values()
    sp = parse("1775", where)
    assert [t["prompt"] for t in sp.turns] == ["collect the top urls per country", "also add austria"]
    assert ("write", "/home/ada/code/urls/de.csv") in {(e[2], e[3]) for e in sp.evidence}
    assert sp.started.startswith("2026-04-03T") and resume({"id": sp.id}) is None


def test_hermes(tmp_path):
    path = tmp_path / "state.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT, title TEXT, cwd TEXT, started_at REAL);
        CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, tool_calls TEXT,
                              timestamp REAL, display_kind TEXT);
        INSERT INTO sessions VALUES ('20260301_100000_ab', 'telegram', 'Garden plan', '/home/ada', 1772359200);
        INSERT INTO sessions VALUES ('20260301_110000_cd', 'cron', NULL, NULL, 1772362800);
        INSERT INTO messages VALUES (1, '20260301_100000_ab', 'user', 'plan the raised beds for spring', NULL, 1772359200.5, NULL);
        INSERT INTO messages VALUES (2, '20260301_100000_ab', 'assistant', 'Three beds, rotating crops.',
            '[{"function": {"name": "write_file", "arguments": "{\\"path\\": \\"/home/ada/garden.md\\"}"}}]', 1772359260, NULL);
        INSERT INTO messages VALUES (3, '20260301_100000_ab', 'user', '[System note: memory updated]', NULL, 1772359270, NULL);
        INSERT INTO messages VALUES (4, '20260301_100000_ab', 'user', 'hidden', NULL, 1772359280, 'internal_notification');
        INSERT INTO messages VALUES (5, '20260301_110000_cd', 'user', '[IMPORTANT: You are running as a scheduled job', NULL, 1772362800, NULL);
    """)
    con.commit()
    items, parse, resume = agents.ADAPTERS["hermes"]
    found = items(str(path))
    human = parse("20260301_100000_ab", str(path))
    assert [t["prompt"] for t in human.turns] == ["plan the raised beds for spring"]
    assert human.turns[0]["texts"] == ["Three beds, rotating crops."] and human.title == "Garden plan"
    assert ("write", "/home/ada/garden.md") in {(e[2], e[3]) for e in human.evidence} and not human.automated
    assert parse("20260301_110000_cd", str(path)).automated is True
    assert set(found) == {"20260301_100000_ab", "20260301_110000_cd"}
    assert resume({"id": human.id}) == "hermes --resume 20260301_100000_ab"


def test_opencode(tmp_path):
    path = tmp_path / "opencode.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE session(id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT, title TEXT, time_created INTEGER);
        CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT);
        CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, data TEXT);
    """)
    rows = [
        ("session", ("ses_1", None, "/home/ada/code/blog", "Blog RSS", 1772359200000)),
        ("session", ("ses_2", "ses_1", "/home/ada/code/blog", "subtask", 1772359300000)),
        ("message", ("msg_1", "ses_1", 1772359200000, json.dumps({"role": "user"}))),
        ("part", ("p1", "msg_1", "ses_1", json.dumps({"type": "text", "text": "add an rss feed"}))),
        ("part", ("p2", "msg_1", "ses_1", json.dumps({"type": "text", "text": "The previous request exceeded…", "synthetic": True}))),
        ("message", ("msg_2", "ses_1", 1772359260000, json.dumps({"role": "assistant"}))),
        ("part", ("p3", "msg_2", "ses_1", json.dumps({"type": "tool", "tool": "write", "state": {"input": {"filePath": "feed.xml"}}}))),
        ("part", ("p4", "msg_2", "ses_1", json.dumps({"type": "text", "text": "Feed added."}))),
    ]
    for table, values in rows:
        con.execute(f"INSERT INTO {table} VALUES ({','.join('?' * len(values))})", values)
    con.commit()
    items, parse, resume = agents.ADAPTERS["opencode"]
    assert set(items(str(path))) == {"ses_1", "ses_2"}
    sp = parse("ses_1", str(path))
    assert [t["prompt"] for t in sp.turns] == ["add an rss feed"] and sp.turns[0]["texts"] == ["Feed added."]
    assert ("write", "/home/ada/code/blog/feed.xml") in {(e[2], e[3]) for e in sp.evidence}
    assert parse("ses_2", str(path)).automated is True
    assert resume({"id": sp.id, "cwd": "/home/ada/code/blog"}) == "cd /home/ada/code/blog && opencode --session ses_1"


def test_a_codex_source_goes_through_pull_scan_and_resume(store, tmp_path):
    cfg, con = store
    root, sid = _codex(tmp_path)
    cfg.sources.append(config.Source(host="laptop", kind="codex", path=str(root)))
    assert sources.pull(log=lambda m: None) == []
    extract.scan_archive(con, log=lambda m: None)
    s = con.execute("SELECT * FROM sessions WHERE id=?", (f"codex-{sid}",)).fetchone()
    assert s["tool"] == "codex" and s["project"] == "tidepool" and s["n_turns"] == 2
    assert extract.scan_archive(con, log=lambda m: None) == 0, "unchanged sessions are not read again"
    from kifu import report
    data = report.collect(con, habits_data={})
    session = next(x for x in data["sessions"] if x["id"] == f"codex-{sid}")
    assert report.resume_command(session) == f"cd /home/ada/code/tidepool && codex resume {sid}"
