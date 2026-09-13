import datetime as dt
import json
import os
import sqlite3
import subprocess

import pytest

from kifu import analyze, db, embed, extract, habits, link, marks, report, verify
from kifu.embed import is_continuation


def one(con, sql, *params):
    return con.execute(sql, params).fetchone()[0]


# ---- scan --------------------------------------------------------------------------------------------------

def test_scan_reads_every_session_with_its_host_and_project(store):
    cfg, con = store
    assert one(con, "SELECT COUNT(*) FROM sessions") == len(__import__("kifu.demo").demo.SESSIONS)
    assert {r[0] for r in con.execute("SELECT DISTINCT host FROM sessions")} == {"laptop", "studio"}
    assert {r[0] for r in con.execute("SELECT DISTINCT project FROM sessions")} == {
        "tidepool", "sensor-hub", "inkwell", "chess-lab", "dotfiles"}


def test_evidence_counts_writes_commits_and_subagent_work(store):
    _, con = store
    kinds = dict(con.execute("SELECT kind, COUNT(*) FROM evidence GROUP BY kind").fetchall())
    assert kinds["commit"] == 18 and kinds["write"] >= 20
    assert one(con, "SELECT COUNT(*) FROM evidence WHERE source='subagent' AND turn_idx IS NULL") == 1


def test_question_dialogs_record_outcome_and_whether_the_recommendation_was_taken(store):
    _, con = store
    outcomes = [json.loads(v) for (v,) in con.execute("SELECT value FROM evidence WHERE kind='ask'")]
    assert sorted(o["outcome"] for o in outcomes) == ["answered", "answered", "dismissed"]
    picks = sorted(p["pick"] for o in outcomes for p in o.get("picks", []))
    assert picks == ["option", "recommended"]


def test_a_turn_ends_before_autonomous_work_resumes_hours_later(store):
    _, con = store
    started, ended = con.execute("SELECT ts, ended FROM turns WHERE prompt='yes run it'").fetchone()
    minutes = extract._minutes(started, ended)
    assert minutes < 60, "the overnight gauntlet must not count as one focused turn"


def test_project_names_are_the_same_on_every_machine():
    roots = ["~/code"]
    assert extract.project_name("/home/ada/code/tidepool", roots) == "tidepool"
    assert extract.project_name("/Users/ada/code/tidepool/web", roots) == "tidepool/web"
    assert extract.project_name("/home/ada", roots) == "~"
    assert extract.project_name("/srv/app", roots) == "/srv/app"


def test_rescan_skips_unchanged_files(store):
    cfg, con = store
    src = cfg.source_list()[0]
    root = os.path.expanduser(src.path)
    extract.scan(con, root, host=src.host, log=lambda m: None)       # the copied store has new paths
    assert extract.scan(con, root, host=src.host, log=lambda m: None) == 0


def test_a_forked_session_keeps_its_copied_turns_in_the_original(store):
    cfg, con = store
    src = cfg.source_list()[0]
    root = os.path.expanduser(src.path)
    original = next(p for p, _ in extract.discover(root).values())
    fork = os.path.join(os.path.dirname(original), "00000000-0000-4000-8000-00000000f0f0.jsonl")
    lines = open(original).read().splitlines()
    with open(fork, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    extract.scan(con, root, host=src.host, log=lambda m: None)
    assert one(con, "SELECT COUNT(*) FROM links WHERE kind='fork'") == 1
    assert one(con, "SELECT COUNT(*) FROM turns WHERE session_id=? AND dup_of IS NOT NULL",
               "00000000-0000-4000-8000-00000000f0f0") > 0


# ---- moves -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("prompt, expected", [
    ("go on", True), ("OK", True), ("a", True), ("2", True), ("sounds good, do it", True),
    ("fixed ip then", True), ("do we have seki?", False), ("results?", False),
    ("could it work offline on the beach? there is no signal down there", False),
])
def test_continuations(store, prompt, expected):
    assert is_continuation(prompt) is expected


def test_configured_confirmations_extend_the_defaults(store):
    cfg, _ = store
    cfg.confirmations = ["auf geht's"]
    embed._confirm_re = None
    assert is_continuation("auf geht's")


