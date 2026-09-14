"""kifu web app and API.

    /            the web app (Aji, Timeline, Sessions, Habits)
    /api/...     JSON API, documented at /docs
Run: kifu serve   (or: uvicorn kifu.api:app --host 127.0.0.1 --port 8765)
"""
import collections
import contextlib
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import uuid
from typing import Literal

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from . import __version__, config, db, habits, marks, report

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOB_KINDS = ("pull", "scan", "embed", "analyze", "link", "verify", "run", "drain")

@contextlib.asynccontextmanager
async def _lifespan(_app):
    threading.Thread(target=_queue_watcher, daemon=True, name="kifu-queue").start()
    yield


app = FastAPI(title="kifu", version=__version__, lifespan=_lifespan,
              description="Ideas, loose ends and work habits recovered from Claude Code sessions.")

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _hostname(value):
    """Host or Origin value without scheme and port: 'http://[::1]:8765' -> '::1'."""
    value = value.split("://", 1)[-1].split("/", 1)[0]
    if value.startswith("["):
        return value[1:value.find("]")]
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


@app.middleware("http")
async def only_this_machine(request: Request, call_next):
    """Refuse DNS rebinding and cross-site requests.

    Binding to 127.0.0.1 is not enough: a web page can point its own domain at 127.0.0.1 and read the API
    as a same-origin page. That request carries the page's domain in Host, so only loopback names (and
    configured allowed_hosts) are served. A request that changes something is also refused when a browser
    says it comes from another origin.
    """
    allowed = LOOPBACK | set(config.get().allowed_hosts)
    if _hostname(request.headers.get("host", "")) not in allowed:
        return JSONResponse({"detail": "host not allowed"}, status_code=421)
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin and _hostname(origin) not in allowed:
        return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
    return await call_next(request)


def con():
    return db.connect()


# ---- cached report payload -------------------------------------------------------------------------------
# Habits take ~1.5 s and depend only on sessions and threads; the idea list takes ~0.5 s and also on marks.
# Each part is cached under a cheap fingerprint of the data it depends on, and the serialized JSON is kept
# too, because FastAPI's own encoder needs several seconds for the 2 MB payload.

_cache = {}
_cache_lock = threading.Lock()


def _fingerprint(c, with_marks):
    # Content, not just ids: `kifu link` deletes and re-inserts lines, and SQLite reuses their ids.
    parts = [tuple(c.execute("SELECT COUNT(*), MAX(ended), SUM(n_turns) FROM sessions").fetchone()),
             tuple(c.execute("SELECT COUNT(*), MAX(id), TOTAL(LENGTH(summary)), TOTAL(LENGTH(loose_ends)) "
                             "FROM threads").fetchone()),
             tuple(c.execute("SELECT COUNT(*), TOTAL(score), TOTAL(LENGTH(title) + LENGTH(summary) + "
                             "LENGTH(COALESCE(verdict, '')) + LENGTH(COALESCE(loose_ends, ''))), MAX(last_ts) "
                             "FROM lines").fetchone())]
    if with_marks:
        parts.append(tuple(c.execute("SELECT COUNT(*), MAX(updated) FROM marks").fetchone()))
    return repr(parts)


def payload():
    with _cache_lock:
        c = con()
        hkey = _fingerprint(c, with_marks=False)
        if _cache.get("habits_key") != hkey:
            _cache["habits"] = habits.compute(c)
            _cache["habits_key"] = hkey
        key = _fingerprint(c, with_marks=True)
        if _cache.get("key") != key:
            _cache["data"] = report.collect(c, habits_data=_cache["habits"])
            _cache["json"] = json.dumps(_cache["data"], ensure_ascii=False).encode()
            _cache["key"] = key
        return _cache["data"]


def payload_json():
    payload()
    return _cache["json"]


def _invalidate():
    """Nothing to drop: fingerprints notice changes. Kept as the one place to hook if that ever changes."""


# ---- web app -----------------------------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    with open(report.TEMPLATE_PATH) as fh:
        return fh.read()


# ---- read API ----------------------------------------------------------------------------------------------

@app.get("/api/health")
def health():
    c = con()
    row = c.execute("SELECT COUNT(*) n, MAX(ended) last FROM sessions").fetchone()
    return {"ok": True, "sessions": row["n"], "latest_session": row["last"], "job": _current_job_summary()}


