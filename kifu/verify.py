"""Stage 5: check open ideas against the repositories they touched.

A session can only say what happened in the session. Work often finishes elsewhere: a commit from the
terminal, another agent, a later session that never mentions the idea. For every open idea this looks at the
files written while the idea was worked on, on the machine the session ran on, and asks git what happened to
them after the idea went quiet. When later commits exist, a model reads their messages against the idea's loose
ends and says which look settled.

The result is evidence shown next to the idea, not a verdict: an idea whose loose ends all look settled drops
down the ranking and out of the digest, but stays visible.
"""
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import shlex
import subprocess

from . import analyze, config

AFTER_QUIET = dt.timedelta(hours=1)     # commits this soon after the last move belong to the session itself
MAX_FILES = 40
MAX_COMMITS = 20
LIKELY_DONE_FACTOR = 0.2

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "settled": {"type": "array", "items": {"type": "integer"},
                    "description": "numbers of the loose ends the commits appear to settle"},
        "note": {"type": "string"},
    },
    "required": ["settled", "note"],
}

JUDGE_SYSTEM = """{user_cap} left an idea with open loose ends in a coding session. Later, commits touched the
files that session had written. Decide which loose ends the commit messages show as done.

Count a loose end as settled only when a commit message clearly does that thing. Related work, refactors or
"wip" do not settle anything. When unsure, leave it open.
note: one sentence for {user} about what happened after the session, in English.
Return JSON only."""


class Host:
    """Run a command on the machine a session came from: here, or over the source's ssh destination."""

    def __init__(self, ssh=None):
        self.ssh = ssh

    def run(self, args, timeout=30):
        if self.ssh:
            cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", self.ssh, shlex.join(args)]
        else:
            cmd = args
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr


def hosts():
    return {s.host: Host(s.ssh) for s in config.get().source_list()}


def _idea_files(con, line):
    """Files written during the idea's own turns, per (host, cwd) of the session."""
    out = {}
    for t in con.execute("""SELECT t.session_id, t.first_turn, t.last_turn, s.host, s.cwd FROM threads t
                            JOIN sessions s ON s.id=t.session_id WHERE t.line_id=?""", (line["id"],)):
        rows = con.execute("""SELECT DISTINCT value FROM evidence WHERE session_id=? AND kind='write'
                              AND turn_idx BETWEEN ? AND ?""", (t["session_id"], t["first_turn"], t["last_turn"]))
        out.setdefault((t["host"], t["cwd"]), set()).update(r["value"] for r in rows)
    return {k: sorted(v) for k, v in out.items() if v}


def check_line(con, line, host_map):
    """git evidence for one idea: repository, files missing now, commits since it went quiet."""
    groups = _idea_files(con, line)
    if not groups:
        return {"files": 0, "missing": 0, "commits": [], "error": "no files were written for this idea"}
    since = (dt.datetime.fromisoformat(line["last_ts"].replace("Z", "+00:00")) + AFTER_QUIET).isoformat()
    result = {"files": 0, "missing": 0, "commits": [], "repo": None, "host": None, "error": None}
    seen = set()
    for (host_name, cwd), files in groups.items():
        if not cwd:
            result["error"] = result["error"] or "the session recorded no working directory"
            continue
        host = host_map.get(host_name)
        if host is None:
            result["error"] = f"no source configured for host {host_name}"
            continue
        try:
            code, out, err = host.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"])
        except (subprocess.TimeoutExpired, OSError) as exc:
            result["error"] = f"{host_name} unreachable: {exc}"
            continue
        if code:
            result["error"] = f"{cwd} on {host_name} is not a git repository"
            continue
        root = out.strip()
        inside = [f for f in files if f.startswith(root.rstrip("/") + "/")][:MAX_FILES]
        if not inside:
            continue
        rel = [f[len(root.rstrip("/")) + 1:] for f in inside]
        result.update(repo=root, host=host_name)
        result["files"] += len(rel)
        probe = "; ".join(f"[ -e {shlex.quote(f)} ] || echo {shlex.quote(f)}" for f in rel)
        code, out, _ = host.run(["sh", "-c", f"cd {shlex.quote(root)} && {probe}"])
        result["missing"] += len([x for x in out.splitlines() if x.strip()]) if code == 0 else 0
        code, out, err = host.run(["git", "-C", root, "log", f"--since={since}", "--no-merges",
                                   f"-n{MAX_COMMITS}", "--format=%h%x1f%cI%x1f%s", "--", *rel])
        if code:
            result["error"] = err.strip()[-200:] or "git log failed"
            continue
        for row in out.splitlines():
            sha, date, subject = (row.split("\x1f") + ["", ""])[:3]
            if sha and sha not in seen:
                seen.add(sha)
                result["commits"].append({"sha": sha, "date": date, "subject": subject})
    result["commits"].sort(key=lambda c: c["date"], reverse=True)
    return result


