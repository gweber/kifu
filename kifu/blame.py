"""`kifu blame file[:line[-line]]`: git blame for intent — the session, and your prompt, behind a line of code.

git says which commit last changed a line; kifu knows which sessions wrote the file. Those sessions' transcripts
still hold every edit, so the edits to the file are replayed in order: a line is introduced by an edit whose new
text has it and whose old text (the Edit's old_string, or the file's previous full Write) did not. The latest such
edit at or before the commit is the answer, with the turn it happened in and the idea that turn belonged to.

When no edit text matches (a tool without edit contents, a line reformatted by hand), the commit's subject is
looked up among the commits sessions made, and failing that the last session that wrote the file before the
commit. Every answer says how it was found: edit, commit or time.
"""
import glob
import json
import os
import socket
import subprocess

from . import agents, config
from .extract import WRITE_TOOLS, moved

SLACK_SECONDS = 180             # an edit a moment after the commit's author time can still be in it
TIME_WINDOW_SECONDS = 6 * 3600  # a commit this soon after a session's last write to the file is likely its work
TRIVIAL = {"", "{", "}", "(", ")", "[", "]", "};", "});", "*/", "/*", "else:", "try:", "pass", "return", "end",
           "fi", "done", "</div>", "<div>", "\"\"\"", "'''", "```"}


class BlameError(Exception):
    pass


def parse_target(target):
    """'path', 'path:12' or 'path:12-20' -> (absolute path, start, end); a line range of None means the whole file."""
    path, start, end = target, None, None
    head, sep, tail = target.rpartition(":")
    if sep and tail.replace("-", "").isdigit() and tail.strip("-"):
        path = head
        a, _, b = tail.partition("-")
        start, end = int(a), int(b or a)
    return os.path.abspath(os.path.expanduser(path)), start, end


