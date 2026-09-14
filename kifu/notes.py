"""Decisions and promises: why things are the way they are, and what the assistant said it would do later.

Loose ends are what the analyzer saw left open. Two things slip through that net:

  decision  a choice with its reason ("SQLite instead of Postgres, because it runs on the Pi"). Months later the
            reason is the part nobody remembers, and the part a new session needs before undoing the choice.
  promise   the assistant deferring something itself ("I'll add the cache later", "out of scope for now"). These
            are easy to lose: the user never typed them, so no prompt carries them.

Replies are searched for the wording decisions and deferrals use; only those turns are read, as excerpts around the
wording, many sessions per model call. Results are cached per session and only new or changed sessions cost a call.

A promise is open when it was made in the last session of its idea and the idea is not shipped or done: when later
sessions continued the idea, the promise was most likely dealt with there.
"""
import concurrent.futures as cf
import hashlib
import json
import re

from . import analyze, config

DECISION = re.compile(r"\b(decided|decision|chose|chosen|went with|going with|opted|instead of|rather than|trade-?offs?|"
                      r"settled on|entschieden|anstatt|stattdessen|wir nehmen|the reason|because)\b", re.I)
PROMISE = re.compile(r"\b(out of scope|follow-?up|for later|later(?=[,.;: ])|deferred|for now|not yet|skipped|TODO|"
                     r"in a (?:later|future|separate|follow-up)|next session|später|vorerst|erst ?mal nicht|noch nicht|"
                     r"im nächsten schritt)\b", re.I)

WINDOW = 320
EXCERPT_CHARS = 900
BATCH_CHARS = 60000

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS notes(
  id INTEGER PRIMARY KEY, session_id TEXT, turn_idx INT, ts TEXT, kind TEXT, text TEXT, because TEXT, quote TEXT);
CREATE INDEX IF NOT EXISTS notes_session ON notes(session_id, turn_idx);
CREATE TABLE IF NOT EXISTS noted(session_id TEXT PRIMARY KEY, digest_hash TEXT, n INT);
"""

SCHEMA = {"type": "object", "required": ["notes"], "properties": {"notes": {"type": "array", "items": {
    "type": "object", "required": ["excerpt", "kind", "text", "because", "quote"],
    "properties": {
        "excerpt": {"type": "integer"},
        "kind": {"type": "string", "enum": ["decision", "promise"]},
        "text": {"type": "string"},
        "because": {"type": "string"},
        "quote": {"type": "string"}}}}}}

SYSTEM = """You read excerpts of conversations between {user} and a coding assistant. Each excerpt is one turn: what {user} asked, and parts of the assistant's reply.

Find two things, and nothing else:

decision: a real choice between alternatives that shapes the work beyond this turn, with the reason if one is given. "Use SQLite instead of Postgres because it runs on the Pi" is one. Routine steps, "I fixed the typo", or restating the task are not.
  text: the decision in one short sentence, English ("SQLite instead of Postgres for the event store")
  because: the reason as given, English, written to follow the word "because" ("it runs on the Pi", not "because it runs on the Pi" or "to save memory"); empty when none is given

promise: the assistant deferring or leaving out something itself, meaning to come back to it: "I'll add caching later", "error handling is out of scope for now", "skipped the migration for now". Not the user's own plans, not a question offered as an option, not something done in the same reply.
  text: what was deferred, one short sentence, English, as a task ("Add caching to the tide endpoint")
  because: why it was deferred, if said, written to follow the word "because"; else empty

