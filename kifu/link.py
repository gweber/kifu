"""Stage 4: follow the same idea across sessions.

Threads are embedded (title, summary, keywords) and grouped by average-linkage
clustering. Hard links from forks and resumes pull threads of related sessions
closer. Lines that span several sessions are consolidated by the LLM, which also
decides which loose ends from earlier sessions were settled later.
"""
import concurrent.futures as cf
import hashlib
import json

import numpy as np

from . import analyze, config, embed, marks
from .extract import area_of

DISTANCE = 0.30           # cosine distance for "same idea"; the LLM splits groups that are only related
FORK_BONUS = 0.08         # threads of forked/resumed sessions count as closer
OPEN = ("proposed", "started", "parked")
KIND_WEIGHT = {"project": 1.0, "feature": 1.0, "idea": 1.2, "research": 0.9, "fix": 0.6, "ops": 0.5,
               "question": 0.3, "chore": 0.1}

LINE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "status": {"type": "string", "enum": analyze.STATUSES},
        "loose_ends": {"type": "array", "items": {"type": "string"}},
        "next_step": {"type": "string"},
        "verdict": {"type": "string"},
        "same_idea": {"type": "boolean"},
    },
    "required": ["same_idea", "title", "summary", "status", "loose_ends", "next_step", "verdict"],
}

LINE_SYSTEM = """You get one idea as it appeared across several work sessions between {user} and an AI coding
assistant, in time order. Each entry is what an earlier reader found in one session: status, summary and the
loose ends open at the end of that session.

First decide same_idea: true if the entries are one idea and its continuation, false if they are only
related topics (same project or theme, different goals). If false, fill the other fields briefly; they are discarded.

Otherwise consolidate them into the current state of the idea:
- status: the state after the latest session (shipped, started, proposed, parked, dropped, answered).
- loose_ends: only what is still open after the latest session. Drop loose ends a later session settled.
  Keep the ones nobody came back to, and say where they came from when it helps ("from Jul 7: ...").
- summary: 2-3 sentences, the story of the idea: where it started, where it moved, where it stopped.
- verdict: one sentence for {user} on whether this is worth picking up again and why.
- next_step: the concrete first move when picking it up again.
English. JSON only."""


def thread_text(t):
    kw = ", ".join(json.loads(t["keywords"] or "[]"))
    return f"{t['title']}. {t['summary']} Keywords: {kw}"


def embed_threads(con):
    rows = con.execute("SELECT * FROM threads WHERE embedding IS NULL").fetchall()
    for i in range(0, len(rows), 128):
        chunk = rows[i:i + 128]
        vecs = embed.embed_texts([thread_text(t) for t in chunk])
        con.executemany("UPDATE threads SET embedding=? WHERE id=?",
                        [(v.tobytes(), t["id"]) for t, v in zip(chunk, vecs)])
    con.commit()
    return len(rows)


def thread_key(t):
    """session:turn:title hash, the same shape as an anchor: stable until the session is analyzed again."""
    return f"{t['session_id']}:{t['first_turn']}:{hashlib.sha1(t['title'].encode()).hexdigest()[:6]}"


def apply_corrections(con, rows, labels):
    """The user's merges and detaches, in the order they were made, on top of the clustering.

    Returns the corrected labels and the labels of groups a merge put together (their consolidation has no veto).
    """
    labels = [int(x) for x in labels]
    index = {}
    for i, r in enumerate(rows):
        index.setdefault(thread_key(r), []).append(i)
    forced = set()
    fresh = max(labels, default=0) + 1
    for c in con.execute("SELECT kind, keys FROM corrections ORDER BY id"):
        members = [i for k in json.loads(c["keys"]) for i in index.get(k, [])]
        if c["kind"] == "detach":
            for i in members:
                labels[i], fresh = fresh, fresh + 1
                forced.discard(labels[i])
        elif c["kind"] == "merge" and len(members) > 1:
            target = labels[members[0]]
            for old in {labels[i] for i in members}:
                labels = [target if x == old else x for x in labels]
            forced.add(target)
    return labels, forced


