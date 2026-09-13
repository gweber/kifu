"""The user's verdicts on ideas, and keeping them attached when ideas are rebuilt.

A mark is stored under the idea's anchor (session, turn and title hash of its first thread). Re-analysing that session can
move the anchor, so a mark also remembers the sessions the idea spanned and the mean embedding of its
threads. After `link` rebuilds lines, a mark whose anchor is gone moves to the line that shares a session and
means the same thing — or stays unattached, which is better than landing on the wrong idea.
"""
import datetime as dt
import json

import numpy as np

REATTACH_SIMILARITY = 0.80


def _line_fingerprint(con, anchor):
    line = con.execute("SELECT id FROM lines WHERE anchor=?", (anchor,)).fetchone()
    if not line:
        return None, None
    rows = con.execute("SELECT session_id, embedding FROM threads WHERE line_id=?", (line["id"],)).fetchall()
    sessions = sorted({r["session_id"] for r in rows})
    vecs = [np.frombuffer(r["embedding"], dtype=np.float32) for r in rows if r["embedding"]]
    if not vecs:
        return sessions, None
    mean = np.mean([v / np.linalg.norm(v) for v in vecs], axis=0)
    return sessions, (mean / np.linalg.norm(mean)).astype(np.float32)


def _upsert(con, anchor, **fields):
    sessions, vec = _line_fingerprint(con, anchor)
    row = con.execute("SELECT state, note, title FROM marks WHERE anchor=?", (anchor,)).fetchone()
    merged = {"state": row["state"], "note": row["note"], "title": row["title"]} if row else {"state": None, "note": "", "title": None}
    merged.update(fields)
    if not merged["state"] and not merged["title"]:
        con.execute("DELETE FROM marks WHERE anchor=?", (anchor,))
    else:
        con.execute("""INSERT OR REPLACE INTO marks(anchor, state, note, updated, sessions, vec, title)
                       VALUES (?,?,?,?,?,?,?)""",
                    (anchor, merged["state"], merged["note"] or "", dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                     json.dumps(sessions or []), vec.tobytes() if vec is not None else None, merged["title"]))
    con.commit()


def set_mark(con, anchor, state, note=""):
    """done or dismissed; open clears the state and keeps a rename."""
    _upsert(con, anchor, state=None if state == "open" else state, note=note)


def set_title(con, anchor, title):
    """The user's own name for an idea; empty restores the analyzed title."""
    _upsert(con, anchor, title=(title or "").strip() or None)


def reattach(con, log=print):
    """Move marks whose anchor disappeared in a rebuild; refresh the fingerprint of those still attached."""
    anchors = {r["anchor"] for r in con.execute("SELECT anchor FROM lines")}
    marked = {r["anchor"] for r in con.execute("SELECT anchor FROM marks")}      # any row, renames included
    moved = orphaned = 0
    for m in con.execute("SELECT * FROM marks").fetchall():
        if m["anchor"] in anchors:
            sessions, vec = _line_fingerprint(con, m["anchor"])
            con.execute("UPDATE marks SET sessions=?, vec=? WHERE anchor=?",
                        (json.dumps(sessions), vec.tobytes() if vec is not None else m["vec"], m["anchor"]))
            continue
        if not m["vec"]:
            orphaned += 1
            continue
        mark_vec = np.frombuffer(m["vec"], dtype=np.float32)
        best, best_sim = None, REATTACH_SIMILARITY
        for sid in json.loads(m["sessions"] or "[]"):
            for line in con.execute("SELECT DISTINCT l.anchor FROM lines l JOIN threads t ON t.line_id=l.id "
                                    "WHERE t.session_id=?", (sid,)):
                if line["anchor"] in marked:
                    continue
                _, vec = _line_fingerprint(con, line["anchor"])
                if vec is not None and float(vec @ mark_vec) >= best_sim:
                    best, best_sim = line["anchor"], float(vec @ mark_vec)
        if best:
            con.execute("UPDATE marks SET anchor=? WHERE anchor=?", (best, m["anchor"]))
            marked.add(best)
            moved += 1
        else:
            orphaned += 1
    con.commit()
    if moved or orphaned:
        log(f"marks: {moved} moved to their rebuilt idea, {orphaned} without a match")
    return moved, orphaned
