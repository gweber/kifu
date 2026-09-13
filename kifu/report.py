"""Stage 5: the data behind the web app, and a static self-contained export of it."""
import datetime as dt
import json
import os
import socket

from . import config, habits, verify
from .extract import area_of
from .link import thread_key

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "report.html")


def collect(con, habits_data=None):
    # A source without ssh is read from this machine, so its sessions resume here.
    local_hosts = {src.host for src in config.get().source_list() if not src.ssh} or {socket.gethostname()}
    sess = []
    for s in con.execute("SELECT * FROM sessions WHERE automated=0 ORDER BY started"):
        sess.append({"id": s["id"], "project": s["project"], "area": area_of(s["project"]), "cwd": s["cwd"],
                     "host": s["host"] or "", "local": not s["host"] or s["host"] in local_hosts,
                     "tool": s["tool"] or "claude",
                     "title": s["title"] or s["ai_title"] or "", "started": s["started"], "ended": s["ended"],
                     "turns": s["n_turns"]})
    for s in sess:
        s["resume"] = resume_command(s)
    by_id = {s["id"]: s for s in sess}
    threads = con.execute("SELECT * FROM threads ORDER BY first_ts").fetchall()
    by_line = {}
    for t in threads:
        by_line.setdefault(t["line_id"], []).append(t)
    marks = {m["anchor"]: dict(m) for m in con.execute("SELECT anchor, state, note, updated, title FROM marks")}
    checks = {c["anchor"]: c for c in con.execute("SELECT * FROM checks")}
    lines = []
    for l in con.execute("SELECT * FROM lines ORDER BY score DESC, last_ts DESC"):
        members = by_line.get(l["id"], [])
        lines.append({
            "id": l["id"], "anchor": l["anchor"],
            "mark": marks[l["anchor"]] if marks.get(l["anchor"], {}).get("state") else None,
            "title": (marks.get(l["anchor"]) or {}).get("title") or l["title"],
            "analyzed_title": l["title"] if (marks.get(l["anchor"]) or {}).get("title") else None,
            "check": verify.summarize(checks.get(l["anchor"]), json.loads(l["loose_ends"] or "[]")),
            "summary": l["summary"], "status": l["status"], "project": l["project"],
            "areas": json.loads(l["areas"] or "[]"), "first": l["first_ts"], "last": l["last_ts"],
            "n_sessions": l["n_sessions"], "score": l["score"], "verdict": l["verdict"] or "",
            "loose": json.loads(l["loose_ends"] or "[]"), "next": l["next_step"] or "",
            "kinds": sorted({t["kind"] for t in members}),
            "threads": [{"session": t["session_id"], "title": t["title"], "status": t["status"], "kind": t["kind"],
                         "first": t["first_ts"], "last": t["last_ts"], "quote": t["quote"],
                         "loose": json.loads(t["loose_ends"] or "[]"), "key": thread_key(t),
                         "project": by_id[t["session_id"]]["project"] if t["session_id"] in by_id else "",
                         "resume": by_id[t["session_id"]]["resume"] if t["session_id"] in by_id else None}
                        for t in members],
        })
    calibrate(lines)
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


CALIBRATE_AFTER = 10       # marks before the user's own verdicts start to shift scores
PRIOR_DISMISSED = 0.25     # what a dismissal rate is assumed to be before there is evidence
PRIOR_WEIGHT = 4


def calibrate(lines):
    """Learn from marks: ideas of a kind or project the user keeps dismissing score lower, ones they finish higher.

    Each kind and each project gets a dismissal rate, smoothed toward a prior so two dismissals do not bury a
    whole category. The factor stays within [0.5, 1.2], applies to open unmarked ideas, and says why.
    """
    marked = [l for l in lines if l["mark"]]
    if len(marked) < CALIBRATE_AFTER:
        return
    stats = {}
    for l in marked:
        dismissed = l["mark"]["state"] == "dismissed"
        for dim, value in (("kind", l["kinds"][0] if l["kinds"] else "?"), ("project", l["areas"][0] if l["areas"] else "?")):
            n, d = stats.get((dim, value), (0, 0))
            stats[(dim, value)] = (n + 1, d + dismissed)
    for l in lines:
        if l["mark"] or l["score"] <= 0:
            continue
        factor, reasons = 1.0, []
        for dim, value in (("kind", l["kinds"][0] if l["kinds"] else "?"), ("project", l["areas"][0] if l["areas"] else "?")):
            n, d = stats.get((dim, value), (0, 0))
            if n < 3:
                continue
            rate = (d + PRIOR_DISMISSED * PRIOR_WEIGHT) / (n + PRIOR_WEIGHT)
            f = 1 - (rate - PRIOR_DISMISSED) * 0.8
            factor *= f
            if abs(f - 1) >= 0.05:
                what = f"{value} ideas" if dim == "project" else f"{value}s" if not value.endswith("s") else value
                reasons.append(f"you dismissed {d} of {n} marked {what}")
        factor = max(0.5, min(1.2, factor))
        if abs(factor - 1) >= 0.05:
            l["score"] = round(l["score"] * factor, 2)
            l["calibration"] = {"factor": round(factor, 2), "reason": "; ".join(reasons)}


def resume_command(session):
    """How to reopen a session in the tool it came from: locally, or through ssh on the machine it ran on."""
    from . import agents
    cmd = agents.resume_command(session)
    if not cmd:
        return None
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
