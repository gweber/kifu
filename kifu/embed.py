"""Stage 2: group each session's turns into moves and embed them.

A move is one substantive prompt plus the short confirmations that follow it
("go on", "yes do both"), with the end of the assistant's reply. Moves are what
the analyzer reads; their embeddings let `habits` tell a topic switch from a
follow-up when no analyzed thread covers a turn.
"""
import hashlib
import re

import httpx
import numpy as np

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS moves(
  id INTEGER PRIMARY KEY, session_id TEXT, first_idx INT, last_idx INT, ts_first TEXT, ts_last TEXT,
  prompts TEXT, reply_tail TEXT, n_writes INT, n_commits INT, text_hash TEXT);
CREATE INDEX IF NOT EXISTS moves_session ON moves(session_id);
CREATE INDEX IF NOT EXISTS moves_session_idx ON moves(session_id, first_idx);
CREATE TABLE IF NOT EXISTS vectors(text_hash TEXT PRIMARY KEY, vec BLOB);
DROP TABLE IF EXISTS topics;
"""

CONFIRMATIONS = ["ok", "okay", "yes", "yep", "yeah", "sure", "go", "go on", "go ahead", "continue", "proceed",
                 "do it", "next", "sounds good", "looks good", "lgtm", "perfect", "great", "thanks", "thank you"]

# Claude Code's own commands: they steer the tool, not the work.
BUILTIN_COMMAND = re.compile(
    r"^/(model|clear|compact|config|login|logout|resume|fast|effort|cost|status|mcp|ide|permissions|memory|context|"
    r"usage|exit|rename|remote-control|add-dir|agents|hooks|doctor|theme|plugin|statusline|vim|export|rewind|tasks|"
    r"todos|output-style|upgrade|workflows|artifacts|color|release-notes|terminal-setup|privacy-settings|feedback|"
    r"bug)\b")

_confirm_re = None


def _confirm_pattern():
    global _confirm_re
    if _confirm_re is None:
        words = sorted({w.lower() for w in CONFIRMATIONS + config.get().confirmations}, key=len, reverse=True)
        alternatives = "|".join(re.escape(w) for w in words)
        # A lone option pick ("a", "2") also just confirms a choice the assistant offered.
        _confirm_re = re.compile(rf"^\W*(?:{alternatives}|[a-d]|[1-9])(?:\W|$)", re.I)
    return _confirm_re


def is_continuation(prompt):
    """A short follow-up that carries no topic of its own. A short question still counts as a prompt."""
    p = prompt.strip()
    if not p or len(p) > 90 or "\n" in p:
        return False
    if _confirm_pattern().match(p):
        return True
    return len(p) < 25 and "?" not in p


def move_text(prompts, reply_tail):
    return f"{prompts[:2000]}\n---\n{reply_tail[-900:]}"


def build_moves(con):
    con.executescript(SCHEMA)
    con.execute("DELETE FROM moves")
    sessions = [r["id"] for r in con.execute("SELECT id FROM sessions WHERE automated=0 ORDER BY started")]
    evidence = {}
    for e in con.execute("SELECT session_id, turn_idx, kind FROM evidence "
                         "WHERE turn_idx IS NOT NULL AND kind IN ('write','commit')"):
        evidence.setdefault((e["session_id"], e["turn_idx"]), []).append(e["kind"])
    n = 0
    for sid in sessions:
        cur = None
        turns = con.execute("SELECT * FROM turns WHERE session_id=? AND dup_of IS NULL ORDER BY idx", (sid,)).fetchall()
        for t in turns:
            if BUILTIN_COMMAND.match(t["prompt"]):
                continue
            kinds = evidence.get((sid, t["idx"]), [])
            if cur is None or not is_continuation(t["prompt"]):
                if cur:
                    _insert_move(con, cur)
                    n += 1
                cur = {"session_id": sid, "first_idx": t["idx"], "prompts": [], "reply": "", "writes": 0,
                       "commits": 0, "ts_first": t["ts"]}
            cur["prompts"].append(t["prompt"])
            cur["last_idx"] = t["idx"]
            cur["ts_last"] = t["ts"]
            if t["reply"]:
                cur["reply"] = t["reply"]
            cur["writes"] += kinds.count("write")
            cur["commits"] += kinds.count("commit")
        if cur:
            _insert_move(con, cur)
            n += 1
    con.commit()
    return n


def _insert_move(con, m):
    prompts = "\n• ".join(m["prompts"])
    text = move_text(prompts, m["reply"])
    h = hashlib.sha1(text.encode()).hexdigest()[:20]
    con.execute("""INSERT INTO moves(session_id, first_idx, last_idx, ts_first, ts_last, prompts, reply_tail,
                   n_writes, n_commits, text_hash) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (m["session_id"], m["first_idx"], m["last_idx"], m["ts_first"], m["ts_last"], prompts,
                 m["reply"][-900:], m["writes"], m["commits"], h))


# Tests and the demo replace this with a deterministic function.
embedder = None


def embed_texts(texts, batch=32):
    """Vectors from any OpenAI-compatible /v1/embeddings endpoint (vLLM, Ollama, LiteLLM, OpenAI)."""
    cfg = config.get()
    if embedder is None and cfg.demo:
        from . import demo  # the demo store embeds with its hashing function, in whichever process opens it
        demo.install()
    if embedder is not None:
        return np.asarray(embedder(texts), dtype=np.float32)
    out = []
    with httpx.Client(timeout=120) as client:
        for i in range(0, len(texts), batch):
            try:
                r = client.post(cfg.embed_url, json={"model": cfg.embed_model, "input": texts[i:i + batch]})
                r.raise_for_status()
            except httpx.HTTPError as exc:
                raise RuntimeError(f"embeddings endpoint {cfg.embed_url} failed ({exc}); set [embeddings] url "
                                   f"and model in {config.default_path()}") from exc
            out.extend(d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"]))
    return np.asarray(out, dtype=np.float32)


def embed_moves(con, log=print):
    missing = con.execute("""SELECT m.text_hash, m.prompts, m.reply_tail FROM moves m
                             LEFT JOIN vectors v ON v.text_hash=m.text_hash WHERE v.text_hash IS NULL
                             GROUP BY m.text_hash""").fetchall()
    log(f"embedding {len(missing)} new moves")
    for i in range(0, len(missing), 256):
        chunk = missing[i:i + 256]
        vecs = embed_texts([move_text(r["prompts"], r["reply_tail"]) for r in chunk])
        con.executemany("INSERT OR REPLACE INTO vectors VALUES (?,?)",
                        [(r["text_hash"], v.tobytes()) for r, v in zip(chunk, vecs)])
        con.commit()
