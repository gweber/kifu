"""`kifu memory-check`: memory and CLAUDE.md files that point at things which are gone.

Memory outlives the code it describes. A note that names a file which moved sends the next session looking in the
wrong place, and a project's memory folder is only loaded while the project's folder exists. This reads:

  - <claude dir>/CLAUDE.md, CLAUDE.md in every project root, and <claude dir>/projects/*/memory/*.md
  - checks every absolute or ~ path they name. Paths of other machines are skipped: a line or memory that names
    another host (a source or an ~/.ssh/config Host) is about that host, and a path outside the home folder is only
    reported when its parent folder exists here.
  - checks each MEMORY.md index: entries whose file is missing, memory files the index does not list
  - finds memory folders whose project folder no longer exists (Claude Code will never load them again), and
    suggests where the project went: the folder whose later sessions wrote the same files

Moved paths are suggested from `moved_paths` in the config. Nothing is changed.
"""
import glob
import json
import os
import re
import socket

from . import config
from .claude_code import claude_dir
from .extract import moved

# /abs/path or ~/path, not part of a URL (://host/...) and not a relative path's tail.
PATH = re.compile(r"(?<![\w:/.~-])(~/|/)(?:[\w.@+-]+/)*[\w.@+-]+/?")
PLACEHOLDER = re.compile(r"[<>$*{}…]|/path/to/|/your/|/foo|/bar\b|/x\b|/example|\b(?:DATE|YYYY|NAME|ID)\b|[-_]$")
SKIP_PREFIXES = ("/tmp", "/dev", "/proc", "/sys", "/run", "/private", "/var/folders")
INDEX_LINK = re.compile(r"\]\(([^)\s]+\.md)\)")


def _expand(path):
    return os.path.expanduser(path.rstrip("/")) if path.startswith("~") else path.rstrip("/")


def other_hosts():
    """Names of machines other than this one: kifu's sources and the Host entries of ~/.ssh/config."""
    cfg = config.get()
    names = {s.host for s in cfg.source_list() if s.ssh} | {s.ssh for s in cfg.source_list() if s.ssh}
    names |= set(cfg.other_hosts)
    try:
        for line in open(os.path.expanduser("~/.ssh/config"), errors="replace"):
            if line.strip().lower().startswith("host "):
                names |= {n for n in line.split()[1:] if not any(c in n for c in "*?!")}
    except OSError:
        pass
    here = {socket.gethostname().split(".")[0].lower()} | {s.host.lower() for s in cfg.source_list() if not s.ssh}
    return {n.lower() for n in names if n} - here


def mentions(text, hosts):
    return any(re.search(rf"(?<![a-z0-9]){re.escape(h)}(?![a-z0-9])", text, re.I) for h in hosts)


def _checkable(path):
    """A path of this machine: its first component exists here (/Users/... on Linux is another machine's)."""
    if PLACEHOLDER.search(path) or path in ("/", "~/"):
        return False
    full = _expand(path)
    if any(full.startswith(os.path.expanduser(r).rstrip("/") + "/") for r in config.get().project_roots):
        return True
    if path.startswith(SKIP_PREFIXES):
        return False
    parts = full.split("/")
    if len(parts) <= 2 or not os.path.isdir("/" + parts[1]):
        return False
    home = os.path.expanduser("~")
    return full.startswith(home + "/") or os.path.isdir(os.path.dirname(full))


def path_findings(file, text, hosts=frozenset()):
    out = []
    seen = set()
    lines = text.splitlines()
    if hosts and mentions(os.path.basename(file) + "\n" + "\n".join(lines[:6]), hosts):
        return out                  # the whole memory is about another machine
    for lineno, line in enumerate(lines, 1):
        if hosts and mentions(line, hosts):
            continue
        for m in PATH.finditer(line):
            raw = m.group(0).rstrip(".,:;)")
            if raw in seen or len(raw) < 4 or not _checkable(raw):
                continue
            seen.add(raw)
            full = _expand(raw)
            if os.path.exists(full):
                continue
            finding = {"file": file, "line": lineno, "kind": "missing-path", "path": raw}
            new = moved(full)
            if new != full:
                finding["moved_to"] = new
                finding["moved_exists"] = os.path.exists(new)
            else:
                parent = os.path.dirname(full)
                while parent not in ("", "/") and not os.path.exists(parent):
                    parent = os.path.dirname(parent)
                finding["nearest_existing"] = parent
            out.append(finding)
    return out


def index_findings(memory_dir):
    index = os.path.join(memory_dir, "MEMORY.md")
    files = {os.path.basename(p) for p in glob.glob(os.path.join(memory_dir, "*.md"))} - {"MEMORY.md"}
    if not os.path.exists(index):
        return [{"file": memory_dir, "kind": "no-index", "unlisted": sorted(files)}] if files else []
    listed = set()
    out = []
    for lineno, line in enumerate(open(index, errors="replace").read().splitlines(), 1):
        for target in INDEX_LINK.findall(line):
            listed.add(os.path.basename(target))
            if not os.path.exists(os.path.join(memory_dir, target)):
                out.append({"file": index, "line": lineno, "kind": "index-missing-file", "path": target})
    for name in sorted(files - listed):
        out.append({"file": os.path.join(memory_dir, name), "kind": "not-in-index"})
    return out


