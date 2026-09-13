"""Sessions of other coding agents: Codex CLI, Gemini CLI, Cline, Hermes Agent, OpenCode.

Each adapter turns one tool's on-disk history into the same SessionParse the Claude Code parser produces, so
everything after scanning — redaction, analysis, linking, git checks, the web app, the hooks — works unchanged.

    kind       where                                                  one session is
    codex      ~/.codex/sessions/**/rollout-*.jsonl                   a rollout file
    gemini     ~/.gemini/tmp/<sha256 of cwd>/chats/session-*.json     a chat file
    cline      <VS Code globalStorage>/saoudrizwan.claude-dev          tasks/<id>/ui_messages.json
    hermes     ~/.hermes/state.db (SQLite)                            a row in sessions
    opencode   ~/.local/share/opencode/opencode.db (SQLite)           a row in session

Only what the user typed becomes a prompt; injected context (IDE blocks, scheduler notes, compaction summaries)
is dropped. Session ids get the tool as prefix (codex-…) so they never collide with Claude Code's.
"""
import datetime as dt
import glob
import hashlib
import json
import os
import re
import sqlite3

from .extract import SessionParse, classify_bash, clean_prompt, new_turn, touch


def iso(value):
    """Timestamps from every tool as the ISO strings Claude Code writes: seconds, milliseconds or ISO in."""
    if isinstance(value, str):
        if value.isdigit():
            value = int(value)
        else:
            return value if value.endswith("Z") else value.replace("+00:00", "Z")
    if isinstance(value, int | float):
        seconds = value / 1000 if value > 1e11 else value
        return dt.datetime.fromtimestamp(seconds, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.") + \
            f"{int(seconds * 1000) % 1000:03d}Z"
    return None


def _parse(kind, sid, path):
    sp = SessionParse(f"{kind}-{sid}", path)
    sp.tool = kind
    return sp


def _reply(cur, sp, text, ts):
    if cur is not None and text and text.strip():
        if not cur["texts"] or cur["texts"][-1].strip() != text.strip():     # tools often log a reply twice
            cur["texts"].append(text)
        touch(cur, ts)


def _write(cur, sp, path, ts, cwd=None):
    if not path:
        return
    if cwd and not os.path.isabs(path):
        path = os.path.normpath(os.path.join(cwd, path))
    sp.add_evidence("write", path, ts)
    if cur is not None:
        cur["n_tools"] += 1
        touch(cur, ts)
        if path not in cur["files"]:
            cur["files"].append(path)


def _command(cur, sp, command, ts):
    if not command:
        return
    for kind, value in classify_bash(command):
        sp.add_evidence(kind, value, ts)
    if cur is not None:
        cur["n_tools"] += 1
        touch(cur, ts)


def _title(sp):
    if not sp.title and sp.turns:
        sp.ai_title = sp.turns[0]["prompt"].splitlines()[0][:80]


def _stat(path):
    st = os.stat(path)
    return st.st_size, st.st_mtime


# ---- Codex CLI ----------------------------------------------------------------------------------------------

_CODEX_REQUEST = "## My request for Codex:"


def codex_items(root):
    out = {}
    for path in glob.glob(os.path.join(root, "**", "rollout-*.jsonl"), recursive=True):
        m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", path)
        if m:
            out[m.group(1)] = (_stat(path), path)
    return out


def codex_parse(sid, path, **_):
    sp = _parse("codex", sid, path)
    cur = None
    for line in open(path, errors="replace"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        ts, p = r.get("timestamp"), r.get("payload") or {}
        if ts:
            sp.started, sp.ended = sp.started or ts, ts
        kind, ptype = r.get("type"), p.get("type")
        if kind == "session_meta":
            if p.get("cwd"):
                sp.cwds[p["cwd"]] += 1
            sp.entrypoint = sp.entrypoint or f"codex:{p.get('originator', '')}"
            sp.branch = (p.get("git") or {}).get("branch") or sp.branch
        elif kind == "turn_context" and p.get("cwd"):
            sp.cwds[p["cwd"]] += 1
        elif kind == "event_msg" and ptype == "user_message":
            text = p.get("message") or ""
            if _CODEX_REQUEST in text:              # the VS Code extension wraps the prompt in IDE context
                text = text.split(_CODEX_REQUEST, 1)[1]
            prompt = clean_prompt(text)
            if prompt:
                cur = new_turn(sp, ts, f"codex-{sid}-{len(sp.turns)}", prompt)
        elif kind == "event_msg" and ptype == "agent_message":
            _reply(cur, sp, p.get("message"), ts)
        elif kind == "event_msg" and ptype == "task_complete":
            _reply(cur, sp, p.get("last_agent_message"), ts)
        elif kind == "response_item" and ptype == "message" and p.get("role") == "assistant":
            _reply(cur, sp, "\n".join(c.get("text", "") for c in p.get("content") or []
                                      if isinstance(c, dict) and c.get("type") == "output_text"), ts)
        elif kind == "response_item" and ptype == "function_call" and p.get("name") == "exec_command":
            try:
                _command(cur, sp, json.loads(p.get("arguments") or "{}").get("cmd"), ts)
            except ValueError:
                pass
        elif kind == "response_item" and ptype == "custom_tool_call" and p.get("name") == "apply_patch":
            cwd = sp.cwds.most_common(1)[0][0] if sp.cwds else None
            for f in re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", p.get("input") or "", re.M):
                _write(cur, sp, f.strip(), ts, cwd)
    _title(sp)
    return sp


def codex_resume(session):
    return f"cd {session['cwd']} && codex resume {session['id'].removeprefix('codex-')}" if session.get("cwd") else None


# ---- Gemini CLI ---------------------------------------------------------------------------------------------

def gemini_items(root):
    out = {}
    for path in glob.glob(os.path.join(root, "*", "chats", "session-*.json")):
        try:
            sid = json.load(open(path)).get("sessionId") or os.path.basename(path)
        except (ValueError, OSError):
            continue
        out[sid] = (_stat(path), path)
    return out


def _gemini_text(content):
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(content or "")


def gemini_parse(sid, path, known_cwds=(), **_):
    data = json.load(open(path))
    sp = _parse("gemini", sid, path)
    sp.entrypoint = "gemini"
    # Gemini stores sha256(cwd) instead of the directory: recover it from directories kifu has seen on that host.
    by_hash = {hashlib.sha256(c.encode()).hexdigest(): c for c in known_cwds if c}
    if data.get("projectHash") in by_hash:
        sp.cwds[by_hash[data["projectHash"]]] += 1
    if not sp.cwds:
        # IDE mode names the open file: its project folder is the working directory (~/code/<project>/...).
        for m in data.get("messages", []):
            found = re.search(r"Current File Path:\s*(?:```\w*\s*)?(/(?:home|Users)/[^/\s]+/[^/\s]+/[^/\s]+)/",
                              _gemini_text(m.get("content")))
            if found:
                sp.cwds[found.group(1)] += 1
                break
    cwd = sp.cwds.most_common(1)[0][0] if sp.cwds else None
    cur = None
    for m in data.get("messages", []):
        ts = m.get("timestamp")
        if ts:
            sp.started, sp.ended = sp.started or ts, ts
        if m.get("type") == "user":
            text = _gemini_text(m.get("content")).split("\nCurrent File Path:", 1)[0]   # IDE mode appends the open file
            prompt = clean_prompt(text)
            if not prompt:
                continue
            if cur is not None and cur["prompt"] == prompt and not cur["texts"]:
                continue                                                         # a retried request
            cur = new_turn(sp, ts, m.get("id"), prompt)
        elif m.get("type") == "gemini":
            _reply(cur, sp, _gemini_text(m.get("content")), ts)
            for call in m.get("toolCalls") or []:
                args = call.get("args") or {}
                if call.get("name") in ("write_file", "replace", "edit"):
                    _write(cur, sp, args.get("file_path") or args.get("path"), call.get("timestamp") or ts, cwd)
                elif call.get("name") == "run_shell_command":
                    _command(cur, sp, args.get("command"), call.get("timestamp") or ts)
    _title(sp)
    return sp


def gemini_resume(session):
    return f"cd {session['cwd']} && gemini --list-sessions" if session.get("cwd") else None


# ---- Cline (VS Code extension) ------------------------------------------------------------------------------

def cline_items(root):
    out = {}
    for path in glob.glob(os.path.join(root, "tasks", "*", "ui_messages.json")):
        out[os.path.basename(os.path.dirname(path))] = (_stat(path), path)
    return out


def _cline_history(path):
    history = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(path))), "state", "taskHistory.json")
    try:
        return {str(t.get("id")): t for t in json.load(open(history))}
    except (OSError, ValueError):
        return {}


