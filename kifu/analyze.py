"""Stage 3: read each session's moves with an LLM and list its threads and loose ends.

Backends:
  claude   headless `claude -p` without session persistence, on the user's own Claude login (default)
  openai   any OpenAI-compatible chat endpoint: vLLM, Ollama, LiteLLM, a hosted API
  fixture  a Python function set by tests and `kifu demo`; no model involved
"""
import concurrent.futures as cf
import hashlib
import json
import os
import re
import subprocess
import time

import httpx

from . import config
from .db import prefix_range

CHUNK_CHARS = 110_000     # ~35k tokens of digest per request
PROMPT_VERSION = "3"      # bump when the prompt changes enough to justify re-reading every session

STATUSES = ["shipped", "started", "proposed", "parked", "dropped", "answered"]
KINDS = ["project", "feature", "idea", "research", "question", "fix", "ops", "chore"]

THREAD_SCHEMA = {
    "type": "object",
    "properties": {
        "session_gist": {"type": "string"},
        "threads": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "kind": {"type": "string", "enum": KINDS},
                    "summary": {"type": "string"},
                    "status": {"type": "string", "enum": STATUSES},
                    "first_move": {"type": "integer"},
                    "last_move": {"type": "integer"},
                    "quote": {"type": "string"},
                    "loose_ends": {"type": "array", "items": {"type": "string"}},
                    "next_step": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "kind", "summary", "status", "first_move", "last_move", "quote",
                             "loose_ends", "next_step", "keywords"],
            },
        },
    },
    "required": ["session_gist", "threads"],
}

SYSTEM = """You read the digest of one work session between {user} and an AI coding assistant.
{user_cap} thinks out loud{writing_note}. Ideas get raised, half-started, pushed aside by the next idea,
and forgotten. Your job is to recover them.

The digest is a list of moves. Each move is {user}'s prompt(s) and the end of the assistant's reply,
with counts of files written and commits made during that move. A prompt marked
"[sent while the assistant was working]" was typed mid-task: it often raises a separate idea that the
assistant only half noticed — give it its own thread when it does, and a loose end when nothing came of it.

List the threads of the session: each distinct idea, feature, project, investigation or decision.
- Merge moves that serve the same goal into one thread. Do not make a thread per move.
- Routine chores (commit and push, update the docs, restart a service) are not threads unless they are the point of the session.
- A question that was answered and led nowhere is kind "question", status "answered".
- status: shipped = built and committed or deployed; started = work began but did not finish;
  proposed = raised or agreed but no work happened; parked = explicitly postponed ("later", "not now");
  dropped = explicitly abandoned or replaced.
- Base status on what the digest shows. Writes and commits are evidence of work; a reply saying
  "done" after zero writes is not.
- loose_ends: concrete things that were proposed, promised, offered or agreed and are NOT done by the end
  of the digest. Include assistant offers {user} accepted but that were never carried out, items from a
  list where only some were done, and "later" items. Be specific ("add the retry to the upload client",
  not "more work").
- quote: {user}'s own words that best capture the idea, verbatim, max 200 characters, original language.
- title, summary, loose_ends, next_step: English. Titles name the idea, max 8 words.
- first_move / last_move: move numbers from the digest.
Return JSON only."""


def system_prompt(template=SYSTEM):
    cfg = config.get()
    note = f", {cfg.writing_note}" if cfg.writing_note else ", sometimes in another language, sometimes dictated"
    return template.format(user=cfg.user, user_cap=cfg.user[:1].upper() + cfg.user[1:], writing_note=note)


def digest_moves(con, session_id):
    moves = con.execute("SELECT * FROM moves WHERE session_id=? ORDER BY first_idx", (session_id,)).fetchall()
    blocks = []
    for n, m in enumerate(moves, 1):
        work = []
        if m["n_writes"]:
            work.append(f"{m['n_writes']} writes")
        if m["n_commits"]:
            work.append(f"{m['n_commits']} commits")
        head = f"[M{n} {m['ts_first'][:16].replace('T', ' ')}{' | ' + ', '.join(work) if work else ''}]"
        reply = re.sub(r"\n{3,}", "\n\n", m["reply_tail"] or "").strip()
        blocks.append(f"{head}\nUSER: {m['prompts'][:3000]}\nASSISTANT (end of reply): {reply[-700:]}")
    return moves, blocks


