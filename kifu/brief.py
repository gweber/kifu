"""A brief for picking an idea up again: what it was, what is still open, what happened since, where to look.

`claude --resume` reopens a session that may be hours long and weeks old. A brief is the opposite: a few
kilobytes of what matters, for a fresh session to start from.
"""
import json

from . import report

INSTRUCTION = """We are picking up an idea from earlier Claude Code sessions. The brief below was put together by kifu
from those sessions and from git. It may be out of date: before changing anything, look at the current state of
the files it mentions, tell me what you find, and propose how to continue with the open loose ends."""


def find_line(data, key):
    """An idea by anchor, anchor prefix, or the best match for search words."""
    for l in data["lines"]:
        if l["anchor"] == key:
            return l
    prefixed = [l for l in data["lines"] if l["anchor"].startswith(key)]
    if len(prefixed) == 1:
        return prefixed[0]
    words = key.lower().split()
    matches = [l for l in data["lines"]
               if all(w in " ".join([l["title"], l["summary"], *l["loose"]]).lower() for w in words)]
    matches.sort(key=lambda l: (l["score"] > 0 and not l["mark"], l["score"]), reverse=True)
    return matches[0] if matches else None


def files_of(con, line):
    rows = con.execute("""SELECT DISTINCT e.value FROM evidence e JOIN threads t ON t.session_id=e.session_id
                          WHERE t.line_id=? AND e.kind='write' AND e.turn_idx BETWEEN t.first_turn AND t.last_turn
                          ORDER BY e.value""", (line["id"],)).fetchall()
    return [r["value"] for r in rows]


def build(con, line, data, with_instruction=True):
    check = line.get("check") or {}
    settled = set(check.get("settled") or [])
    open_loose = [x for x in line["loose"] if x not in settled]
    out = []
    if with_instruction:
        out += [INSTRUCTION, ""]
    out += [f"# {line['title']}", "",
            f"Status: {line['status']} · project {line['project']} · {line['first'][:10]} to {line['last'][:10]} "
            f"· {line['n_sessions']} session{'s' if line['n_sessions'] != 1 else ''}"]
    if line["summary"]:
        out += ["", line["summary"]]
    quotes = [t["quote"] for t in line["threads"] if t.get("quote")]
    if quotes:
        out += ["", "In my own words at the time:"] + [f"> {q}" for q in quotes[:3]]
    if open_loose:
        out += ["", "Still open:"] + [f"- {x}" for x in open_loose]
    if settled:
        out += ["", "Looks settled by later commits:"] + [f"- {x}" for x in line["loose"] if x in settled]
    if line["next"]:
        out += ["", f"Suggested next step: {line['next']}"]
    if check.get("commits_since"):
        commits = json.loads(con.execute("SELECT commits FROM checks WHERE anchor=?", (line["anchor"],)).fetchone()[0])
        out += ["", f"Since the idea went quiet, {check['commits_since']}{'+' if check.get('commits_capped') else ''} "
                f"commits touched its files in {check.get('repo')}:"]
        out += [f"- {c['date'][:10]} {c['sha']} {c['subject'][:120]}" for c in commits[:5]]
        if check.get("note"):
            out += [f"({check['note']})"]
    files = files_of(con, line)
    if files:
        out += ["", "Files it touched:"] + [f"- {f}" for f in files[:15]]
        if len(files) > 15:
            out.append(f"- … and {len(files) - 15} more")
    out += ["", "Earlier sessions (resume one for the full history):"]
    out += [f"- {t['first'][:10]} {t['title']} ({t['status']}): {t['resume']}" for t in line["threads"] if t.get("resume")]
    return "\n".join(out) + "\n"


def workdir(line, data):
    """Where to start the new session: the latest session of the idea, and whether that is this machine."""
    sessions = {s["id"]: s for s in data["sessions"]}
    for t in reversed(line["threads"]):
        s = sessions.get(t["session"])
        if s and s["cwd"]:
            return s["cwd"], s["host"], s["local"]
    return None, None, True


def collect(con):
    return report.collect(con, habits_data={})
