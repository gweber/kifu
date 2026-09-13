"""Stage 5: the data behind the web app, and a static self-contained export of it."""
import datetime as dt
import json
import os
import socket

from . import config, habits, verify
from .extract import area_of

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "report.html")


def collect(con, habits_data=None):
    # A source without ssh is read from this machine, so its sessions resume here.
    local_hosts = {src.host for src in config.get().source_list() if not src.ssh} or {socket.gethostname()}
    sess = []
    for s in con.execute("SELECT * FROM sessions WHERE automated=0 ORDER BY started"):
        sess.append({"id": s["id"], "project": s["project"], "area": area_of(s["project"]), "cwd": s["cwd"],
                     "host": s["host"] or "", "local": not s["host"] or s["host"] in local_hosts,
                     "title": s["title"] or s["ai_title"] or "", "started": s["started"], "ended": s["ended"],
                     "turns": s["n_turns"]})
    by_id = {s["id"]: s for s in sess}
    threads = con.execute("SELECT * FROM threads ORDER BY first_ts").fetchall()
    by_line = {}
    for t in threads:
        by_line.setdefault(t["line_id"], []).append(t)
    marks = {m["anchor"]: dict(m) for m in con.execute("SELECT anchor, state, note, updated FROM marks")}
    checks = {c["anchor"]: c for c in con.execute("SELECT * FROM checks")}
    lines = []
    for l in con.execute("SELECT * FROM lines ORDER BY score DESC, last_ts DESC"):
        members = by_line.get(l["id"], [])
        lines.append({
            "id": l["id"], "anchor": l["anchor"], "mark": marks.get(l["anchor"]), "title": l["title"],
            "check": verify.summarize(checks.get(l["anchor"]), json.loads(l["loose_ends"] or "[]")),
            "summary": l["summary"], "status": l["status"], "project": l["project"],
            "areas": json.loads(l["areas"] or "[]"), "first": l["first_ts"], "last": l["last_ts"],
            "n_sessions": l["n_sessions"], "score": l["score"], "verdict": l["verdict"] or "",
            "loose": json.loads(l["loose_ends"] or "[]"), "next": l["next_step"] or "",
            "kinds": sorted({t["kind"] for t in members}),
            "threads": [{"session": t["session_id"], "title": t["title"], "status": t["status"], "kind": t["kind"],
                         "first": t["first_ts"], "last": t["last_ts"], "quote": t["quote"],
                         "loose": json.loads(t["loose_ends"] or "[]"),
                         "project": by_id[t["session_id"]]["project"] if t["session_id"] in by_id else "",
                         "resume": resume_command(by_id[t["session_id"]]) if t["session_id"] in by_id else None}
                        for t in members],
        })
    for line in lines:
        # Every loose end looks settled by later commits: probably done outside the sessions.
        if line["check"] and line["check"]["likely_done"] and line["score"] > 0:
            line["score"] = round(line["score"] * verify.LIKELY_DONE_FACTOR, 2)
    lines.sort(key=lambda l: l["last"], reverse=True)           # among equal scores, most recent first
    lines.sort(key=lambda l: l["score"], reverse=True)
    stats = {
        "sessions": len(sess),
        "turns": sum(s["turns"] for s in sess),
        "threads": len(threads),
        "lines": len(lines),
        "open": sum(1 for l in lines if l["score"] > 0 and not l["mark"]),
        "loose": sum(len(l["loose"]) for l in lines if l["score"] > 0 and not l["mark"]),
        "marked": sum(1 for l in lines if l["mark"]),
        "areas": sorted({a for l in lines if l["score"] > 0 for a in l["areas"]}),
        "now": con.execute("SELECT MAX(ended) FROM sessions").fetchone()[0],
    }
    return {"lines": lines, "sessions": sess, "stats": stats,
            "habits": habits_data if habits_data is not None else habits.compute(con)}


def resume_command(session):
    """How to reopen a session: locally, or through ssh on the machine it ran on."""
    cmd = f"cd {session['cwd']} && claude --resume {session['id']}"
    return cmd if session["local"] else f"ssh -t {session['host']} '{cmd}'"


def quiet_days(line, now):
    a = dt.datetime.fromisoformat(line["last"].replace("Z", "+00:00"))
    b = dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
    return max(0, (b - a).days)


def compact(line, data):
    """An idea in a few hundred bytes: what an agent tool or a notification needs."""
    sessions = {s["id"]: s for s in data["sessions"]}
    latest = sessions.get(line["threads"][-1]["session"]) if line["threads"] else None
    return {"anchor": line["anchor"], "title": line["title"], "status": line["status"], "project": line["project"],
            "areas": line["areas"], "first": line["first"][:10], "last": line["last"][:10],
            "quiet_days": quiet_days(line, data["stats"]["now"]), "sessions": line["n_sessions"],
            "score": line["score"], "open": line["score"] > 0 and not line["mark"],
            "mark": (line["mark"] or {}).get("state"), "verdict": line["verdict"], "next": line["next"],
            "loose_ends": line["loose"][:3], "more_loose_ends": max(0, len(line["loose"]) - 3),
            "quote": line["threads"][0]["quote"] if line["threads"] else "",
            "resume": resume_command(latest) if latest else None,
            "activity": _activity(line["check"])}


def _activity(check):
    """None when git could not look (no repository, host unreachable): silence, not "no commits"."""
    if not check or (check["error"] and not check["commits_since"]):
        return None
    return {k: check[k] for k in ("commits_since", "commits_capped", "latest_commit", "settled", "likely_done", "note",
                                  "missing_files")}


def render(con, out_path):
    data = json.dumps(collect(con), ensure_ascii=False).replace("</", "<\\/")
    with open(TEMPLATE_PATH) as fh:
        html = fh.read().replace("/*__KIFU_DATA__*/null", data)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        fh.write(html)
    return out_path
