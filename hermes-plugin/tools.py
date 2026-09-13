"""Agent tools. Short schemas on purpose: every enabled tool costs prompt tokens on every turn."""
from __future__ import annotations

import json
from typing import Any

try:
    from . import kifu_client as client
except ImportError:          # loaded by path
    import kifu_client as client  # type: ignore

TOOLSET = "kifu"


def _schema(name, description, props, required=None):
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": props, "required": required or []}}


def ideas(args: dict) -> dict:
    limit = max(1, min(int(args.get("limit") or 8), 20))
    status, data = client.get("/api/lines", open="true", compact="true", q=args.get("query"),
                              area=args.get("project"), limit=limit)
    if status != 200:
        return {"ok": False, "error": f"kifu answered {status}: {data}"}
    return {"ok": True, "total_open": data["total"], "ideas": data["items"],
            "hint": "kifu_idea(anchor) shows where an idea started and how to resume it"}


def idea(args: dict) -> dict:
    status, data = client.get(f"/api/lines/{args['anchor']}")
    if status != 200:
        return {"ok": False, "error": f"no idea with anchor {args['anchor']}"}
    return {"ok": True, "title": data["title"], "status": data["status"], "mark": (data["mark"] or {}).get("state"),
            "summary": data["summary"], "verdict": data["verdict"], "next": data["next"],
            "loose_ends": data["loose"], "first": data["first"][:10], "last": data["last"][:10],
            "trail": [{"date": t["first"][:10], "project": t["project"], "title": t["title"], "status": t["status"],
                       "quote": t["quote"], "resume": t["resume"]} for t in data["threads"]]}


def mark(args: dict) -> dict:
    status, data = client.request("PUT", f"/api/lines/{args['anchor']}/mark",
                                  body={"state": args["state"], "note": args.get("note") or ""})
    if status != 200:
        return {"ok": False, "error": f"kifu answered {status}: {data}"}
    return {"ok": True, "title": data["title"], "mark": (data["mark"] or {}).get("state", "open")}


TOOLS = [
    (_schema("kifu_ideas",
             "Open ideas from the user's past Claude Code sessions: loose ends, where each stopped, how to resume. "
             "Use when they ask what they left unfinished, what they wanted to do in a project, or where an idea went.",
             {"query": {"type": "string", "description": "words that must all appear"},
              "project": {"type": "string"}, "limit": {"type": "integer", "description": "1-20, default 8"}}),
     ideas, "🪨"),
    (_schema("kifu_idea", "One idea's trail through sessions: quotes, loose ends, resume commands.",
             {"anchor": {"type": "string", "description": "from kifu_ideas"}}, ["anchor"]),
     idea, "🧵"),
    (_schema("kifu_mark", "Mark an idea done, dismissed or open again. Only when the user says so.",
             {"anchor": {"type": "string"}, "state": {"type": "string", "enum": ["done", "dismissed", "open"]},
              "note": {"type": "string"}}, ["anchor", "state"]),
     mark, "✅"),
]


def _wrap(fn):
    def handler(args: dict, **_: Any) -> str:
        try:
            return json.dumps(fn(args or {}), ensure_ascii=False)
        except client.KifuUnavailable as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        except Exception as exc:  # a tool must never break the turn
            return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return handler


def register_tools(ctx) -> None:
    for schema, fn, emoji in TOOLS:
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema, handler=_wrap(fn), emoji=emoji,
                          description=schema["description"])
