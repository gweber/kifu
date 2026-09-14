"""`kifu rules`: what you keep telling the assistant, as rules it could read instead.

A correction that comes back session after session ("keine shims", "English file names", "stop pausing") belongs in
CLAUDE.md or memory, where every session reads it. This finds them:

  1. prompts that read like corrections or standing preferences (cue words in English and German)
  2. one model call groups the ones that say the same thing, words each group as a rule, and checks it against the
     rules that already exist (the global CLAUDE.md, project CLAUDE.md files, memory indexes)
  3. a group counts only with at least MIN_TIMES prompts from MIN_SESSIONS sessions: checked here from the prompt
     ids the model returns, not taken from its word

The answer is cached until the candidates or the existing rules change. Nothing is written: each suggestion names
the file it would go in.
"""
import datetime as dt
import glob
import hashlib
import json
import os
import re

from . import analyze, config
from .claude_code import claude_dir

MIN_TIMES = 3
MIN_SESSIONS = 2
MAX_CANDIDATES = 400
RULES_CHARS = 12000

STARTS = ["no", "nope", "nein", "don't", "dont", "do not", "never", "always", "stop", "please don't", "please do not",
          "please never", "please always", "please use", "i said", "i told you", "i asked", "again", "wrong",
          "not like that", "why did you", "why do you", "why are you", "nicht", "kein", "keine", "immer", "nie",
          "bitte nicht", "bitte kein", "bitte keine", "bitte immer", "bitte nie", "hab ich doch", "hab ich dir",
          "schon wieder", "warum hast du", "warum machst du", "ich hatte gesagt", "ich hatte doch gesagt", "lass das",
          "hör auf", "stopp", "use", "nutze", "benutze", "only", "nur"]
ANYWHERE = ["you always", "every time", "each time", "from now on", "in future", "in the future", "i told you",
            "how often", "remember to", "remember that", "keep in mind", "ab jetzt", "in zukunft", "merk dir", "wie oft",
            "jedes mal", "schon wieder", "hatte ich gesagt", "hatte ich doch gesagt", "not again", "i said", "again and again",
            "immer wieder", "nicht mehr", "no more"]
CUE = re.compile(r"^\W*(?:" + "|".join(re.escape(w) for w in STARTS) + r")\b|\b(?:"
                 + "|".join(re.escape(w) for w in ANYWHERE) + r")\b", re.I)

SCHEMA = {"type": "object", "required": ["rules"], "properties": {"rules": {"type": "array", "items": {
    "type": "object", "required": ["rule", "prompts", "scope", "covered_by", "why"],
    "properties": {
        "rule": {"type": "string"},
        "prompts": {"type": "array", "items": {"type": "integer"}},
        "scope": {"type": "string"},
        "covered_by": {"type": ["string", "null"]},
        "why": {"type": "string"}}}}}}

SYSTEM = """You read short messages {user} typed to coding assistants and find the corrections and standing preferences {user} keeps repeating.

Each message has a number. Group messages that ask for the same thing, even when worded differently or in another language ("keine shims" and "no compatibility shims" are the same). A group is a rule only if it would still apply in a future session: a lasting preference about how to work, write, name, build or communicate. Leave out one-off decisions about a single task, bug reports, chat, and moods.

For each rule:
- rule: one imperative sentence in English, the way it would be written in CLAUDE.md ("Never add compatibility shims or legacy bridges; remove them.")
- prompts: the numbers of every message in the group
- scope: "global", or "project:<name>" when all its messages come from one project, or "tool:<name>" when they come from one tool other than claude
- covered_by: the path of the existing rules file that already says this (from the list below), or null when no file does. Covered means the file states the same rule, not a related one.
- why: one short sentence on what keeps happening, for {user} to decide whether to add it

Only groups with at least {min_times} messages. Existing rules:

{existing}

JSON only."""


def candidates(con, limit=MAX_CANDIDATES):
    rows = con.execute("""SELECT t.session_id, t.idx, t.ts, t.prompt, s.project, s.tool FROM turns t
                          JOIN sessions s ON s.id=t.session_id
                          WHERE s.automated=0 AND t.dup_of IS NULL AND LENGTH(t.prompt) BETWEEN 8 AND 800
                          ORDER BY t.ts DESC""").fetchall()
    out, seen = [], set()
    for r in rows:
        text = " ".join(r["prompt"].split())
        if text.lower() in seen or not CUE.search(text[:300]):
            continue
        seen.add(text.lower())
        out.append({"session": r["session_id"], "turn": r["idx"], "ts": r["ts"], "project": r["project"],
                    "tool": r["tool"] or "claude", "text": text[:300]})
        if len(out) == limit:
            break
    return out