def _project_cwd(project_dir, con=None):
    """The working directory a Claude Code project folder belongs to: from a session in it, or kifu's store."""
    for path in sorted(glob.glob(os.path.join(project_dir, "*.jsonl")), key=os.path.getmtime, reverse=True)[:3]:
        with open(path, errors="replace") as fh:
            for raw in fh:
                if '"cwd"' in raw:
                    try:
                        cwd = json.loads(raw).get("cwd")
                    except ValueError:
                        continue
                    if cwd:
                        return cwd
    if con is not None:
        slug = os.path.basename(project_dir)
        row = con.execute("SELECT cwd FROM sessions WHERE path LIKE ? AND cwd IS NOT NULL ORDER BY ended DESC LIMIT 1",
                          (f"%/{slug}/%",)).fetchone()
        if row:
            return row["cwd"]
    return None


def project_root(path):
    """The project folder a path belongs to: <project root>/<name>, or the path's folder outside project roots."""
    for base in config.get().project_roots:
        base = os.path.expanduser(base).rstrip("/") + "/"
        if path.startswith(base) and "/" in path[len(base):]:
            return base + path[len(base):].split("/")[0]
    return os.path.dirname(path)


def went_to(con, cwd):
    """Where an orphaned project's files are written now: the existing folder whose later sessions wrote the most
    files with the same names (last two path parts) as files once written under cwd."""
    if con is None:
        return None
    prefix = cwd.rstrip("/") + "/"
    rows = con.execute("SELECT value, MAX(ts) ts FROM evidence WHERE kind='write' AND value >= ? AND value < ? "
                       "GROUP BY value", (prefix, prefix[:-1] + "0")).fetchall()
    tails = {"/".join(r["value"].split("/")[-2:]) for r in rows}
    if not rows or not tails:
        return None
    last = max(r["ts"] or "" for r in rows)
    votes = {}
    for r in con.execute("SELECT DISTINCT value FROM evidence WHERE kind='write' AND ts > ? AND value NOT LIKE ?",
                         (last, prefix + "%")):
        path = r["value"]
        tail = "/".join(path.split("/")[-2:])
        if tail in tails and os.path.exists(path):
            votes.setdefault(project_root(path), set()).add(tail)
    if not votes:
        return None
    root, found = max(votes.items(), key=lambda kv: len(kv[1]))
    return {"path": root, "files": len(found)} if len(found) >= 3 else None


def check(con=None, root=None):
    root = root or claude_dir()
    hosts = other_hosts()
    out = []
    docs = [os.path.join(root, "CLAUDE.md")]
    for project_root in config.get().project_roots:
        base = os.path.expanduser(project_root)
        docs += glob.glob(os.path.join(base, "*", "CLAUDE.md")) + glob.glob(os.path.join(base, "*", ".claude", "CLAUDE.md"))
    for memory_dir in sorted(glob.glob(os.path.join(root, "projects", "*", "memory"))):
        docs += sorted(glob.glob(os.path.join(memory_dir, "*.md")))
        out += index_findings(memory_dir)
        cwd = _project_cwd(os.path.dirname(memory_dir), con)
        if cwd and _checkable(cwd) and not os.path.isdir(cwd):
            finding = {"file": memory_dir, "kind": "orphaned-memory", "path": cwd,
                       "memories": len(glob.glob(os.path.join(memory_dir, "*.md")))}
            if moved(cwd) != cwd:
                finding["moved_to"] = moved(cwd)
            elif went := went_to(con, cwd):
                finding["went_to"] = went
            out.append(finding)
    for doc in dict.fromkeys(docs):
        if os.path.isfile(doc):
            out += path_findings(doc, open(doc, errors="replace").read(), hosts)
    return out


def describe(f):
    where = f"{f['file']}:{f['line']}" if f.get("line") else f["file"]
    kind = f["kind"]
    if kind == "missing-path":
        hint = (f" → moved to {f['moved_to']}" + ("" if f["moved_exists"] else " (also missing)") if "moved_to" in f
                else f" (nearest existing: {f['nearest_existing']})")
        return f"{where}: {f['path']} does not exist{hint}"
    if kind == "orphaned-memory":
        hint = (f"; the project moved to {f['moved_to']}: rename the folder" if "moved_to" in f
                else f"; its files are written in {f['went_to']['path']} now ({f['went_to']['files']} of the same "
                     f"files): move the memories that still apply" if "went_to" in f else "")
        return f"{where}: {f['memories']} memory file(s) for {f['path']}, which no longer exists{hint}"
    if kind == "index-missing-file":
        return f"{where}: the index lists {f['path']}, which does not exist"
    if kind == "not-in-index":
        return f"{where}: not listed in MEMORY.md, so it is never loaded"
    if kind == "no-index":
        return f"{where}: {len(f['unlisted'])} memory file(s) but no MEMORY.md"
    return f"{where}: {kind}"
