"""Déjà vu: when a new session starts on something an earlier session already worked on, tell Claude.

The UserPromptSubmit hook sends the first few substantive prompts of a session to the service, which compares each
with every idea's threads (embeddings of their title and summary) and returns the closest earlier ideas.

Similarity alone cannot tell "the same idea again" from "another idea in the same field": measured on 300 real
returns and 300 new ideas, a threshold of 0.7 caught 27% of returns and flagged 9% of new ideas, and a small local
model judging the best candidate still raised false alarms on 20%. A paraphrase of an earlier idea often scores
only 0.5. So the threshold is low and nothing reaches the user from similarity alone: the candidates go to Claude
as context, with the instruction to mention one only when it is clearly what the user is returning to. Claude reads
the whole conversation and is the better judge. Each idea is offered at most once per session; ideas the analyzer
classified as personal are never offered.
"""
import collections
import json
import threading

import numpy as np

from . import config, embed
from .embed import BUILTIN_COMMAND, is_continuation

MIN_CHARS = 25

_index = {"key": None, "mat": None, "rows": None}
_index_lock = threading.Lock()
_seen = collections.OrderedDict()           # session id -> {"checked": n, "offered": set of anchors}


def _key(con):
    return tuple(con.execute("SELECT COUNT(*), MAX(id), TOTAL(LENGTH(title)) FROM lines").fetchone()) + tuple(
        con.execute("SELECT COUNT(*), MAX(updated) FROM marks").fetchone())


def index(con):
    """Thread vectors with their idea, rebuilt when lines or marks change."""
    key = _key(con)
    with _index_lock:
        if _index["key"] == key:
            return _index["mat"], _index["rows"]
        rows = [dict(r) for r in con.execute(
            """SELECT t.session_id, t.embedding, l.id AS line_id, l.anchor, COALESCE(m.title, l.title) AS title,
                      l.status, l.project, l.last_ts, l.loose_ends, l.score, m.state AS mark
               FROM threads t JOIN lines l ON l.id=t.line_id LEFT JOIN marks m ON m.anchor=l.anchor
               WHERE t.embedding IS NOT NULL AND COALESCE(t.kind, '') != 'personal'""")]
        if rows:
            mat = np.vstack([np.frombuffer(r.pop("embedding"), dtype=np.float32) for r in rows])
            mat /= np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9
        else:
            mat = np.zeros((0, 1), dtype=np.float32)
        _index.update(key=key, mat=mat, rows=rows)
        return mat, rows


def worth_checking(prompt):
    p = (prompt or "").strip()
    return len(p) >= MIN_CHARS and not p.startswith("/") and not BUILTIN_COMMAND.match(p) and not is_continuation(p)


def related(con, prompt, session_id=None, top=None, threshold=None):
    """Earlier ideas closest to prompt, best first, leaving out ideas the session itself belongs to."""
    cfg = config.get()
    top = top or cfg.dejavu_top
    threshold = cfg.dejavu_threshold if threshold is None else threshold
    mat, rows = index(con)
    if not rows:
        return []
    v = embed.embed_texts([prompt[:2000]])[0]
    v = v / (np.linalg.norm(v) + 1e-9)
    if mat.shape[1] != v.shape[0]:
        return []
    sims = mat @ v
    own = {r["line_id"] for r in rows if r["session_id"] == session_id}
    best = {}
    for i in np.argsort(-sims)[:200]:
        r = rows[i]
        if sims[i] < threshold:
            break
        if r["line_id"] in own or r["line_id"] in best:
            continue
        best[r["line_id"]] = {"anchor": r["anchor"], "title": r["title"], "status": r["status"],
                              "mark": r["mark"], "project": r["project"], "last": (r["last_ts"] or "")[:10],
                              "similarity": round(float(sims[i]), 2),
                              "loose_ends": json.loads(r["loose_ends"] or "[]")[:3]}
        if len(best) == top:
            break
    return list(best.values())


def check(con, prompt, session_id):
    """What the hook should tell Claude about this prompt: None, or {"ideas": [...], "context": text}."""
    cfg = config.get()
    if cfg.dejavu_prompts <= 0 or not worth_checking(prompt):
        return None
    state = _seen.setdefault(session_id or "", {"checked": 0, "offered": set()})
    _seen.move_to_end(session_id or "")
    while len(_seen) > 500:
        _seen.popitem(last=False)
    if state["checked"] >= cfg.dejavu_prompts:
        return None
    state["checked"] += 1
    ideas = [i for i in related(con, prompt, session_id) if i["anchor"] not in state["offered"]]
    if not ideas:
        return None
    state["offered"] |= {i["anchor"] for i in ideas}
    return {"ideas": ideas, "context": context(ideas)}


def context(ideas):
    out = ["kifu: earlier sessions had ideas that may be related to this message. They were found by similarity "
           "only, so most are probably NOT the same thing. If one is clearly what the user is returning to, say so "
           "in one sentence (when it was, where it stopped, what was left open) and offer to pick it up with "
           "kifu_brief. If none clearly is, do not mention this at all."]
    for i in ideas:
        state = f"marked {i['mark']}" if i["mark"] else i["status"]
        out.append(f"- {i['title']} [{state}, {i['project']}, last {i['last']}, anchor {i['anchor']}]"
                   + "".join(f"\n  - open: {x}" for x in i["loose_ends"]))
    return "\n".join(out)
