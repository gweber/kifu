"""Stage 1: read Claude Code session files into SQLite. Deterministic, no LLM.

A turn starts with a prompt the user typed and collects everything the assistant did
until the next one: the reply text, files written, commits, cards, PRs.
"""
import collections
import glob
import hashlib
import json
import os
import re
import shlex

from . import config
from .redact import redact

ACTIVE_GAP_MIN = 45        # silence that separates a turn's own work from later autonomous wakeups
REPLY_KEEP = 2400          # chars of assistant text kept per turn (head + tail)
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

_TAG_BLOCKS = re.compile(
    r"<(ide_opened_file|ide_selection|system-reminder|local-command-stdout|local-command-stderr)>.*?</\1>",
    re.S)
_NOT_PROMPTS = ("<task-notification>", "<local-command", "Caveat: The messages below",
                "This session is being continued from a previous conversation")


def clean_prompt(text):
    """Strip IDE and harness wrappers; return '' when nothing a human wrote is left."""
    if text.lstrip().startswith(_NOT_PROMPTS):
        return ""
    m = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
    if m:
        args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
        return f"{m.group(1).strip()} {args.group(1).strip() if args else ''}".strip()
    text = _TAG_BLOCKS.sub("", text)
    text = text.replace("[Request interrupted by user]", "").replace("[Request interrupted by user for tool use]", "")
    return text.strip()


def _minutes(a, b):
    from datetime import datetime
    parse = lambda x: datetime.fromisoformat(x.replace("Z", "+00:00"))
    return (parse(b) - parse(a)).total_seconds() / 60


def _text_of(content):
    if isinstance(content, str):
        return content
    return "\n".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text")


def is_human_prompt(r):
    if r.get("type") != "user" or r.get("isMeta") or r.get("isCompactSummary") or r.get("isSidechain"):
        return False
    origin = r.get("origin")
    if origin is not None:
        return origin.get("kind") == "human"
    content = r.get("message", {}).get("content")
    if isinstance(content, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in content):
        return False
    return bool(_text_of(content).strip())


def _clip_reply(texts):
    joined = "\n\n".join(t.strip() for t in texts if t.strip())
    if len(joined) <= REPLY_KEEP:
        return joined
    head = texts[0].strip()[:400]
    return head + "\n[...]\n" + joined[-(REPLY_KEEP - 420):]


def classify_bash(cmd):
    """Map a shell command to evidence of work that left the conversation."""
    out = []
    if re.search(r"\bgit\b[^|;&]*\bcommit\b", cmd):
        m = re.search(r"-m\s+(\"[^\"]*\"|'[^']*'|\$\(cat <<'?EOF'?\n.*?\n)", cmd, re.S)
        msg = m.group(1).strip("\"'").replace("$(cat <<'EOF'", "").strip() if m else ""
        out.append(("commit", msg.splitlines()[0][:200] if msg else ""))
    if re.search(r"\bgit\b[^|;&]*\bpush\b", cmd):
        out.append(("push", ""))
    if re.search(r"\bgh\s+pr\s+create\b", cmd):
        out.append(("pr", ""))
    if re.search(r"\bkanban\b.*\bcreate\b", cmd):
        m = re.search(r"--title[= ](\"[^\"]*\"|'[^']*'|\S+)", cmd)
        if not m:
            try:
                parts = shlex.split(cmd.splitlines()[0])
                i = parts.index("create")
                m_title = parts[i + 1] if i + 1 < len(parts) and not parts[i + 1].startswith("-") else ""
            except (ValueError, IndexError):
                m_title = ""
        else:
            m_title = m.group(1).strip("\"'")
        out.append(("card", m_title[:200]))
    if re.search(r"\b(systemctl\s+(--user\s+)?(restart|enable)|docker\s+compose\s+up)\b", cmd):
        out.append(("deploy", cmd.splitlines()[0][:160]))
    return out