def cline_parse(sid, path, **_):
    sp = _parse("cline", sid, path)
    sp.entrypoint = "cline"
    meta = _cline_history(path).get(sid, {})
    if meta.get("cwdOnTaskInitialization"):
        sp.cwds[meta["cwdOnTaskInitialization"]] += 1
    cwd = meta.get("cwdOnTaskInitialization")
    cur = None
    for m in json.load(open(path)):
        ts = iso(m.get("ts"))
        if ts:
            sp.started, sp.ended = sp.started or ts, ts
        say = m.get("say")
        if m.get("type") == "say" and say in ("task", "user_feedback"):
            prompt = clean_prompt(m.get("text") or "")
            if prompt:
                cur = new_turn(sp, ts, f"cline-{sid}-{len(sp.turns)}", prompt)
        elif say == "text":
            _reply(cur, sp, m.get("text"), ts)
        elif say == "command":
            _command(cur, sp, m.get("text"), ts)
        elif say == "tool":
            try:
                tool = json.loads(m.get("text") or "{}")
            except ValueError:
                continue
            if tool.get("tool") in ("newFileCreated", "editedExistingFile"):
                _write(cur, sp, tool.get("path"), ts, cwd)
    _title(sp)
    return sp


def cline_resume(session):
    return None     # Cline reopens tasks from its history panel; there is no command line