def group(con):
    from sklearn.cluster import AgglomerativeClustering

    rows = con.execute("SELECT id, session_id, first_turn, title, embedding FROM threads "
                       "WHERE embedding IS NOT NULL ORDER BY id").fetchall()
    if len(rows) < 2:
        return {r["id"]: 0 for r in rows}, set()
    mat = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
    mat /= np.linalg.norm(mat, axis=1, keepdims=True)
    dist = np.clip(1.0 - mat @ mat.T, 0.0, 2.0)
    sessions = [r["session_id"] for r in rows]
    related = {}
    for l in con.execute("SELECT src, dst FROM links WHERE kind='fork'"):
        related.setdefault(l["src"], set()).add(l["dst"])
        related.setdefault(l["dst"], set()).add(l["src"])
    index = {}
    for i, s in enumerate(sessions):
        index.setdefault(s, []).append(i)
    for s, others in related.items():
        for o in others:
            for i in index.get(s, []):
                for j in index.get(o, []):
                    dist[i, j] = max(0.0, dist[i, j] - FORK_BONUS)
    labels = AgglomerativeClustering(n_clusters=None, metric="precomputed", linkage="average",
                                     distance_threshold=DISTANCE).fit_predict(dist)
    labels, forced = apply_corrections(con, rows, labels)
    return {r["id"]: lab for r, lab in zip(rows, labels)}, forced


def score_line(status, kinds, n_loose, n_sessions, last_ts, now_ts):
    """Aji: how much potential is left on the board. Open, substantial, spread out, quietly forgotten."""
    if status not in OPEN:
        return 0.0
    days_quiet = (np.datetime64(now_ts[:19]) - np.datetime64(last_ts[:19])) / np.timedelta64(1, "D")
    quiet = min(days_quiet, 45) / 45                      # longer silence, more buried
    weight = max(KIND_WEIGHT.get(k, 0.5) for k in kinds)
    return round(weight * (1 + min(n_loose, 6) * 0.5) * (1 + 0.6 * min(n_sessions - 1, 4)) * (0.5 + quiet)
                 * (0.7 if status == "parked" else 1.0), 2)


def group_key(thread_ids):
    """Threads are re-inserted with new ids whenever their session is analyzed again, so ids identify content."""
    return hashlib.sha1(json.dumps(sorted(thread_ids)).encode()).hexdigest()[:16]