def judge(con, line, commits, backend):
    """Which loose ends the later commits settle. Cached by the loose ends and the commits themselves."""
    loose = json.loads(line["loose_ends"] or "[]")
    if not loose or not commits:
        return [], ""
    key = hashlib.sha1(json.dumps([loose, [c["sha"] for c in commits], backend]).encode()).hexdigest()[:16]
    hit = con.execute("SELECT settled, note FROM judged WHERE input_hash=?", (key,)).fetchone()
    if hit:
        return json.loads(hit["settled"]), hit["note"]
    user = (f"Idea: {line['title']}\nWent quiet: {line['last_ts'][:10]}\n\nLoose ends:\n"
            + "\n".join(f"{i}. {x}" for i, x in enumerate(loose, 1))
            + "\n\nLater commits touching the idea's files (newest first):\n"
            + "\n".join(f"- {c['date'][:10]} {c['subject']}" for c in commits))
    out = analyze.BACKENDS[backend](analyze.system_prompt(JUDGE_SYSTEM), user, schema=JUDGE_SCHEMA)
    settled = sorted({i for i in out.get("settled", []) if 1 <= i <= len(loose)})
    con.execute("INSERT OR REPLACE INTO judged VALUES (?,?,?)", (key, json.dumps(settled), out.get("note", "")))
    con.commit()
    return settled, out.get("note", "")


def verify(con_factory, backend=None, workers=None, log=print):
    cfg = config.get()
    backend = "openai" if backend == "local" else (backend or cfg.backend)
    con = con_factory()
    lines = con.execute("""SELECT * FROM lines WHERE score > 0
                           AND anchor NOT IN (SELECT anchor FROM marks WHERE state IS NOT NULL)""").fetchall()
    host_map = hosts()
    log(f"checking {len(lines)} open ideas against git")
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")

    def one(line):
        c = con_factory()
        try:
            result = check_line(c, line, host_map)
            settled, note = judge(c, line, result["commits"], backend) if result["commits"] else ([], "")
        except Exception as exc:  # one broken repository must not stop the others, but it is counted
            result, settled, note = {"files": 0, "missing": 0, "commits": [], "failed": True}, [], ""
            result["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return line, result, settled, note

    # Collect everything first, write afterwards: the workers cache judgements in the same database, and a write
    # transaction held here while they run would make them wait for the lock until they give up.
    with cf.ThreadPoolExecutor(workers or min(cfg.workers, 6)) as pool:
        results = list(pool.map(one, lines))
    counts = {"checked": 0, "with_commits": 0, "likely_done": 0, "failed": 0}
    for line, result, settled, note in results:
        con.execute("""INSERT OR REPLACE INTO checks(anchor, checked, host, repo, files, missing, commits,
                       settled, note, error) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (line["anchor"], now, result.get("host"), result.get("repo"), result["files"],
                     result["missing"], json.dumps(result["commits"]), json.dumps(settled), note,
                     result.get("error")))
        counts["checked"] += 1
        counts["with_commits"] += bool(result["commits"])
        counts["failed"] += bool(result.get("failed"))
        loose = json.loads(line["loose_ends"] or "[]")
        counts["likely_done"] += bool(loose) and len(settled) == len(loose)
    con.execute("DELETE FROM checks WHERE anchor NOT IN (SELECT anchor FROM lines)")
    con.commit()
    log(f"  {counts['with_commits']} with commits after they went quiet, {counts['likely_done']} look done"
        + (f", {counts['failed']} failed (see checks.error)" if counts["failed"] else ""))
    return counts


def summarize(check, loose_ends):
    """What the web app, the API and the plugin show about one idea's check."""
    if not check:
        return None
    commits = json.loads(check["commits"] or "[]")
    settled = json.loads(check["settled"] or "[]")
    return {
        "checked": check["checked"], "repo": check["repo"], "host": check["host"], "files": check["files"],
        "missing_files": check["missing"], "commits_since": len(commits), "latest_commit": commits[0] if commits else None,
        "commits_capped": len(commits) >= MAX_COMMITS,
        "settled": [loose_ends[i - 1] for i in settled if 1 <= i <= len(loose_ends)],
        "likely_done": bool(loose_ends) and len(settled) == len(loose_ends),
        "note": check["note"] or "", "error": check["error"],
    }