def chunks(blocks):
    cur, size = [], 0
    for b in blocks:
        if cur and size + len(b) > CHUNK_CHARS:
            yield cur
            cur, size = [], 0
        cur.append(b)
        size += len(b)
    if cur:
        yield cur


def call_openai(system, user, schema=THREAD_SCHEMA):
    cfg = config.get()
    headers = {}
    if cfg.openai_api_key_env and os.environ.get(cfg.openai_api_key_env):
        headers["Authorization"] = f"Bearer {os.environ[cfg.openai_api_key_env]}"
    body = {"model": cfg.openai_model, "temperature": 0.2, "max_tokens": 6000,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "result", "schema": schema}},
            # Qwen-style servers: skip hidden reasoning, the schema does the structuring.
            "chat_template_kwargs": {"enable_thinking": False}}
    r = httpx.post(cfg.openai_url, json=body, headers=headers, timeout=1800)
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])


def call_claude(system, user, schema=THREAD_SCHEMA):
    cfg = config.get()
    # No MCP servers, settings or skills: otherwise every call carries ~7k tokens of tool listings and setup.
    cmd = ["claude", "-p", "--model", cfg.model, "--no-session-persistence", "--output-format", "json",
           "--json-schema", json.dumps(schema), "--tools", "", "--system-prompt", system,
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources=",
           "--disable-slash-commands", "--effort", cfg.effort]
    for attempt in range(3):
        r = subprocess.run(cmd, input=user, capture_output=True, text=True, timeout=1800)
        if r.returncode == 0:
            break
        try:
            out = json.loads(r.stdout)
            error = f"{out.get('subtype')}: {str(out.get('result'))[:300]}"
        except json.JSONDecodeError:
            error = (r.stderr or r.stdout)[-300:]
        if REFUSAL.search(error):
            raise Refused(error)            # asking again gets the same answer and costs again
        if attempt == 2:
            raise RuntimeError(error)
        time.sleep(20 * (attempt + 1))      # usually rate limiting when several run in parallel
    out = json.loads(r.stdout)
    result = out.get("structured_output") or out.get("result")
    if isinstance(result, str) and REFUSAL.search(result):
        raise Refused(result[:200])
    return result if isinstance(result, dict) else json.loads(result)


class Refused(Exception):
    """The model declined the content. Recorded as analyzed, never retried."""


REFUSAL = re.compile(r"can't help with this|cannot help with this|unable to help with this|content policy", re.I)


# `kifu demo` and the tests install a function (system, user, schema) -> dict here.
fixture = None


def call_fixture(system, user, schema=THREAD_SCHEMA):
    if fixture is None and config.get().demo:
        from . import demo  # a demo store analyzes with scripted answers, in whichever process opens it
        demo.install()
    if fixture is None:
        raise RuntimeError("the fixture backend needs kifu.analyze.fixture to be set")
    return fixture(system, user, schema)


BACKENDS = {"claude": call_claude, "openai": call_openai, "fixture": call_fixture}


def cache_key(session_digest, backend):
    """Re-read a session only when its turns change, the prompt changes, or the backend changes."""
    return hashlib.sha1(f"{session_digest}|{PROMPT_VERSION}|{backend}".encode()).hexdigest()[:16]