def build_lines(con, backend=None, workers=None, log=print):
    cfg = config.get()
    backend = "openai" if backend == "local" else (backend or cfg.backend)
    workers = workers or cfg.workers
    log(f"embedded {embed_threads(con)} new threads")
    labels, forced = group(con)
    threads = {t["id"]: t for t in con.execute(
        "SELECT t.*, s.project, s.cwd FROM threads t JOIN sessions s ON s.id=t.session_id")}
    groups = {}
    for tid, lab in labels.items():
        groups.setdefault(lab, []).append(threads[tid])
    old = {r["input_hash"]: r for r in con.execute("SELECT * FROM lines")}
    now_ts = con.execute("SELECT MAX(ended) FROM sessions").fetchone()[0]
    jobs, rows = [], []
    for label, members in groups.items():
        members.sort(key=lambda t: t["first_ts"])
        row = {"thread_ids": [t["id"] for t in members], "forced": label in forced,
               "areas": sorted({area_of(t["project"]) for t in members}),
               "project": members[-1]["project"],
               "first_ts": members[0]["first_ts"], "last_ts": max(t["last_ts"] for t in members),
               "n_sessions": len({t["session_id"] for t in members}),
               "kinds": [t["kind"] for t in members]}
        latest = members[-1]
        if row["n_sessions"] == 1 and len(members) == 1:
            row.update(title=latest["title"], summary=latest["summary"], status=latest["status"],
                       loose_ends=json.loads(latest["loose_ends"] or "[]"), next_step=latest["next_step"], verdict="")
        else:
            h = group_key([t["id"] for t in members])
            row["input_hash"] = h
            if h in old and old[h]["title"]:
                o = old[h]
                row.update(title=o["title"], summary=o["summary"], status=o["status"],
                           loose_ends=json.loads(o["loose_ends"] or "[]"), next_step=o["next_step"], verdict=o["verdict"])
            elif h + ":split" in old and not row["forced"]:
                row["split"] = True
            else:
                jobs.append((row, members))
        rows.append(row)
    log(f"{len(threads)} threads -> {len(rows)} lines, {sum(1 for r in rows if r['n_sessions'] > 1)} across sessions, "
        f"{len(jobs)} to consolidate with {backend}")

    system = analyze.system_prompt(LINE_SYSTEM)

    def consolidate(job):
        row, members = job
        parts = []
        for t in members:
            loose = "\n".join(f"    - {x}" for x in json.loads(t["loose_ends"] or "[]"))
            parts.append(f"[{t['first_ts'][:10]} .. {t['last_ts'][:10]} | session {t['session_id'][:8]} | {t['project']}]"
                         f"\n  {t['title']} ({t['kind']}, {t['status']})\n  {t['summary']}\n  In their words: “{t['quote']}”"
                         + (f"\n  loose ends:\n{loose}" if loose else ""))
        out = analyze.BACKENDS[backend](system, "\n\n".join(parts), schema=LINE_SCHEMA)
        if not out.get("same_idea", True) and not row["forced"]:     # the user said these belong together
            row["split"] = True
            return
        row.update(title=out["title"], summary=out["summary"], status=out["status"], loose_ends=out["loose_ends"],
                   next_step=out["next_step"], verdict=out["verdict"])

    with cf.ThreadPoolExecutor(workers) as pool:
        for n, f in enumerate(cf.as_completed([pool.submit(consolidate, j) for j in jobs]), 1):
            try:
                f.result()
            except Exception as e:
                log(f"  consolidation failed: {str(e)[:200]}")
            if n % 10 == 0 or n == len(jobs):
                log(f"  {n}/{len(jobs)}")

    for row in [r for r in rows if r.pop("split", False)]:
        rows.remove(row)
        for tid in row["thread_ids"]:
            t = threads[tid]
            rows.append({"thread_ids": [tid], "areas": [area_of(t["project"])], "project": t["project"],
                         "first_ts": t["first_ts"], "last_ts": t["last_ts"], "n_sessions": 1, "kinds": [t["kind"]],
                         "title": t["title"], "summary": t["summary"], "status": t["status"],
                         "loose_ends": json.loads(t["loose_ends"] or "[]"), "next_step": t["next_step"], "verdict": "",
                         "input_hash": row.get("input_hash") + ":split" if row.get("input_hash") else None})

    con.execute("DELETE FROM lines")
    con.execute("UPDATE threads SET line_id=NULL")
    for row in rows:
        if "title" not in row:        # consolidation failed: fall back to the latest thread
            t = threads[row["thread_ids"][-1]]
            row.update(title=t["title"], summary=t["summary"], status=t["status"],
                       loose_ends=json.loads(t["loose_ends"] or "[]"), next_step=t["next_step"], verdict="")
            row.pop("input_hash", None)
        score = score_line(row["status"], row["kinds"], len(row["loose_ends"]), row["n_sessions"], row["last_ts"], now_ts)
        first = min((threads[tid] for tid in row["thread_ids"]), key=lambda t: t["first_ts"])
        # session:turn alone collides when one move holds two ideas; the title hash tells them apart.
        anchor = f"{first['session_id']}:{first['first_turn']}:{hashlib.sha1(first['title'].encode()).hexdigest()[:6]}"
        cur = con.execute("""INSERT INTO lines(title, summary, project, status, first_ts, last_ts, n_sessions, score,
                             verdict, loose_ends, next_step, areas, input_hash, thread_ids, anchor)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (row["title"], row["summary"], row["project"], row["status"], row["first_ts"], row["last_ts"],
                           row["n_sessions"], score, row["verdict"], json.dumps(row["loose_ends"]), row["next_step"],
                           json.dumps(row["areas"]), row.get("input_hash"), json.dumps(row["thread_ids"]), anchor))
        con.executemany("UPDATE threads SET line_id=? WHERE id=?", [(cur.lastrowid, tid) for tid in row["thread_ids"]])
    con.commit()
    marks.reattach(con, log=log)