def test_moves_skip_claude_code_commands_and_fold_confirmations(store):
    _, con = store
    assert one(con, "SELECT COUNT(*) FROM moves WHERE prompts LIKE '/model%'") == 0
    folded = one(con, "SELECT prompts FROM moves WHERE prompts LIKE 'I want a small web app%'")
    assert "• go on" in folded


# ---- analyze -----------------------------------------------------------------------------------------------

def test_analysis_is_cached_per_session(store):
    cfg, con = store
    calls = []
    original = analyze.fixture

    def counting(*a):
        calls.append(1)
        return original(*a)

    analyze.fixture = counting
    analyze.analyze(lambda: db.connect(cfg.db_path), backend="fixture", workers=1, log=lambda m: None)
    assert calls == []
    con.execute("UPDATE sessions SET digest_hash='changed' WHERE id=(SELECT id FROM sessions LIMIT 1)")
    con.commit()
    analyze.analyze(lambda: db.connect(cfg.db_path), backend="fixture", workers=1, log=lambda m: None)
    assert len(calls) == 1


def test_prompts_name_the_configured_user(store):
    cfg, _ = store
    assert "between Ada and an AI coding assistant" in analyze.system_prompt()
    assert "Ada" in analyze.system_prompt(link.LINE_SYSTEM)


# ---- link --------------------------------------------------------------------------------------------------

def test_an_idea_is_followed_across_sessions(store):
    _, con = store
    row = con.execute("SELECT n_sessions, status, loose_ends FROM lines WHERE title='Offline mode for tidepool'").fetchone()
    assert row["n_sessions"] == 2 and row["status"] == "started"
    assert json.loads(row["loose_ends"]) == ["Banner when cached predictions are older than a week"]


def test_only_open_ideas_score(store):
    _, con = store
    assert one(con, "SELECT COUNT(*) FROM lines WHERE score > 0 AND status IN ('shipped','dropped','answered')") == 0
    top = con.execute("SELECT title FROM lines ORDER BY score DESC LIMIT 1").fetchone()[0]
    assert top == "Weekend low-tide push alerts"


def test_marks_survive_a_relink(store):
    cfg, con = store
    anchor = one(con, "SELECT anchor FROM lines WHERE title='Weekend low-tide push alerts'")
    marks.set_mark(con, anchor, "dismissed")
    con.execute("UPDATE lines SET verdict='x'")      # anything; the rebuild replaces lines
    link.build_lines(con, backend="fixture", workers=1, log=lambda m: None)
    data = report.collect(con, habits_data={})
    line = next(l for l in data["lines"] if l["title"] == "Weekend low-tide push alerts")
    assert line["mark"]["state"] == "dismissed"


# ---- habits ------------------------------------------------------------------------------------------------

def test_habits_summary(store):
    _, con = store
    h = habits.compute(con)
    s = h["summary"]
    assert s["questions_asked"] > 0
    assert s["recommendation_offered"] == 2 and s["recommendation_taken"] == 1
    assert s["dialogs_dismissed_pct"] == 33
    assert h["timezone"] == "Europe/Lisbon"
    assert sum(sum(row) for row in h["rhythm"]["grid"]) == h["n_prompts"]


# ---- api ---------------------------------------------------------------------------------------------------

@pytest.fixture
def client(store):
    from fastapi.testclient import TestClient

    from kifu import api
    return TestClient(api.app, base_url="http://127.0.0.1:8765")


def test_api_reads(client):
    assert client.get("/").status_code == 200
    assert client.get("/api/health").json()["ok"] is True
    overview = client.get("/api/overview").json()
    assert overview["config"]["user"] == "Ada" and overview["top"][0]["title"] == "Weekend low-tide push alerts"
    compact = client.get("/api/lines", params={"open": "true", "compact": "true"}).json()
    assert compact["total"] == 5 and set(compact["items"][0]) >= {"anchor", "loose_ends", "resume", "quiet_days"}
    assert client.get("/api/lines", params={"q": "offline"}).json()["total"] == 1
    assert "quiet for" in client.get("/api/digest", params={"quiet_days": 0}).json()["text"]
    sid = compact["items"][0]["resume"].split()[-1]
    detail = client.get(f"/api/sessions/{sid[:8]}").json()
    assert detail["threads"] and detail["turns"]
    assert client.get("/api/sessions/zzzz").status_code == 404
    assert client.get("/api/lines/nope").status_code == 404