def analyze_session(con_factory, session_id, backend):
    con = con_factory()
    s = con.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    key = cache_key(s["digest_hash"], backend)
    if con.execute("SELECT 1 FROM analyzed WHERE session_id=? AND digest_hash=?", (session_id, key)).fetchone():
        return session_id, 0, "cached"
    moves, blocks = digest_moves(con, session_id)
    if not moves:
        con.execute("INSERT OR REPLACE INTO analyzed VALUES (?,?,?,?)", (session_id, key, backend, 0))
        con.commit()
        return session_id, 0, "empty"
    call = BACKENDS[backend]
    system = system_prompt()
    results, offset, earlier = [], 0, []
    t0 = time.time()
    for part in chunks(blocks):
        context = ""
        if earlier:
            context = ("Threads already found in earlier parts of this session (continue them if they come back, "
                       "reuse the exact title):\n" + "\n".join(f"- {t}" for t in earlier) + "\n\n")
        user = (f"Session: {s['title'] or s['ai_title'] or ''} | project {s['project']} | "
                f"{s['started'][:10]} .. {s['ended'][:10]}\n\n{context}" + "\n\n".join(part))
        try:
            out = call(system, user)
        except Refused:
            con.execute("INSERT OR REPLACE INTO analyzed VALUES (?,?,?,?)", (session_id, key, f"{backend}:refused", 0))
            con.commit()
            return session_id, 0, "refused by the model (recorded, not retried)"
        for t in out.get("threads", []):
            t["_offset"] = offset
            results.append(t)
            earlier.append(t["title"])
        offset += len(part)
    con.execute("DELETE FROM threads WHERE session_id=?", (session_id,))
    merged = merge_same_title(results)
    con.execute("INSERT OR REPLACE INTO analyzed VALUES (?,?,?,?)", (session_id, key, backend, len(merged)))
    for t in merged:
        first = max(1, min(len(moves), t["first_move"])) - 1
        last = max(1, min(len(moves), t["last_move"])) - 1
        con.execute("""INSERT INTO threads(session_id, title, summary, kind, status, first_turn, last_turn, first_ts,
                       last_ts, quote, next_step, keywords, backend, digest_hash, loose_ends)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (session_id, t["title"], t["summary"], t["kind"], t["status"], moves[first]["first_idx"],
                     moves[last]["last_idx"], moves[first]["ts_first"], moves[last]["ts_last"], t["quote"],
                     t["next_step"], json.dumps(t["keywords"]), backend, key, json.dumps(t["loose_ends"])))
    con.commit()
    return session_id, len(merged), f"{time.time() - t0:.0f}s"


def merge_same_title(threads):
    """Chunked sessions report a continued thread again under the same title."""
    by_title = {}
    for t in threads:
        t = dict(t)
        t["first_move"] += t.get("_offset", 0)
        t["last_move"] += t.get("_offset", 0)
        key = t["title"].strip().lower()
        if key not in by_title:
            by_title[key] = t
            continue
        old = by_title[key]
        old["last_move"] = max(old["last_move"], t["last_move"])
        old["first_move"] = min(old["first_move"], t["first_move"])
        old["status"], old["summary"], old["next_step"] = t["status"], t["summary"], t["next_step"]
        old["loose_ends"] = t["loose_ends"]
    return list(by_title.values())


def analyze(con_factory, backend=None, workers=None, only=None, log=print):
    cfg = config.get()
    backend = backend or cfg.backend
    if backend == "local":          # the name before 1.0
        backend = "openai"
    workers = workers or cfg.workers
    con = con_factory()
    q = "SELECT id FROM sessions WHERE automated=0"
    params = []
    if only:
        q += " AND id >= ? AND id < ?"
        params += prefix_range(only)
    ids = [r["id"] for r in con.execute(q + " ORDER BY bytes", params)]
    log(f"analyzing {len(ids)} sessions with {backend}, {workers} in parallel")
    done = failed = 0
    with cf.ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(analyze_session, con_factory, sid, backend): sid for sid in ids}
        for f in cf.as_completed(futures):
            done += 1
            try:
                sid, n, note = f.result()
                if note != "cached":
                    log(f"  {done}/{len(ids)} {sid[:8]} {n} threads {note}")
            except Exception as e:  # keep going; a rerun picks up what failed
                failed += 1
                log(f"  {done}/{len(ids)} {futures[f][:8]} FAILED {str(e)[:200]}")
    return failed