@app.get("/api/report", summary="Everything the web app shows, in one payload")
def get_report():
    return Response(content=payload_json(), media_type="application/json")


@app.get("/api/stats")
def stats():
    return payload()["stats"]


@app.get("/api/overview", summary="Header numbers, habit summary, top open ideas, questions left behind")
def overview(top: int = Query(5, le=50)):
    data = payload()
    open_lines = [l for l in data["lines"] if _open(l)]
    cfg = config.get()
    return {"stats": data["stats"], "summary": data["habits"].get("summary", {}),
            "top": [report.compact(l, data) for l in open_lines[:top]],
            "left_behind": data["habits"].get("questions", {}).get("left_behind", [])[:10],
            "job": _current_job_summary(),
            "config": {"user": cfg.user, "timezone": data["habits"].get("timezone"),
                       "hosts": [s.host for s in cfg.source_list()], "backend": cfg.backend, "demo": cfg.demo}}


@app.get("/api/digest", summary="Open ideas that went quiet, as structured items and as a ready message")
def digest(quiet_days: int = Query(21, ge=0), limit: int = Query(5, le=50)):
    data = payload()
    items = [report.compact(l, data) for l in data["lines"] if _open(l)]
    items = [i for i in items if i["quiet_days"] >= quiet_days
             and not (i["activity"] and i["activity"]["likely_done"])][:limit]
    if not items:
        return {"items": [], "text": f"No open idea has been quiet for {quiet_days} days or more."}
    lines = [f"{len(items)} idea{'s' if len(items) > 1 else ''} quiet for {quiet_days}+ days:"]
    for i in items:
        lines.append(f"\n• {i['title']} ({i['project']}, {i['status']}, quiet {i['quiet_days']} days)")
        for loose in i["loose_ends"][:2]:
            lines.append(f"  – {loose}")
    return {"items": items, "text": "\n".join(lines)}


def _open(line):
    return line["score"] > 0 and not line["mark"]


@app.get("/api/lines", summary="Ideas followed across sessions")
def lines(status: str | None = Query(None, description="comma separated: shipped,started,proposed,parked,dropped,answered"),
          open_only: bool = Query(False, alias="open", description="only open, unmarked ideas"),
          area: str | None = None, q: str | None = None, marked: bool | None = None, tool: str | None = None,
          personal: bool = Query(False, description="include ideas that are only private conversation"),
          compact: bool = Query(False, description="short items for agents and notifications"),
          limit: int = Query(50, le=1000), offset: int = 0):
    items = payload()["lines"]
    if status:
        wanted = set(status.split(","))
        items = [l for l in items if l["status"] in wanted]
    if open_only:
        items = [l for l in items if _open(l)]
    if not personal:
        items = [l for l in items if not l["kinds"] or set(l["kinds"]) != {"personal"}]
    if marked is not None:
        items = [l for l in items if bool(l["mark"]) == marked]
    if area:
        items = [l for l in items if area in l["areas"]]
    if tool:
        items = [l for l in items if tool in l["tools"]]
    if q:
        words = q.lower().split()
        def hay(l):
            return " ".join([l["title"], l["summary"], l["verdict"], l["next"], *l["loose"],
                             *[t["quote"] or "" for t in l["threads"]]]).lower()
        items = [l for l in items if all(w in hay(l) for w in words)]
    page = items[offset:offset + limit]
    if compact:
        data = payload()
        page = [report.compact(l, data) for l in page]
    return {"total": len(items), "items": page}


def _line(anchor):
    for l in payload()["lines"]:
        if l["anchor"] == anchor or str(l["id"]) == anchor:
            return l
    raise HTTPException(404, f"no idea with anchor {anchor}")


@app.get("/api/lines/{anchor}", summary="One idea with its trail through sessions")
def line(anchor: str):
    return _line(anchor)


@app.get("/api/sessions")
def sessions(host: str | None = None, project: str | None = None, q: str | None = None,
             limit: int = Query(100, le=1000), offset: int = 0):
    items = list(reversed(payload()["sessions"]))
    if host:
        items = [s for s in items if s["host"] == host]
    if project:
        items = [s for s in items if project in s["project"]]
    if q:
        items = [s for s in items if q.lower() in (s["title"] + " " + s["project"]).lower()]
    return {"total": len(items), "items": items[offset:offset + limit]}


