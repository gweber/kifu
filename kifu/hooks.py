"""Claude Code hooks: `kifu hook session-start` and `kifu hook session-end`.

Both read the hook's JSON from stdin and must never get in the way of a session: they import only the standard
library and kifu's light modules, finish in well under a second, and on any error exit quietly.

session-start  shows the open ideas of the project the session starts in, to the user and to Claude.
session-end    queues the finished session, so kifu analyzes it minutes later instead of on the next manual run.
"""
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.request

from . import config, db
from .extract import area_of, project_name

TOP = 3


def _read_stdin():
    try:
        return json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        return {}


def open_ideas_for(con, cwd, top=TOP):
    """Open, unmarked ideas whose project is the one cwd belongs to, most aji first."""
    project = project_name(cwd)
    if project in ("~", "?") or project.startswith("/"):
        return project, []
    area = area_of(project)
    rows = con.execute("""SELECT l.anchor, l.title, l.status, l.loose_ends, l.last_ts, l.score, c.settled
                          FROM lines l LEFT JOIN checks c ON c.anchor=l.anchor
                          WHERE l.score > 0 AND l.anchor NOT IN (SELECT anchor FROM marks)
                          AND EXISTS (SELECT 1 FROM json_each(l.areas) WHERE json_each.value=?)
                          ORDER BY l.score DESC""", (area,)).fetchall()
    ideas = []
    for r in rows:
        loose = json.loads(r["loose_ends"] or "[]")
        settled = json.loads(r["settled"] or "[]")
        if loose and len(settled) == len(loose):
            continue            # looks done according to git
        still_open = [x for i, x in enumerate(loose, 1) if i not in settled]
        ideas.append({"anchor": r["anchor"], "title": r["title"], "status": r["status"], "loose": still_open,
                      "last": r["last_ts"][:10]})
        if len(ideas) == top:
            break
    return area, ideas


def session_start(payload):
    if payload.get("source") not in (None, "startup"):
        return None
    cwd = payload.get("cwd") or os.getcwd()
    if not os.path.exists(config.get().db_path):
        return None
    con = db.connect()
    area, ideas = open_ideas_for(con, cwd)
    if not ideas:
        return None
    user_lines = [f"kifu: {len(ideas)} open idea{'s' if len(ideas) > 1 else ''} in {area}"]
    context = [f"kifu found these open ideas from earlier sessions in {area} (the user may want to pick one up; "
               f"do not start on them unasked). Details: the kifu_idea / kifu_brief tools, or `kifu brief <anchor>`."]
    for i in ideas:
        first = f" — next: {i['loose'][0]}" if i["loose"] else ""
        user_lines.append(f"  • {i['title']} ({i['status']}, since {i['last']}){first}")
        context.append(f"- {i['title']} [{i['status']}, anchor {i['anchor']}]"
                       + "".join(f"\n  - {x}" for x in i["loose"][:4]))
    return {"systemMessage": "\n".join(user_lines),
            "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n".join(context)}}


def queue_path():
    return os.path.join(config.get().data_dir, "queue.jsonl")


def session_end(payload):
    """Queue the session; wake the service, or start a detached drain when there is no service."""
    cfg = config.get()
    os.makedirs(cfg.data_dir, exist_ok=True)
    entry = {"session_id": payload.get("session_id"), "transcript": payload.get("transcript_path"),
             "reason": payload.get("reason"), "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
    with open(queue_path(), "a") as fh:
        fh.write(json.dumps(entry) + "\n")
    url = f"http://127.0.0.1:{cfg.server_port}/api/queue"
    try:
        req = urllib.request.Request(url, data=b"{}", method="POST", headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=0.5):
            return None                 # the service drains the queue
    except OSError:
        pass
    log = open(os.path.join(cfg.data_dir, "drain.log"), "a")
    subprocess.Popen([sys.executable, "-m", "kifu", "drain"], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                     start_new_session=True, env={**os.environ, **({"KIFU_CONFIG": cfg.path} if cfg.path else {})})
    return None


def main(argv):
    event = argv[0] if argv else ""
    payload = _read_stdin()
    try:
        out = {"session-start": session_start, "session-end": session_end}[event](payload)
    except KeyError:
        print(f"kifu hook: unknown event {event!r} (session-start, session-end)", file=sys.stderr)
        return 0
    except Exception as exc:  # a hook must never break a session
        print(f"kifu hook {event}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0
    if out:
        print(json.dumps(out))
    return 0