def ask_outcome(ask_input, result):
    """answered or dismissed, and per question whether the recommended option, another option or free text was picked."""
    text = result.get("content")
    if not isinstance(text, str):
        text = " ".join(x.get("text", "") for x in text or [] if isinstance(x, dict))
    questions = ask_input.get("questions") or []
    if result.get("is_error") or not text.startswith("Your questions have been answered"):
        return {"outcome": "dismissed", "n": len(questions)}
    picks = []
    for q in questions:
        m = re.search(re.escape(f'"{q.get("question", "")}"="') + r'(.*?)"(?:, "|\. You can)', text, re.S)
        if not m:
            continue
        answer = m.group(1)
        labels = [o.get("label", "") for o in q.get("options") or []]
        chosen = [l for l in labels if l and l in answer]
        had_rec = any("(Recommended)" in l for l in labels)
        if any("(Recommended)" in l for l in chosen):
            pick = "recommended"
        elif chosen:
            pick = "option"
        else:
            pick = "own words"
        picks.append({"pick": pick, "had_rec": had_rec})
    return {"outcome": "answered", "n": len(questions), "picks": picks}


class SessionParse:
    def __init__(self, session_id, path):
        self.id = session_id
        self.path = path
        self.turns = []              # dicts
        self.evidence = []           # (turn_idx, ts, kind, value, source)
        self.meta = collections.Counter()
        self.cwds = collections.Counter()
        self.branch = None
        self.entrypoint = None
        self.title = None
        self.ai_title = None
        self.agent_name = None
        self.started = None
        self.ended = None
        self.teleported = []
        self.pr_links = []
        self.tool = "claude"
        self.automated = None       # None: decided by store() from turns and markers

    def add_evidence(self, kind, value, ts, source="main"):
        idx = len(self.turns) - 1 if self.turns else None
        self.evidence.append((idx, ts, kind, value, source))


def queued_prompt(r):
    """The text of a message the user sent while the assistant was working, or None."""
    a = r.get("attachment")
    if not isinstance(a, dict) or a.get("type") != "queued_command" or (a.get("origin") or {}).get("kind") != "human":
        return None
    if a.get("commandMode") not in (None, "prompt"):
        return None
    text = a.get("prompt")
    text = _text_of(text) if isinstance(text, list) else str(text or "")
    return clean_prompt(text) or None


def touch(cur, ts):
    """A turn ends at the last activity before a long silence; later wakeups run on their own."""
    if cur is None or not ts or cur["detached"]:
        return
    if _minutes(cur["ended"], ts) > ACTIVE_GAP_MIN:
        cur["detached"] = True
    else:
        cur["ended"] = ts


def new_turn(sp, ts, uuid, prompt, queued=False):
    turn = {"idx": len(sp.turns), "ts": ts, "ended": ts, "detached": False, "uuid": uuid, "prompt": prompt,
            "texts": [], "n_tools": 0, "files": [], "queued": queued}
    sp.turns.append(turn)
    return turn


def _already_queued(sp, prompt, ts):
    """A queued message that Claude Code later also records as a regular prompt is one turn, not two."""
    for turn in reversed(sp.turns[-5:]):
        if turn.get("queued") and turn["prompt"] == prompt and ts and _minutes(turn["ts"], ts) < 60:
            return True
    return False


def _scan_records(path):
    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _tool_evidence(sp, block, ts, source):
    name = block.get("name")
    inp = block.get("input") or {}
    if name in WRITE_TOOLS:
        fp = inp.get("file_path") or inp.get("notebook_path")
        if fp:
            sp.add_evidence("write", fp, ts, source)
            return fp
    elif name == "Bash":
        for kind, value in classify_bash(inp.get("command", "")):
            sp.add_evidence(kind, value, ts, source)
    elif name == "Artifact" and inp.get("file_path") and inp.get("action", "publish") == "publish":
        sp.add_evidence("artifact", inp["file_path"], ts, source)
    return None