@app.get("/api/sessions/{session_id}", summary="One session: turns, threads, evidence")
def session(session_id: str, turns: bool = True):
    c = con()
    s = c.execute("SELECT * FROM sessions WHERE id >= ? AND id < ? LIMIT 2", db.prefix_range(session_id)).fetchall()
    if len(s) != 1:
        raise HTTPException(404 if not s else 409, "no such session" if not s else "ambiguous session prefix")
    s = dict(s[0])
    out = {"session": s,
           "threads": [dict(t, loose_ends=json.loads(t["loose_ends"] or "[]"), keywords=json.loads(t["keywords"] or "[]"))
                       for t in c.execute("""SELECT id, title, summary, kind, status, first_turn, last_turn, first_ts, last_ts,
                                            quote, next_step, keywords, loose_ends, line_id FROM threads
                                            WHERE session_id=? ORDER BY first_ts""", (s["id"],))],
           "evidence": {e["kind"]: e["n"] for e in c.execute(
               "SELECT kind, COUNT(*) n FROM evidence WHERE session_id=? GROUP BY kind", (s["id"],))}}
    if turns:
        out["turns"] = [dict(t) for t in c.execute(
            "SELECT idx, ts, ended, prompt, reply, n_tools, dup_of FROM turns WHERE session_id=? ORDER BY idx", (s["id"],))]
    return out


@app.get("/api/blame", summary="git blame for intent: the session, prompt and idea behind lines of a file")
def get_blame(file: str, start: int | None = Query(None, ge=1), end: int | None = Query(None, ge=1)):
    from . import blame
    target = f"{file}:{start}-{end or start}" if start else file
    try:
        return blame.blame(con(), target)
    except blame.BlameError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/dejavu", summary="Earlier ideas a session's prompt resembles, as context for Claude (the prompt hook)")
def dejavu(prompt: str = Body(...), session_id: str | None = Body(None)):
    from . import dejavu as dv
    return dv.check(con(), prompt, session_id) or {"ideas": [], "context": None}


@app.get("/api/rules", summary="Corrections you keep repeating, as rules: the last answer, without a model call")
def get_rules():
    from . import rules
    c = con()
    c.execute(rules.SCHEMA_SQL)
    row = c.execute("SELECT result, created FROM rule_runs ORDER BY created DESC LIMIT 1").fetchone()
    if not row:
        return {"rules": [], "created": None, "note": "run `kifu rules` once"}
    cands = rules.candidates(c)
    return {"rules": rules._checked(json.loads(row["result"]).get("rules", []), cands, rules.rule_files()),
            "created": row["created"]}


@app.get("/api/memory-check", summary="Memory and CLAUDE.md files that name paths, files or projects which are gone")
def memory_check():
    from . import memcheck
    return memcheck.check(con())


@app.get("/api/habits", summary="Rhythm, focus, reply time, juggling, questions left behind")
def get_habits():
    return payload()["habits"]


# ---- marks -------------------------------------------------------------------------------------------------

@app.put("/api/lines/{anchor}/mark", summary="Mark an idea done, dismissed, or open again")
def mark(anchor: str, state: Literal["done", "dismissed", "open"] = Body(..., embed=True),
         note: str = Body("", embed=True)):
    line_ = _line(anchor)
    marks.set_mark(con(), line_["anchor"], state, note)
    _invalidate()
    return _line(line_["anchor"])


@app.put("/api/lines/{anchor}/title", summary="Rename an idea; an empty title restores the analyzed one")
def rename(anchor: str, title: str = Body(..., embed=True)):
    line_ = _line(anchor)
    marks.set_title(con(), line_["anchor"], title)
    return _line(line_["anchor"])


# ---- corrections -------------------------------------------------------------------------------------------

def _correct(kind, keys):
    c = con()
    cur = c.execute("INSERT INTO corrections(kind, keys, created) VALUES (?,?,?)",
                    (kind, json.dumps(sorted(set(keys))), dt.datetime.now(dt.UTC).isoformat(timespec="seconds")))
    c.commit()
    job = _start("link")
    return {"correction": cur.lastrowid, "job": {k: job[k] for k in ("id", "kind", "status")} if job else None,
            "note": None if job else "another job is running; the correction applies on the next link"}


@app.post("/api/lines/{anchor}/merge", summary="These two ideas are one: keep their threads together")
def merge(anchor: str, into: str = Body(..., embed=True)):
    a, b = _line(anchor), _line(into)
    if a["anchor"] == b["anchor"]:
        raise HTTPException(400, "an idea cannot be merged with itself")
    return _correct("merge", [t["key"] for t in a["threads"] + b["threads"]])