For both: excerpt is the excerpt number; quote is the exact words from the excerpt (at most 200 characters), in their original language.
Most excerpts contain neither: return only what clearly qualifies. JSON only."""


def excerpt(reply):
    """The parts of a reply around decision and promise wording, merged, at most EXCERPT_CHARS."""
    spans = sorted([max(0, m.start() - WINDOW), min(len(reply), m.end() + WINDOW)]
                   for m in list(DECISION.finditer(reply)) + list(PROMISE.finditer(reply)))
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    text = " … ".join(reply[a:b].strip() for a, b in merged)
    return text[:EXCERPT_CHARS]


def pending(con, only=None):
    """Sessions whose turns changed since their notes were taken, with their candidate turns."""
    con.executescript(SCHEMA_SQL)
    q = """SELECT s.id, s.digest_hash, s.project, n.digest_hash AS noted FROM sessions s
           LEFT JOIN noted n ON n.session_id=s.id WHERE s.automated=0"""
    out = []
    for s in con.execute(q):
        if only is not None and s["id"] not in only:
            continue
        if s["noted"] == _key(s["digest_hash"]):
            continue
        turns = [dict(t) for t in con.execute("SELECT idx, ts, prompt, reply FROM turns WHERE session_id=? "
                                              "AND dup_of IS NULL AND reply != '' ORDER BY idx", (s["id"],))
                 if DECISION.search(t["reply"]) or PROMISE.search(t["reply"])]
        out.append({"session": s["id"], "digest": s["digest_hash"], "project": s["project"], "turns": turns})
    return out


def _key(digest):
    return hashlib.sha1(f"{digest}|{hashlib.sha1(SYSTEM.encode()).hexdigest()[:8]}".encode()).hexdigest()[:16]


def batches(sessions):
    batch, size = [], 0
    for s in sessions:
        for t in s["turns"]:
            item = {"session": s["session"], "project": s["project"], "idx": t["idx"], "ts": t["ts"],
                    "text": f"{' '.join(t['prompt'].split())[:300]}\nASSISTANT: {excerpt(t['reply'])}"}
            if batch and size + len(item["text"]) > BATCH_CHARS:
                yield batch
                batch, size = [], 0
            batch.append(item)
            size += len(item["text"])
    if batch:
        yield batch


def take(con_factory, backend=None, workers=None, only=None, log=print):
    """Read decisions and promises from new or changed sessions. Returns the number of batches that failed."""
    cfg = config.get()
    backend = backend or cfg.backend
    con = con_factory()
    todo = pending(con, only)
    with_turns = [s for s in todo if s["turns"]]
    parts = list(batches(with_turns))
    log(f"notes: {len(todo)} sessions to read, {sum(len(s['turns']) for s in with_turns)} turns in {len(parts)} calls")
    system = analyze.system_prompt(SYSTEM)
    done_sessions = {s["session"] for s in todo if not s["turns"]}
    failed_sessions = set()

    def run(batch):
        user = "\n\n".join(f"[E{n}] {b['ts'][:10]} · {b['project']}\nUSER: {b['text']}" for n, b in enumerate(batch, 1))
        return batch, analyze.BACKENDS[backend](system, user, schema=SCHEMA)

    results, failed = [], 0
    with cf.ThreadPoolExecutor(workers or cfg.workers) as pool:
        futures = {pool.submit(run, b): b for b in parts}
        for n, f in enumerate(cf.as_completed(futures), 1):
            batch = futures[f]
            try:
                results.append(f.result())
                log(f"  {n}/{len(parts)}")
            except analyze.Refused:
                results.append((batch, {"notes": []}))      # recorded as read, like a refused analysis
                log(f"  {n}/{len(parts)} refused by the model")
            except Exception as exc:  # its sessions stay pending: the next run asks again
                failed += 1
                failed_sessions |= {b["session"] for b in batch}
                log(f"  {n}/{len(parts)} FAILED {str(exc)[:200]}")
    by_session = {}
    for batch, out in results:
        for note in out.get("notes", []):
            n = note.get("excerpt")
            if not isinstance(n, int) or not 1 <= n <= len(batch) or not (note.get("text") or "").strip():
                continue
            b = batch[n - 1]
            by_session.setdefault(b["session"], []).append((b["idx"], b["ts"], note))
    digests = {s["session"]: s["digest"] for s in todo}
    written = 0
    for sid in ({s["session"] for s in with_turns} - failed_sessions) | done_sessions:
        con.execute("DELETE FROM notes WHERE session_id=?", (sid,))
        rows = by_session.get(sid, [])
        con.executemany("INSERT INTO notes(session_id, turn_idx, ts, kind, text, because, quote) VALUES (?,?,?,?,?,?,?)",
                        [(sid, idx, ts, note["kind"], note["text"].strip(), (note.get("because") or "").strip(),
                          (note.get("quote") or "")[:300]) for idx, ts, note in rows])
        con.execute("INSERT OR REPLACE INTO noted VALUES (?,?,?)", (sid, _key(digests[sid]), len(rows)))
        written += len(rows)
    con.commit()
    log(f"notes: {written} decisions and promises")
    return failed


def for_lines(con, lines):
    """Attach each line's decisions and promises (by the turns its threads cover), and whether a promise is open."""
    con.executescript(SCHEMA_SQL)
    notes = {}
    for n in con.execute("SELECT * FROM notes ORDER BY ts"):
        notes.setdefault(n["session_id"], []).append(dict(n))
    for line in lines:
        found, seen = [], set()
        last_session = line["threads"][-1]["session"] if line["threads"] else None
        finished = line["status"] == "shipped" or (line["mark"] or {}).get("state") in ("done", "dismissed")
        for t in line["threads"]:
            for n in notes.get(t["session"], []):
                if n["id"] in seen or not (t["first_turn"] <= n["turn_idx"] <= t["last_turn"]):
                    continue
                seen.add(n["id"])
                item = {k: n[k] for k in ("kind", "text", "because", "quote", "ts")} | {"session": n["session_id"]}
                if n["kind"] == "promise":
                    item["open"] = not finished and n["session_id"] == last_session
                found.append(item)
        line["decisions"] = [n for n in found if n["kind"] == "decision"]
        line["promises"] = [n for n in found if n["kind"] == "promise"]
    return lines


def recent_sessions(con, days=2):
    """Sessions that ended in the last days: what the drain after a session end reads, never a whole backfill."""
    return {r["id"] for r in con.execute("SELECT id FROM sessions WHERE ended >= datetime('now', ?)", (f"-{days} days",))}


def search(con, kind, q=None, open_only=False, limit=50):
    """Decisions or promises with their idea, newest first; q: words that must all appear."""
    from . import report
    data = report.collect(con, habits_data={})
    out = []
    for line in data["lines"]:
        for item in line["decisions" if kind == "decision" else "promises"]:
            if open_only and not item.get("open"):
                continue
            hay = f"{item['text']} {item['because']} {item['quote']} {line['title']} {line['project']}".lower()
            if q and not all(w in hay for w in q.lower().split()):
                continue
            session = next((s for s in data["sessions"] if s["id"] == item["session"]), None)
            out.append({**item, "idea": line["title"], "anchor": line["anchor"], "project": line["project"],
                        "status": line["status"], "resume": session["resume"] if session else None})
    out.sort(key=lambda x: x["ts"] or "", reverse=True)
    return out[:limit]