# ---- SQLite-based tools -------------------------------------------------------------------------------------

def _ro(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _columns(con, table):
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


# ---- Hermes Agent -------------------------------------------------------------------------------------------

HERMES_AUTOMATED = {"cron", "kanban", "subagent"}
HERMES_INJECTED = ("[IMPORTANT: You are running as a schedul", "[CONTEXT COMPACTION", "[IMPORTANT: Background process",
                   "[System note:", "You've reached the maximum number of too", "[Your active task list was preserved",
                   "### Task:\nSuggest 3-5 relevant follow-up")


def hermes_items(path):
    con = _ro(path)
    rows = con.execute("""SELECT s.id, COUNT(m.id) n, MAX(m.timestamp) last FROM sessions s
                          LEFT JOIN messages m ON m.session_id=s.id GROUP BY s.id""").fetchall()
    return {r["id"]: ((r["n"], r["last"] or 0), path) for r in rows}


def hermes_parse(sid, path, **_):
    con = _ro(path)
    s = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    sp = _parse("hermes", sid, path)
    sp.entrypoint = f"hermes:{s['source']}"
    sp.automated = s["source"] in HERMES_AUTOMATED
    sp.title = s["title"] if "title" in s.keys() else None
    if "cwd" in s.keys() and s["cwd"]:
        sp.cwds[s["cwd"]] += 1
    cwd = s["cwd"] if "cwd" in s.keys() else None
    has_display = "display_kind" in _columns(con, "messages")
    cur = None
    for m in con.execute("SELECT * FROM messages WHERE session_id=? ORDER BY timestamp, id", (sid,)):
        ts = iso(m["timestamp"])
        if ts:
            sp.started, sp.ended = sp.started or ts, ts
        if m["role"] == "user":
            if has_display and m["display_kind"]:
                continue
            text = m["content"] or ""
            if text.lstrip().startswith(HERMES_INJECTED):
                continue
            prompt = clean_prompt(text)
            if prompt:
                cur = new_turn(sp, ts, f"hermes-{sid}-{m['id']}", prompt)
        elif m["role"] == "assistant":
            _reply(cur, sp, m["content"], ts)
            try:
                calls = json.loads(m["tool_calls"] or "[]")
            except ValueError:
                calls = []
            for call in calls:
                fn = call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                if fn.get("name") in ("write_file", "patch"):
                    _write(cur, sp, args.get("path") or args.get("file_path"), ts, cwd)
                elif fn.get("name") == "terminal":
                    _command(cur, sp, args.get("command"), ts)
    _title(sp)
    return sp


def hermes_resume(session):
    return f"hermes --resume {session['id'].removeprefix('hermes-')}"


# ---- OpenCode -----------------------------------------------------------------------------------------------

def opencode_items(path):
    con = _ro(path)
    rows = con.execute("""SELECT s.id, COUNT(m.id) n, MAX(m.time_created) last FROM session s
                          LEFT JOIN message m ON m.session_id=s.id GROUP BY s.id""").fetchall()
    return {r["id"]: ((r["n"], r["last"] or 0), path) for r in rows}


def opencode_parse(sid, path, **_):
    con = _ro(path)
    s = con.execute("SELECT * FROM session WHERE id=?", (sid,)).fetchone()
    sp = _parse("opencode", sid, path)
    sp.entrypoint = "opencode"
    sp.automated = bool(s["parent_id"])          # child sessions are the agent's own subtasks
    sp.title = s["title"]
    if s["directory"]:
        sp.cwds[s["directory"]] += 1
    cur = None
    for m in con.execute("SELECT * FROM message WHERE session_id=? ORDER BY time_created, id", (sid,)):
        data = json.loads(m["data"] or "{}")
        ts = iso(m["time_created"])
        if ts:
            sp.started, sp.ended = sp.started or ts, ts
        parts = [json.loads(p["data"] or "{}") for p in
                 con.execute("SELECT data FROM part WHERE message_id=? ORDER BY rowid", (m["id"],))]
        if data.get("role") == "user":
            text = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text" and not p.get("synthetic"))
            prompt = clean_prompt(text)
            if prompt:
                cur = new_turn(sp, ts, f"opencode-{m['id']}", prompt)
        elif data.get("role") == "assistant":
            for p in parts:
                if p.get("type") == "text":
                    _reply(cur, sp, p.get("text"), ts)
                elif p.get("type") == "tool":
                    args = (p.get("state") or {}).get("input") or {}
                    if p.get("tool") == "bash":
                        _command(cur, sp, args.get("command"), ts)
                    elif p.get("tool") in ("write", "edit"):
                        _write(cur, sp, args.get("filePath"), ts, s["directory"])
    _title(sp)
    return sp


def opencode_resume(session):
    return f"cd {session['cwd']} && opencode --session {session['id'].removeprefix('opencode-')}" if session.get("cwd") else None


ADAPTERS = {
    "codex": (codex_items, codex_parse, codex_resume),
    "gemini": (gemini_items, gemini_parse, gemini_resume),
    "cline": (cline_items, cline_parse, cline_resume),
    "hermes": (hermes_items, hermes_parse, hermes_resume),
    "opencode": (opencode_items, opencode_parse, opencode_resume),
}


def resume_command(session):
    tool = session.get("tool") or "claude"
    if tool == "claude":
        return f"cd {session['cwd']} && claude --resume {session['id']}"
    return ADAPTERS[tool][2](session) if tool in ADAPTERS else None