def test_api_mark_roundtrip(client):
    anchor = client.get("/api/lines", params={"open": "true"}).json()["items"][0]["anchor"]
    assert client.put(f"/api/lines/{anchor}/mark", json={"state": "done"}).json()["mark"]["state"] == "done"
    assert client.get("/api/stats").json()["marked"] == 1
    assert client.get("/api/lines", params={"open": "true"}).json()["total"] == 4
    assert client.put(f"/api/lines/{anchor}/mark", json={"state": "open"}).json()["mark"] is None
    assert client.put(f"/api/lines/{anchor}/mark", json={"state": "bogus"}).status_code == 422


# ---- db ----------------------------------------------------------------------------------------------------

def test_an_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE sessions(id TEXT PRIMARY KEY, path TEXT, project TEXT);
        CREATE TABLE turns(session_id TEXT, idx INT, ts TEXT, prompt TEXT, PRIMARY KEY(session_id, idx));
        CREATE TABLE lines(id INTEGER PRIMARY KEY, title TEXT);
        CREATE TABLE topics(id INTEGER PRIMARY KEY);
        INSERT INTO sessions VALUES ('s1', '/x', 'p');""")
    old.commit()
    old.close()
    con = db.connect(str(path))
    assert {"host", "digest_hash"} <= {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
    assert "anchor" in {r[1] for r in con.execute("PRAGMA table_info(lines)")}
    assert one(con, "SELECT COUNT(*) FROM sqlite_master WHERE name='topics'") == 0
    assert one(con, "SELECT project FROM sessions WHERE id='s1'") == "p"


def test_the_api_notices_a_relink_that_keeps_the_same_ids(client, store):
    _, con = store
    before = client.get("/api/lines", params={"q": "offline"}).json()["items"][0]["summary"]
    con.execute("UPDATE lines SET summary='rewritten by a relink' WHERE title='Offline mode for tidepool'")
    con.commit()
    after = client.get("/api/lines", params={"q": "offline"}).json()["items"][0]["summary"]
    assert before != after == "rewritten by a relink"


def test_dns_rebinding_and_cross_site_writes_are_refused(client, store):
    cfg, _ = store
    assert client.get("/api/stats", headers={"host": "evil.example"}).status_code == 421
    assert client.get("/api/stats", headers={"host": "localhost:8765"}).status_code == 200
    assert client.get("/api/stats", headers={"host": "[::1]:8765"}).status_code == 200
    anchor = client.get("/api/lines", params={"open": "true"}).json()["items"][0]["anchor"]
    body = {"state": "done"}
    assert client.put(f"/api/lines/{anchor}/mark", json=body, headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/jobs", json={"kind": "pull"}, headers={"origin": "https://evil.example"}).status_code == 403
    assert client.put(f"/api/lines/{anchor}/mark", json={"state": "open"},
                      headers={"origin": "http://127.0.0.1:8765"}).status_code == 200
    cfg.allowed_hosts = ["kifu.example.org"]
    assert client.get("/api/stats", headers={"host": "kifu.example.org"}).status_code == 200


def test_a_mark_follows_its_idea_when_reanalysis_moves_the_anchor(store):
    _, con = store
    old_anchor = one(con, "SELECT anchor FROM lines WHERE title='Offline mode for tidepool'")
    marks.set_mark(con, old_anchor, "done")
    # Re-analysing the first session finds the idea one turn later: its anchor changes.
    sid, turn = old_anchor.split(":")[:2]
    con.execute("UPDATE threads SET first_turn=first_turn+1 WHERE session_id=? AND first_turn=?", (sid, int(turn)))
    con.commit()
    link.build_lines(con, backend="fixture", workers=1, log=lambda m: None)
    new_anchor = one(con, "SELECT anchor FROM lines WHERE title='Offline mode for tidepool'")
    assert new_anchor != old_anchor
    assert one(con, "SELECT state FROM marks WHERE anchor=?", new_anchor) == "done"
    assert one(con, "SELECT COUNT(*) FROM marks") == 1


def test_a_mark_without_a_matching_idea_stays_unattached(store):
    _, con = store
    anchor = one(con, "SELECT anchor FROM lines WHERE title='Offline mode for tidepool'")
    marks.set_mark(con, anchor, "dismissed")
    con.execute("UPDATE marks SET anchor='gone:0', sessions='[]'")      # its sessions no longer hold the idea
    con.commit()
    assert marks.reattach(con, log=lambda m: None) == (0, 1)
    data = report.collect(con, habits_data={})
    assert not any(l["mark"] for l in data["lines"]), "an orphaned mark must not land on some other idea"


# ---- verify ------------------------------------------------------------------------------------------------

def _git(repo, *args, date=None):
    env = {**os.environ, "GIT_AUTHOR_NAME": "Ada", "GIT_AUTHOR_EMAIL": "ada@example.org",
           "GIT_COMMITTER_NAME": "Ada", "GIT_COMMITTER_EMAIL": "ada@example.org"}
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def test_verify_finds_later_commits_that_settle_an_idea(store, tmp_path):
    cfg, con = store
    line = con.execute("SELECT * FROM lines WHERE title='Weekend low-tide push alerts'").fetchone()
    thread = con.execute("SELECT * FROM threads WHERE line_id=?", (line["id"],)).fetchone()
    repo = tmp_path / "tidepool"
    (repo / "jobs").mkdir(parents=True)
    _git(repo, "init", "-q")
    alerts = repo / "jobs" / "alerts.py"
    alerts.write_text("# first draft\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "draft alerts, during the session", date=line["last_ts"])
    # The idea's session wrote this file; move the session to the temporary repository.
    con.execute("UPDATE sessions SET cwd=? WHERE id=?", (str(repo), thread["session_id"]))
    con.execute("INSERT INTO evidence VALUES (?,?,?,?,?,?)",
                (thread["session_id"], thread["first_turn"], line["last_ts"], "write", str(alerts), "main"))
    con.commit()
    later = (dt.datetime.fromisoformat(line["last_ts"].replace("Z", "+00:00")) + dt.timedelta(days=9)).isoformat()
    for subject in ("add daily job that checks the next 7 days", "web push subscription for alerts"):
        alerts.write_text(alerts.read_text() + subject + "\n")
        _git(repo, "commit", "-qam", subject, date=later)

    counts = verify.verify(lambda: db.connect(cfg.db_path), backend="fixture", workers=1, log=lambda m: None)
    assert counts["with_commits"] == 1 and counts["likely_done"] == 1
    data = report.collect(con, habits_data={})
    idea = next(l for l in data["lines"] if l["title"] == "Weekend low-tide push alerts")
    check = idea["check"]
    assert check["commits_since"] == 2, "the commit made during the session does not count"
    assert check["likely_done"] and len(check["settled"]) == 2
    assert idea["score"] == round(line["score"] * verify.LIKELY_DONE_FACTOR, 2)
    assert data["lines"][0]["title"] != "Weekend low-tide push alerts", "a likely-done idea drops down the ranking"
    assert report.compact(idea, data)["activity"]["latest_commit"]["subject"].startswith("web push")


def test_verify_says_so_when_an_idea_wrote_no_files(store):
    cfg, con = store
    verify.verify(lambda: db.connect(cfg.db_path), backend="fixture", workers=1, log=lambda m: None)
    errors = [c["error"] for c in con.execute("SELECT error FROM checks")]
    assert errors and all(e for e in errors), "demo sessions live in /home/ada, which is no repository here"


def test_two_ideas_from_the_same_move_get_different_anchors(store):
    cfg, con = store
    first = con.execute("SELECT * FROM threads ORDER BY id LIMIT 1").fetchone()
    con.execute("""INSERT INTO threads(session_id, title, summary, kind, status, first_turn, last_turn, first_ts, last_ts,
                   quote, next_step, keywords, loose_ends, backend, digest_hash)
                   SELECT session_id, 'A second idea in the same move', 'x', 'idea', 'proposed', first_turn, last_turn,
                   first_ts, last_ts, quote, '', '["zzz"]', '["something"]', backend, digest_hash FROM threads WHERE id=?""",
                (first["id"],))
    con.commit()
    link.build_lines(con, backend="fixture", workers=1, log=lambda m: None)
    assert one(con, "SELECT COUNT(*) FROM lines") == one(con, "SELECT COUNT(DISTINCT anchor) FROM lines")