@app.post("/api/lines/{anchor}/detach", summary="This thread is not part of the idea: make it an idea of its own")
def detach(anchor: str, thread: str = Body(..., embed=True, description="the thread's key")):
    line_ = _line(anchor)
    if thread not in {t["key"] for t in line_["threads"]}:
        raise HTTPException(404, "that thread is not part of this idea")
    if len(line_["threads"]) < 2:
        raise HTTPException(400, "an idea with one thread has nothing to detach from")
    return _correct("detach", [thread])


@app.get("/api/corrections")
def corrections():
    return [dict(r, keys=json.loads(r["keys"])) for r in con().execute("SELECT * FROM corrections ORDER BY id")]


@app.delete("/api/corrections/{correction_id}", summary="Take a correction back")
def delete_correction(correction_id: int):
    c = con()
    if not c.execute("DELETE FROM corrections WHERE id=?", (correction_id,)).rowcount:
        raise HTTPException(404, "no such correction")
    c.commit()
    job = _start("link")
    return {"deleted": correction_id, "job": {k: job[k] for k in ("id", "kind", "status")} if job else None}


# ---- jobs --------------------------------------------------------------------------------------------------

_jobs = collections.OrderedDict()
_jobs_lock = threading.Lock()


def _current_job_summary():
    with _jobs_lock:
        for j in reversed(_jobs.values()):
            if j["status"] == "running":
                return {k: j[k] for k in ("id", "kind", "status", "started")}
    return None


def _run_job(job):
    cmd = [sys.executable, "-m", "kifu", job["kind"]]
    proc = subprocess.Popen(cmd, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                            env={**os.environ, "PYTHONUNBUFFERED": "1"})
    for line_ in proc.stdout:
        if "FutureWarning" in line_ or line_.startswith("  warn("):
            continue
        job["log"].append(line_.rstrip())
        del job["log"][:-2000]
    job["exit_code"] = proc.wait()
    job["status"] = "done" if job["exit_code"] == 0 else "failed"
    job["finished"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    _invalidate()


@app.post("/api/jobs", status_code=202, summary="Start pull, scan, embed, analyze, link or run in the background")
def start_job(kind: Literal["pull", "scan", "embed", "analyze", "link", "verify", "run", "drain"] = Body(..., embed=True)):
    job = _start(kind)
    if job is None:
        with _jobs_lock:
            running = next(j for j in _jobs.values() if j["status"] == "running")
        raise HTTPException(409, f"job {running['id']} ({running['kind']}) is still running")
    return {k: job[k] for k in ("id", "kind", "status", "started")}


def _start(kind):
    """Start a job unless one is running; returns the job or None."""
    with _jobs_lock:
        if any(j["status"] == "running" for j in _jobs.values()):
            return None
        job = {"id": uuid.uuid4().hex[:12], "kind": kind, "status": "running", "log": [], "exit_code": None,
               "started": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "finished": None}
        _jobs[job["id"]] = job
        while len(_jobs) > 50:
            _jobs.popitem(last=False)
    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return job


# ---- queue from the SessionEnd hook ------------------------------------------------------------------------

_queue_event = threading.Event()


@app.post("/api/queue", summary="A session ended (the SessionEnd hook): analyze queued sessions soon")
def queue():
    from . import drain
    _queue_event.set()
    return {"pending": drain.pending()}


def _queue_watcher():
    """Start a drain job when sessions are queued and nothing else runs; the drain waits for the burst to end."""
    from . import drain
    while True:
        _queue_event.wait(timeout=60)
        _queue_event.clear()
        try:
            if drain.pending():
                _start("drain")
        except Exception:  # the watcher must outlive a bad tick
            pass


@app.get("/api/jobs")
def jobs():
    with _jobs_lock:
        return [{k: j[k] for k in ("id", "kind", "status", "started", "finished", "exit_code")} for j in reversed(_jobs.values())]


@app.get("/api/jobs/{job_id}")
def job(job_id: str, tail: int = Query(200, le=2000)):
    with _jobs_lock:
        j = _jobs.get(job_id)
        if not j:
            raise HTTPException(404, "no such job")
        return {**{k: j[k] for k in ("id", "kind", "status", "started", "finished", "exit_code")}, "log": j["log"][-tail:]}