def parse_session(path):
    session_id = os.path.basename(path)[:-len(".jsonl")]
    sp = SessionParse(session_id, path)
    cur = None
    asks = {}                    # AskUserQuestion tool_use id -> input, until its result arrives
    for r in _scan_records(path):
        t = r.get("type")
        ts = r.get("timestamp")
        if ts:
            sp.started = sp.started or ts
            sp.ended = ts
        if t == "custom-title":
            sp.title = r.get("customTitle") or sp.title
        elif t == "ai-title":
            sp.ai_title = r.get("aiTitle") or sp.ai_title
        elif t == "agent-name":
            sp.agent_name = r.get("agentName") or sp.agent_name
        elif t == "summary" and not sp.ai_title:
            sp.ai_title = r.get("summary")
        elif t == "teleported-from":
            sp.teleported.append(r.get("remoteSessionId"))
        elif t == "pr-link":
            sp.pr_links.append(r.get("prUrl"))
        elif t == "frame-link" and r.get("frameUrl"):
            sp.add_evidence("artifact", r["frameUrl"], ts)
        elif t == "system" and r.get("subtype") == "compact_boundary":
            sp.meta["compactions"] += 1
        elif t == "attachment" and not r.get("isSidechain"):
            queued = queued_prompt(r)
            if queued:
                # Typed while the assistant was still working: often a different idea, and the easiest to lose.
                cur = new_turn(sp, ts, r.get("uuid"), queued, queued=True)
        if t not in ("user", "assistant"):
            continue
        if r.get("cwd"):
            sp.cwds[r["cwd"]] += 1
        sp.branch = r.get("gitBranch") or sp.branch
        sp.entrypoint = sp.entrypoint or r.get("entrypoint")
        if r.get("isSidechain"):
            continue
        if t == "user":
            content = r.get("message", {}).get("content")
            if isinstance(content, list) and asks:
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in asks:
                        sp.add_evidence("ask", json.dumps(ask_outcome(asks.pop(b["tool_use_id"]), b)), ts)
            if is_human_prompt(r):
                prompt = clean_prompt(_text_of(r["message"]["content"]))
                if prompt and not _already_queued(sp, prompt, ts):
                    cur = new_turn(sp, ts, r.get("uuid"), prompt)
            continue
        touch(cur, ts)
        for block in r.get("message", {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and cur is not None:
                cur["texts"].append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                sp.meta["tool_calls"] += 1
                if block.get("name") == "AskUserQuestion":
                    asks[block.get("id")] = block.get("input") or {}
                if cur is not None:
                    cur["n_tools"] += 1
                fp = _tool_evidence(sp, block, ts, "main")
                if fp and cur is not None and fp not in cur["files"]:
                    cur["files"].append(fp)
    for url in dict.fromkeys(sp.pr_links):
        sp.add_evidence("pr", url, None)
    return sp


def parse_subagent(path, sp):
    """Subagents do work on behalf of a session; their writes and commits count as its evidence."""
    for r in _scan_records(path):
        if r.get("type") != "assistant":
            continue
        for block in r.get("message", {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                before = len(sp.evidence)
                _tool_evidence(sp, block, r.get("timestamp"), "subagent")
                # Subagent evidence is not tied to a turn.
                sp.evidence[before:] = [(None, *e[1:]) for e in sp.evidence[before:]]


def project_name(cwd, roots=None):
    """The same name for the same project on every machine.

    The home directory is dropped whoever owns it (/Users/ada/code/x and /home/ada/code/x are both
    code/x), then the first matching project root (~/code -> code/) is dropped too, leaving x.
    """
    if not cwd:
        return "?"
    m = re.match(r"^/(?:home|Users)/[^/]+(?:/(.*))?$", cwd.rstrip("/"))
    if not m:
        return cwd
    rel = m.group(1) or ""
    if not rel:
        return "~"
    for root in config.get().project_roots if roots is None else roots:
        # Roots are home-relative on every machine: ~/code, /home/ada/code and /Users/ada/code all mean code/.
        prefix = re.sub(r"^(?:~|/(?:home|Users)/[^/]+)/?", "", root).strip("/")
        if prefix and rel.startswith(prefix + "/"):
            return rel[len(prefix) + 1:]
    return rel


DEFAULT_IGNORED_PROJECTS = ["?", "~", "/tmp*", "/var/*", "/private/*", "_*"]


def is_project(area):
    """Whether an area names a real project, or a place sessions merely ran in (home, tmp, benchmarks)."""
    import fnmatch
    if not area:
        return False
    cfg = config.get()
    roots = {re.sub(r"^(?:~|/(?:home|Users)/[^/]+)/?", "", r).strip("/") for r in cfg.project_roots}
    if area in roots:
        return False                    # a session started in ~/code itself
    return not any(fnmatch.fnmatch(area, pattern) for pattern in DEFAULT_IGNORED_PROJECTS + cfg.ignore_projects)


def area_of(project):
    """Top-level project: tidepool/web/frontend -> tidepool."""
    return project.split("/")[0]


def digest_hash(turns):
    h = hashlib.sha1()
    for t in turns:
        h.update((t["uuid"] or "").encode())
        h.update(str(len(t["texts"])).encode())
    return h.hexdigest()[:16]


def discover(root):
    """Top-level session files and the subagent files that belong to each."""
    sessions = {}
    for path in glob.glob(os.path.join(root, "*", "*.jsonl")):
        sid = os.path.basename(path)[:-len(".jsonl")]
        subs = glob.glob(os.path.join(os.path.dirname(path), sid, "subagents", "**", "*.jsonl"), recursive=True)
        sessions[sid] = (path, subs)
    return sessions


def _stat_key(paths):
    return sum(os.path.getsize(p) for p in paths), max(os.path.getmtime(p) for p in paths)


def scan(con, root, force=False, log=print, host=None):
    found = discover(root)
    lo, hi = os.path.join(root, ""), os.path.join(root, "") + "\uffff"
    known = {r["session_id"]: (r["size"], r["mtime"]) for r in con.execute(
        "SELECT * FROM files WHERE kind='session' AND path >= ? AND path < ?", (lo, hi))}
    todo = []
    for sid, (path, subs) in found.items():
        key = _stat_key([path, *subs])
        if force or known.get(sid) != key:
            todo.append((sid, path, subs, key))
    log(f"{len(found)} sessions under {root}, {len(todo)} new or changed")
    for n, (_sid, path, subs, key) in enumerate(sorted(todo, key=lambda x: x[3][0]), 1):
        sp = parse_session(path)
        for sub in subs:
            parse_subagent(path=sub, sp=sp)
        store(con, sp, n_subagents=len(subs), size=key[0], mtime=key[1], host=host)
        if n % 20 == 0 or n == len(todo):
            log(f"  {n}/{len(todo)}  {project_name(sp.cwds.most_common(1)[0][0] if sp.cwds else '')}  {len(sp.turns)} turns")
        con.commit()
    link_forks(con)
    con.commit()
    if todo:
        con.execute("PRAGMA optimize")
    return len(todo)


def scan_archive(con, force=False, log=print):
    """Scan every source. A session that is gone upstream stays in the archive and in the database."""
    from . import sources
    archive = os.path.join(config.get().archive_dir, "")
    live = [sources.source_root(src) for src in config.get().source_list() if not sources.archived(src)]
    con.execute("DELETE FROM files WHERE kind='session' AND NOT (path >= ? AND path < ?)"
                + "".join(" AND NOT (path >= ? AND path < ?)" for _ in live),
                (archive, archive + "\uffff", *[x for root in live for x in (root, root + "\uffff")]))
    n = 0
    for src in config.get().source_list():
        root = sources.source_root(src)
        if not os.path.exists(root):
            continue
        say = (lambda m, h=src.host, k=src.kind: log(f"{h}/{k}: {m}"))
        if src.kind == "claude":
            n += scan(con, root, force=force, log=say, host=src.host)
        else:
            n += scan_agent(con, src.kind, root, force=force, log=say, host=src.host)
    return n


def scan_agent(con, kind, root, force=False, log=print, host=None):
    """Sessions of another coding agent (see agents.py), with the same change detection as Claude Code files."""
    from . import agents
    items, parse, _ = agents.ADAPTERS[kind]
    found = items(root)
    prefix = f"{root}#"
    known = {r["path"]: (r["size"], r["mtime"]) for r in con.execute(
        "SELECT * FROM files WHERE kind='session' AND path >= ? AND path < ?", (prefix, prefix + "\uffff"))}
    cwds = [r["cwd"] for r in con.execute("SELECT DISTINCT cwd FROM sessions WHERE host IS ? AND cwd IS NOT NULL", (host,))]
    todo = [(sid, key, where) for sid, (key, where) in found.items() if force or known.get(prefix + sid) != tuple(key)]
    log(f"{len(found)} {kind} sessions, {len(todo)} new or changed")
    for sid, key, where in todo:
        try:
            sp = parse(sid, where, known_cwds=cwds)
        except Exception as exc:  # one unreadable session must not stop the others
            log(f"  {kind} {sid}: {type(exc).__name__}: {exc}")
            continue
        path = sp.path
        sp.path = prefix + sid
        store(con, sp, n_subagents=0, size=key[0], mtime=key[1], host=host)
        sp.path = path
        con.commit()
    if todo:
        con.execute("PRAGMA optimize")
    return len(todo)


def store(con, sp, n_subagents, size, mtime, host=None):
    cwd = sp.cwds.most_common(1)[0][0] if sp.cwds else None
    automated = int(len(sp.turns) == 0 or bool(sp.automated) or sp.entrypoint == "sdk-cli"
                    or any(marker in sp.path for marker in config.get().automated_markers))
    con.execute("DELETE FROM turns WHERE session_id=?", (sp.id,))
    con.execute("DELETE FROM evidence WHERE session_id=?", (sp.id,))
    con.execute("DELETE FROM links WHERE src=? AND kind IN ('fork','teleport')", (sp.id,))
    con.execute("""INSERT OR REPLACE INTO sessions(id, path, project, cwd, branch, entrypoint, started, ended, title,
                   ai_title, agent_name, n_prompts, n_turns, n_tool_calls, n_subagents, n_compactions, bytes, automated,
                   digest_hash, host, tool) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sp.id, sp.path, project_name(cwd), cwd, sp.branch, sp.entrypoint, sp.started, sp.ended, redact(sp.title),
                 redact(sp.ai_title), sp.agent_name, len(sp.turns), len(sp.turns), sp.meta["tool_calls"], n_subagents,
                 sp.meta["compactions"], size, automated, digest_hash(sp.turns), host, sp.tool))
    # Secrets are removed here, before anything is stored: see redact.py.
    con.executemany("""INSERT INTO turns(session_id, idx, ts, ended, uuid, prompt, reply, n_tools, files, queued)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    [(sp.id, t["idx"], t["ts"], t["ended"], t["uuid"], redact(t["prompt"]),
                      redact(_clip_reply(t["texts"])) if t["texts"] else "", t["n_tools"], json.dumps(t["files"]),
                      int(t.get("queued", False))) for t in sp.turns])
    con.executemany("INSERT INTO evidence(session_id, turn_idx, ts, kind, value, source) VALUES (?,?,?,?,?,?)",
                    [(sp.id, idx, ts, kind, redact(value), source) for idx, ts, kind, value, source in sp.evidence])
    for remote in sp.teleported:
        con.execute("INSERT OR REPLACE INTO links VALUES (?,?,?,?,?)", (sp.id, remote, "teleport", 1.0, "teleported-from"))
    con.execute("INSERT OR REPLACE INTO files(path, size, mtime, session_id, kind) VALUES (?,?,?,?,?)",
                (sp.path, size, mtime, sp.id, "session"))


def link_forks(con):
    """A resumed or forked session copies earlier turns. Keep each turn in the oldest session, link the copy."""
    con.execute("UPDATE turns SET dup_of=NULL")
    con.execute("DELETE FROM links WHERE kind='fork'")
    rows = con.execute("""SELECT t.uuid, t.session_id, s.started FROM turns t JOIN sessions s ON s.id=t.session_id
                          WHERE t.uuid IN (SELECT uuid FROM turns WHERE uuid IS NOT NULL GROUP BY uuid HAVING COUNT(*)>1)
                          ORDER BY t.uuid, s.started""").fetchall()
    shared = collections.Counter()
    owner = None
    for uuid, sid, _ in rows:
        if owner is None or owner[0] != uuid:
            owner = (uuid, sid)
            continue
        con.execute("UPDATE turns SET dup_of=? WHERE session_id=? AND uuid=?", (owner[1], sid, uuid))
        shared[(sid, owner[1])] += 1
    for (src, dst), n in shared.items():
        con.execute("INSERT OR REPLACE INTO links VALUES (?,?,?,?,?)", (src, dst, "fork", float(n), f"{n} shared turns"))
