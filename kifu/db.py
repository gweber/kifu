"""SQLite store: one file, rebuilt incrementally. Session files are only ever read."""
import os
import re
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
  path TEXT PRIMARY KEY, size INT, mtime REAL, session_id TEXT, kind TEXT);
CREATE INDEX IF NOT EXISTS files_kind_path ON files(kind, path);

-- One Claude Code session. digest_hash changes when its turns change.
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, path TEXT, project TEXT, cwd TEXT, branch TEXT, entrypoint TEXT,
  started TEXT, ended TEXT, title TEXT, ai_title TEXT, agent_name TEXT,
  n_prompts INT, n_turns INT, n_tool_calls INT, n_subagents INT, n_compactions INT,
  bytes INT, automated INT, digest_hash TEXT, host TEXT);
CREATE INDEX IF NOT EXISTS sessions_started ON sessions(started);

-- A prompt the user typed and what the assistant did until the next one.
-- ended: the last activity before a long silence; dup_of: the session a forked copy came from.
CREATE TABLE IF NOT EXISTS turns(
  session_id TEXT, idx INT, ts TEXT, ended TEXT, uuid TEXT, prompt TEXT, reply TEXT,
  n_tools INT, files TEXT, dup_of TEXT,
  PRIMARY KEY(session_id, idx));
CREATE INDEX IF NOT EXISTS turns_uuid ON turns(uuid);

-- Work that left the conversation: write, commit, push, pr, card, deploy, artifact; ask = a question dialog.
CREATE TABLE IF NOT EXISTS evidence(
  session_id TEXT, turn_idx INT, ts TEXT, kind TEXT, value TEXT, source TEXT);
CREATE INDEX IF NOT EXISTS evidence_session ON evidence(session_id);
CREATE INDEX IF NOT EXISTS evidence_kind ON evidence(kind, session_id, turn_idx);

CREATE TABLE IF NOT EXISTS links(
  src TEXT, dst TEXT, kind TEXT, weight REAL, detail TEXT,
  PRIMARY KEY(src, dst, kind));

-- A prompt and its short follow-ups, as the analyzer reads them.
CREATE TABLE IF NOT EXISTS moves(
  id INTEGER PRIMARY KEY, session_id TEXT, first_idx INT, last_idx INT, ts_first TEXT, ts_last TEXT,
  prompts TEXT, reply_tail TEXT, n_writes INT, n_commits INT, text_hash TEXT);
CREATE INDEX IF NOT EXISTS moves_session_idx ON moves(session_id, first_idx);
CREATE TABLE IF NOT EXISTS vectors(text_hash TEXT PRIMARY KEY, vec BLOB);

-- Ideas found in one session by the analyzer.
CREATE TABLE IF NOT EXISTS threads(
  id INTEGER PRIMARY KEY, session_id TEXT, title TEXT, summary TEXT, kind TEXT, status TEXT,
  first_turn INT, last_turn INT, first_ts TEXT, last_ts TEXT, quote TEXT, next_step TEXT,
  keywords TEXT, loose_ends TEXT, backend TEXT, digest_hash TEXT, line_id INT, embedding BLOB);
CREATE INDEX IF NOT EXISTS threads_session ON threads(session_id);
CREATE INDEX IF NOT EXISTS threads_line ON threads(line_id);

-- Sessions already read by the analyzer, including those that yielded no threads.
CREATE TABLE IF NOT EXISTS analyzed(
  session_id TEXT PRIMARY KEY, digest_hash TEXT, backend TEXT, n_threads INT);

-- Lines: the same idea followed across sessions. anchor = session_id:first_turn of its first thread.
CREATE TABLE IF NOT EXISTS lines(
  id INTEGER PRIMARY KEY, title TEXT, summary TEXT, project TEXT, status TEXT,
  first_ts TEXT, last_ts TEXT, n_sessions INT, score REAL, verdict TEXT,
  loose_ends TEXT, next_step TEXT, areas TEXT, input_hash TEXT, thread_ids TEXT, anchor TEXT);
CREATE INDEX IF NOT EXISTS lines_anchor ON lines(anchor);

-- The user's own verdict on an idea, kept across rebuilds of lines.
CREATE TABLE IF NOT EXISTS marks(
  anchor TEXT PRIMARY KEY, state TEXT, note TEXT, updated TEXT);
"""

# Superseded objects from before 1.0.
DROP = ["DROP TABLE IF EXISTS topics", "DROP INDEX IF EXISTS moves_session"]


def _migrate(con):
    """Bring a database created by an older version up to SCHEMA: add missing columns, drop dead objects."""
    for table, body in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)\((.*?)\);", SCHEMA, re.S):
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for col in body.replace("\n", " ").split(","):
            parts = col.split()
            if len(parts) >= 2 and not col.strip().startswith("PRIMARY KEY") and parts[0] not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {parts[0]} {parts[1]}")
    for stmt in DROP:
        con.execute(stmt)


def connect(path=None):
    path = path or config.get().db_path
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    # The web service, scheduled pulls and jobs write concurrently: wait for the lock instead of failing.
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA synchronous=NORMAL")
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if tables:
        # Old databases lack columns that SCHEMA's indexes need: create tables, migrate, then index.
        con.executescript("\n".join(re.findall(r"CREATE TABLE IF NOT EXISTS \w+\(.*?\);", SCHEMA, re.S)))
        _migrate(con)
    con.executescript(SCHEMA)
    return con


def prefix_range(prefix):
    """WHERE col >= ? AND col < ? matches a prefix and, unlike LIKE, uses the index."""
    return prefix, prefix + "￿"