def rule_files(root=None):
    """The rules sessions already read: global CLAUDE.md, project CLAUDE.md files, memory indexes."""
    root = root or claude_dir()
    files = [os.path.join(root, "CLAUDE.md")]
    for base in config.get().project_roots:
        base = os.path.expanduser(base)
        files += sorted(glob.glob(os.path.join(base, "*", "CLAUDE.md")))
    files += sorted(glob.glob(os.path.join(root, "projects", "*", "memory", "MEMORY.md")))
    return [f for f in files if os.path.isfile(f)]


def existing_rules(files):
    parts, budget = [], RULES_CHARS
    for f in files:
        text = open(f, errors="replace").read().strip()
        if not text:
            continue
        chunk = f"## {f}\n{text[:max(0, min(len(text), budget // 2 if len(files) > 1 else budget))]}"
        parts.append(chunk)
        budget -= len(chunk)
        if budget <= 0:
            break
    return "\n\n".join(parts) or "(none)"


SCHEMA_SQL = "CREATE TABLE IF NOT EXISTS rule_runs(input_hash TEXT PRIMARY KEY, created TEXT, result TEXT)"


def suggest(con, backend=None, refresh=False, root=None, log=print):
    cfg = config.get()
    backend = backend or cfg.backend
    con.execute(SCHEMA_SQL)
    cands = candidates(con)
    if not cands:
        return {"rules": [], "candidates": 0, "cached": False}
    files = rule_files(root)
    existing = existing_rules(files)
    listing = "\n".join(f"{n}. [{c['ts'][:10]} · {c['tool']} · {c['project']}] {c['text']}" for n, c in enumerate(cands, 1))
    system = SYSTEM.format(user=cfg.user, min_times=MIN_TIMES, existing=existing)
    key = hashlib.sha1(f"{system}\n{listing}\n{backend}".encode()).hexdigest()[:16]
    row = con.execute("SELECT result, created FROM rule_runs WHERE input_hash=?", (key,)).fetchone()
    if row and not refresh:
        result = json.loads(row["result"])
        cached = True
    else:
        log(f"asking {backend} about {len(cands)} correction-like prompts against {len(files)} rules files")
        result = analyze.BACKENDS[backend](system, listing, schema=SCHEMA)
        con.execute("INSERT OR REPLACE INTO rule_runs VALUES (?,?,?)",
                    (key, dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), json.dumps(result)))
        con.commit()
        cached = False
    return {"rules": _checked(result.get("rules", []), cands, files), "candidates": len(cands), "cached": cached,
            "files": files}


def _checked(rules, cands, files):
    """Keep groups the prompts really support, with the evidence attached; uncovered rules first, most sessions first."""
    out = []
    known = set(files)
    for r in rules:
        members = [cands[n - 1] for n in dict.fromkeys(r.get("prompts") or []) if isinstance(n, int) and 1 <= n <= len(cands)]
        sessions = {m["session"] for m in members}
        if len(members) < MIN_TIMES or len(sessions) < MIN_SESSIONS:
            continue
        covered = r.get("covered_by")
        out.append({"rule": r["rule"], "scope": r.get("scope") or "global", "why": r.get("why") or "",
                    "covered_by": covered if covered in known else None,
                    "times": len(members), "sessions": len(sessions),
                    "first": min(m["ts"] for m in members)[:10], "last": max(m["ts"] for m in members)[:10],
                    "target": target_file(r.get("scope") or "global", members),
                    "examples": [{k: m[k] for k in ("ts", "project", "tool", "text", "session")}
                                 for m in sorted(members, key=lambda m: m["ts"], reverse=True)[:5]]})
    return sorted(out, key=lambda r: (r["covered_by"] is not None, -r["sessions"], -r["times"]))


def target_file(scope, members):
    """Where the rule would be read: the global CLAUDE.md, or the project's own CLAUDE.md when it is one project's."""
    if scope.startswith("project:"):
        name = scope.split(":", 1)[1].split("/")[0]
        for base in config.get().project_roots:
            folder = os.path.join(os.path.expanduser(base), name)
            if os.path.isdir(folder):
                return os.path.join(folder, "CLAUDE.md")
    if scope.startswith("tool:"):
        return None                         # another tool's own instructions file: kifu does not know where it is
    return os.path.join(claude_dir(), "CLAUDE.md")


def format_text(result):
    if not result["rules"]:
        return f"no repeated corrections found among {result['candidates']} correction-like prompts"
    out = []
    for r in result["rules"]:
        head = "already in " + r["covered_by"] if r["covered_by"] else "missing: add to " + (r["target"] or r["scope"])
        out.append(f"\n{r['rule']}\n  {r['times']} times in {r['sessions']} sessions, {r['first']} .. {r['last']} · {head}")
        out.append(f"  {r['why']}")
        for e in r["examples"][:3]:
            out.append(f"  - {e['ts'][:10]} {e['project']}: “{e['text'][:140]}”")
    missing = sum(1 for r in result["rules"] if not r["covered_by"])
    out.append(f"\n{missing} rule(s) not written down yet, {len(result['rules']) - missing} already covered"
               + (" (cached answer; --refresh asks again)" if result["cached"] else ""))
    return "\n".join(out)