def git_blame(path, start, end):
    """[{line, sha, time, summary, text}] from git blame --porcelain; sha None for uncommitted lines."""
    if not os.path.isfile(path):
        raise BlameError(f"{path}: no such file")
    cmd = ["git", "-C", os.path.dirname(path), "blame", "--porcelain"]
    if start:
        cmd += ["-L", f"{start},{end}"]
    r = subprocess.run(cmd + ["--", os.path.basename(path)], capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise BlameError(r.stderr.strip() or "git blame failed")
    commits, out, cur = {}, [], None
    for raw in r.stdout.splitlines():
        if raw.startswith("\t"):
            info = commits[cur["sha"]]
            uncommitted = set(cur["sha"]) == {"0"}
            out.append({"line": cur["line"], "sha": None if uncommitted else cur["sha"],
                        "time": None if uncommitted else int(info.get("author-time", 0)),
                        "summary": None if uncommitted else info.get("summary"), "text": raw[1:]})
            continue
        parts = raw.split(" ")
        if len(parts) >= 3 and len(parts[0]) == 40 and parts[1].isdigit():
            cur = {"sha": parts[0], "line": int(parts[2])}
            commits.setdefault(parts[0], {})
        elif cur:
            key, _, value = raw.partition(" ")
            commits[cur["sha"]][key] = value
    return out


def _norm(text):
    return text.strip()


def _lines(text):
    return {_norm(x) for x in (text or "").splitlines()}


def _epoch(ts):
    import datetime as dt
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def session_edits(session_path, path):
    """Every edit to path in a Claude Code transcript and its subagents: [(ts, new lines, old lines or None, full)].
    old None with full=True is a whole-file Write, compared later with the file's previous Write."""
    files = [session_path]
    sid = os.path.basename(session_path)[:-len(".jsonl")]
    files += glob.glob(os.path.join(os.path.dirname(session_path), sid, "subagents", "**", "*.jsonl"), recursive=True)
    edits = []
    for f in files:
        if not os.path.isfile(f):
            continue
        with open(f, errors="replace") as fh:
            for raw in fh:
                if '"tool_use"' not in raw or os.path.basename(path) not in raw:
                    continue
                try:
                    r = json.loads(raw)
                except ValueError:
                    continue
                for block in (r.get("message") or {}).get("content") or []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("name") not in WRITE_TOOLS:
                        continue
                    inp = block.get("input") or {}
                    fp = inp.get("file_path") or inp.get("notebook_path")
                    if not fp or moved(fp) != path:
                        continue
                    ts = r.get("timestamp")
                    if block["name"] == "Write":
                        edits.append((ts, _lines(inp.get("content")), None, True))
                    elif block["name"] == "Edit":
                        edits.append((ts, _lines(inp.get("new_string")), _lines(inp.get("old_string")), False))
                    elif block["name"] == "MultiEdit":
                        for e in inp.get("edits") or []:
                            edits.append((ts, _lines(e.get("new_string")), _lines(e.get("old_string")), False))
                    elif block["name"] == "NotebookEdit":
                        edits.append((ts, _lines(inp.get("new_source")), None, False))
    return edits


def _local_hosts():
    return {s.host for s in config.get().source_list() if not s.ssh} or {socket.gethostname()}


def _session(con, sid):
    s = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    return dict(s) if s else None


def _asked(con, sid, idx):
    """The last prompt at or before a turn that carries a topic: "go on" says what, the prompt before it says why."""
    from .embed import is_continuation
    for row in con.execute("SELECT prompt FROM turns WHERE session_id=? AND idx<=? AND dup_of IS NULL "
                           "ORDER BY idx DESC LIMIT 20", (sid, idx)):
        if not is_continuation(row["prompt"]):
            return row["prompt"]
    return None


def _turn_for(con, sid, ts, turn_idx=None):
    if turn_idx is None:
        row = con.execute("SELECT * FROM turns WHERE session_id=? AND ts<=? ORDER BY idx DESC LIMIT 1", (sid, ts)).fetchone()
    else:
        row = con.execute("SELECT * FROM turns WHERE session_id=? AND idx=?", (sid, turn_idx)).fetchone()
    return dict(row) if row else None


def _idea_for(con, sid, idx):
    if idx is None:
        return None
    t = con.execute("""SELECT t.title, t.quote, l.anchor, COALESCE(m.title, l.title) AS line_title, l.status
                       FROM threads t LEFT JOIN lines l ON l.id=t.line_id LEFT JOIN marks m ON m.anchor=l.anchor
                       WHERE t.session_id=? AND t.first_turn<=? AND t.last_turn>=?
                       ORDER BY t.last_turn - t.first_turn LIMIT 1""", (sid, idx, idx)).fetchone()
    if not t:
        return None
    return {"anchor": t["anchor"], "title": t["line_title"] or t["title"], "status": t["status"], "quote": t["quote"]}


def _describe_session(con, sid, ts, turn_idx=None):
    s = _session(con, sid)
    if not s:
        return None
    turn = _turn_for(con, sid, ts, turn_idx)
    idx = turn["idx"] if turn else None
    info = {"session": sid, "tool": s["tool"] or "claude", "host": s["host"], "project": s["project"],
            "session_title": s["title"] or s["ai_title"] or "", "at": ts,
            "turn": idx, "prompt": (turn or {}).get("prompt"), "idea": _idea_for(con, sid, idx)}
    asked = _asked(con, sid, idx) if idx is not None else None
    info["asked"] = asked if asked and asked != info["prompt"] else None
    local = not s["host"] or s["host"] in _local_hosts()
    cmd = agents.resume_command({**s, "tool": s["tool"] or "claude"})
    info["resume"] = cmd if not cmd or local else f"ssh -t {s['host']} '{cmd}'"
    return info


def blame(con, target, max_sessions=40):
    path, start, end = parse_target(target)
    blamed = git_blame(path, start, end)
    writers = con.execute("""SELECT e.session_id, s.path, s.tool, MIN(e.ts) first, MAX(e.ts) last FROM evidence e
                             JOIN sessions s ON s.id=e.session_id WHERE e.kind='write' AND e.value=?
                             GROUP BY e.session_id ORDER BY last DESC LIMIT ?""", (path, max_sessions)).fetchall()
    # Replay every recorded edit to the file in time order: which edit introduced which line.
    edits = []
    for w in writers:
        if (w["tool"] or "claude") == "claude" and w["path"]:
            edits += [(ts, w["session_id"], new, old, full) for ts, new, old, full in session_edits(w["path"], path)]
    edits.sort(key=lambda e: e[0] or "")
    introduced = []                     # (epoch, session, ts, lines this edit introduced)
    known = None                        # the file's lines as far as the replay knows them, from the first Write on
    for ts, sid, new, old, full in edits:
        if full:
            lines = new - known if known is not None else new
            known = set(new)
        else:
            lines = new - (old or set())
            if known is not None:
                known = (known - (old or set())) | new
        introduced.append((_epoch(ts), sid, ts, lines))

    groups = []
    for b in blamed:
        key = _norm(b["text"])
        found = None
        if key not in TRIVIAL and len(key) >= 3:
            limit = (b["time"] + SLACK_SECONDS) if b["time"] else float("inf")
            for epoch, sid, ts, lines in reversed(introduced):
                if epoch <= limit and key in lines:
                    found = ("edit", sid, ts, None)
                    break
        if found is None and b["summary"]:
            c = con.execute("SELECT session_id, ts, turn_idx FROM evidence WHERE kind='commit' AND value=? "
                            "ORDER BY ts DESC LIMIT 1", (b["summary"],)).fetchone()
            if c:
                found = ("commit", c["session_id"], c["ts"], c["turn_idx"])
        if found is None and b["time"]:
            before = [w for w in writers if w["first"] and _epoch(w["first"]) <= b["time"] + SLACK_SECONDS]
            if before:
                w = max(before, key=lambda w: w["last"])
                if b["time"] - _epoch(w["last"]) < TIME_WINDOW_SECONDS:
                    found = ("time", w["session_id"], w["last"], None)
        sig = (b["sha"], found[1] if found else None, found[2] if found else None)
        if groups and groups[-1]["sig"] == sig and groups[-1]["end"] == b["line"] - 1:
            groups[-1]["end"] = b["line"]
            groups[-1]["text"].append(b["text"])
            continue
        groups.append({"sig": sig, "start": b["line"], "end": b["line"], "text": [b["text"]],
                       "commit": {"sha": b["sha"][:10], "time": b["time"], "summary": b["summary"]} if b["sha"] else None,
                       "how": found[0] if found else None,
                       "origin": _describe_session(con, found[1], found[2], found[3]) if found else None})
    for g in groups:
        g.pop("sig")
    return {"file": path, "sessions_that_wrote_it": len(writers), "edits_replayed": len(edits), "ranges": groups}


def by_session(result):
    """A whole file's blame folded per session: how many lines each session's work left, most first."""
    folded = {}
    for g in result["ranges"]:
        o = g["origin"]
        key = o["session"] if o else None
        f = folded.setdefault(key, {"origin": o, "lines": 0, "how": set()})
        f["lines"] += g["end"] - g["start"] + 1
        if g["how"]:
            f["how"].add(g["how"])
    return sorted(({**f, "how": sorted(f["how"])} for f in folded.values()), key=lambda f: -f["lines"])


def _quote(text, width):
    text = " ".join((text or "").split())
    return f"“{text[:width]}{'…' if len(text) > width else ''}”"


def format_text(result, width=400, whole_file=False):
    import datetime as dt
    out = [f"{result['file']}  ({result['sessions_that_wrote_it']} session(s) wrote it, {result['edits_replayed']} edits replayed)"]
    if whole_file:
        total = sum(g["end"] - g["start"] + 1 for g in result["ranges"])
        for f in by_session(result):
            o = f["origin"]
            share = f"{f['lines']:5} lines ({100 * f['lines'] // max(total, 1):2}%)"
            if not o:
                out.append(f"\n{share}  from outside the recorded sessions")
                continue
            out.append(f"\n{share}  {o['at'][:10]} · {o['tool']} · {o['session_title']}  [{', '.join(f['how'])}]")
            out.append(f"  you: {_quote(o['asked'] or o['prompt'], width)}")
            if o["idea"]:
                out.append(f"  idea: {o['idea']['title']} ({o['idea']['status']})")
        return "\n".join(out)
    for g in result["ranges"]:
        span = f"{g['start']}" if g["start"] == g["end"] else f"{g['start']}-{g['end']}"
        c = g["commit"]
        commit = (f"{c['sha']} {dt.datetime.fromtimestamp(c['time']).strftime('%Y-%m-%d')} “{c['summary']}”"
                  if c else "not committed yet")
        out.append(f"\nlines {span} · {commit}")
        preview = g["text"][0].strip()
        out.append(f"  | {preview[:120]}" + (f"  (+{len(g['text']) - 1} more)" if len(g["text"]) > 1 else ""))
        o = g["origin"]
        if not o:
            out.append("  no session found: written outside the recorded sessions")
            continue
        how = {"edit": "an edit in the session wrote this line", "commit": "the session made this commit",
               "time": "the last session that wrote the file before the commit (a guess by time)"}[g["how"]]
        out.append(f"  {o['at'][:16].replace('T', ' ')} · {o['tool']} · {o['project']} · {o['session_title']}  [{how}]")
        if o["prompt"]:
            out.append(f"  you: {_quote(o['prompt'], width)}")
        if o["asked"]:
            out.append(f"  before that: {_quote(o['asked'], width)}")
        if o["idea"]:
            out.append(f"  idea: {o['idea']['title']} ({o['idea']['status']}, {o['idea']['anchor']})")
        if o["resume"]:
            out.append(f"  {o['resume']}")
    return "\n".join(out)
