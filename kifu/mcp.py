"""`kifu mcp`: an MCP server over stdio, so Claude Code can ask kifu about open ideas in the middle of a session.

A tools-only server with no dependencies: newline-delimited JSON-RPC 2.0 on stdin/stdout, logs on stderr.
Register it for all projects with `kifu install claude`, or: claude mcp add -s user kifu -- kifu mcp
"""
import json
import os
import sys

from . import brief, db, marks, report
from .extract import area_of, project_name

SUPPORTED = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = ("kifu knows the ideas left open in the user's earlier Claude Code sessions: loose ends, where each "
                "idea started, what git shows happened since. Use kifu_ideas when the user asks what they left "
                "unfinished or wanted to do here, kifu_brief to pick an idea up, kifu_mark only when they say an "
                "idea is done or not worth it.")


def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


TOOLS = [
    {"name": "kifu_ideas",
     "description": "Open ideas from the user's earlier Claude Code sessions, most potential first, with loose ends "
                    "and what git shows happened since. Defaults to the current project.",
     "inputSchema": _schema({"query": {"type": "string", "description": "words that must all appear"},
                             "scope": {"type": "string", "enum": ["project", "all"], "description": "default project"},
                             "limit": {"type": "integer", "description": "1-20, default 8"}})},
    {"name": "kifu_idea",
     "description": "One idea's trail through sessions: quotes, all loose ends, git activity, resume commands.",
     "inputSchema": _schema({"anchor": {"type": "string"}}, ["anchor"])},
    {"name": "kifu_brief",
     "description": "A compact brief to continue an idea in this session: open loose ends, what changed since, "
                    "files to look at. Accepts an anchor or search words.",
     "inputSchema": _schema({"idea": {"type": "string", "description": "anchor or words from the title"}}, ["idea"])},
    {"name": "kifu_decisions",
     "description": "Decisions made in the user's earlier sessions and why (\"SQLite instead of Postgres, because…\"), "
                    "or what the assistant promised to do later and never did. Use before reversing a design choice, "
                    "or when the user asks why something was done a certain way.",
     "inputSchema": _schema({"query": {"type": "string", "description": "words that must all appear"},
                             "kind": {"type": "string", "enum": ["decision", "promise"], "description": "default decision"},
                             "limit": {"type": "integer", "description": "1-30, default 10"}})},
    {"name": "kifu_why",
     "description": "Why code exists: for lines of a file, the earlier session and the user's own prompt that "
                    "wrote them, the idea they belonged to, and the commit. Use before changing code whose intent "
                    "is unclear, or when the user asks why something is the way it is.",
     "inputSchema": _schema({"file": {"type": "string", "description": "path, absolute or relative to the project"},
                             "start": {"type": "integer"}, "end": {"type": "integer"}}, ["file"])},
    {"name": "kifu_mark",
     "description": "Mark an idea done, dismissed, or open again. Only when the user says so.",
     "inputSchema": _schema({"anchor": {"type": "string"}, "state": {"type": "string", "enum": ["done", "dismissed", "open"]},
                             "note": {"type": "string"}}, ["anchor", "state"])},
]


def _project():
    return project_name(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def call(name, args):
    con = db.connect()
    if name == "kifu_why":
        return _why(con, args)
    if name == "kifu_decisions":
        from . import notes
        kind = args.get("kind") or "decision"
        items = notes.search(con, kind, args.get("query"), open_only=kind == "promise",
                             limit=max(1, min(int(args.get("limit") or 10), 30)))
        return {"kind": kind, "items": items}
    data = report.collect(con, habits_data={})
    if name == "kifu_ideas":
        limit = max(1, min(int(args.get("limit") or 8), 20))
        items = [l for l in data["lines"] if l["score"] > 0 and not l["mark"]]
        project = _project()
        scope = args.get("scope") or "project"
        if scope == "project" and project not in ("~", "?") and not project.startswith("/"):
            items = [l for l in items if area_of(project) in l["areas"]]
        if args.get("query"):
            words = args["query"].lower().split()
            items = [l for l in items if all(w in json.dumps(report.compact(l, data)).lower() for w in words)]
        out = {"project": area_of(project), "scope": scope, "total_open": len(items),
               "ideas": [report.compact(l, data) for l in items[:limit]]}
        if not items and scope == "project":
            out["note"] = "No open ideas for this project; call again with scope=all."
        return out
    if name in ("kifu_idea", "kifu_brief"):
        line = brief.find_line(data, args.get("anchor") or args.get("idea") or "")
        if not line:
            raise ValueError("no idea matches that anchor or those words")
        if name == "kifu_brief":
            return {"anchor": line["anchor"], "brief": brief.build(con, line, data, with_instruction=False)}
        return {k: line[k] for k in ("anchor", "title", "status", "summary", "verdict", "next", "loose", "check",
                                     "first", "last", "n_sessions")} | {
            "mark": (line["mark"] or {}).get("state"),
            "trail": [{k: t[k] for k in ("first", "project", "title", "status", "quote", "resume")} for t in line["threads"]]}
    if name == "kifu_mark":
        line = brief.find_line(data, args["anchor"])
        if not line or line["anchor"] != args["anchor"]:
            raise ValueError("kifu_mark needs an exact anchor from kifu_ideas")
        marks.set_mark(con, line["anchor"], args["state"], args.get("note") or "")
        return {"anchor": line["anchor"], "title": line["title"], "mark": args["state"]}
    raise ValueError(f"unknown tool {name}")


def _why(con, args):
    """kifu_why: blame for a line range, or for a whole file the sessions whose work it holds, most lines first."""
    from . import blame
    file = os.path.expanduser(args["file"])
    if not os.path.isabs(file):
        file = os.path.join(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd(), file)
    start = args.get("start")
    result = blame.blame(con, f"{file}:{start}-{args.get('end') or start}" if start else file)
    if start:
        return result
    return {"file": result["file"], "sessions_that_wrote_it": result["sessions_that_wrote_it"],
            "sessions": blame.by_session(result)[:10]}


def handle(msg):
    """One JSON-RPC message in, one response out (None for notifications)."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    try:
        if method == "initialize":
            asked = (msg.get("params") or {}).get("protocolVersion")
            from . import __version__
            result = {"protocolVersion": asked if asked in SUPPORTED else SUPPORTED[1],
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "kifu", "version": __version__}, "instructions": INSTRUCTIONS}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = msg.get("params") or {}
            try:
                payload = call(params.get("name"), params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "isError": False}
            except Exception as exc:  # a tool error is a result the model can read, not a protocol error
                result = {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    except Exception as exc:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(exc)}}


def serve(stdin=sys.stdin, stdout=sys.stdout):
    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n")
            stdout.flush()
            continue
        response = handle(msg)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
