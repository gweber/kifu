"""How the user works with Claude: rhythm, modes, focus, juggling, unanswered questions, idea lifetimes.

Deterministic, on top of what scan, embed and analyze stored. Times are in the configured timezone.
"""
import collections
import datetime as dt
import json
import re
import statistics

import numpy as np

from . import config
from .embed import BUILTIN_COMMAND, is_continuation

PAUSE_MIN = 45             # a gap this long ends a focus stretch without counting as a switch
JUGGLE_GAP_MIN = 30        # prompts this close in two different sessions count as juggling
SWITCH_SIM = 0.52          # embedding fallback for turns no thread covers; below this is a new topic
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def local(ts):
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(config.get().tz)


def week_of(t):
    monday = (t - dt.timedelta(days=t.weekday())).date()
    return monday.isoformat()


def mode_of(prompt):
    """drive: keep going. ask: a question. steer: a short direction. deep: a long, thought-out prompt."""
    p = prompt.strip()
    if is_continuation(p):
        return "drive"
    if len(p) >= 280:
        return "deep"
    if "?" in p:
        return "ask"
    return "steer"


def ends_with_question(reply):
    tail = (reply or "").strip()[-300:]
    last = re.split(r"(?<=[.!?])\s+|\n+", tail.strip())
    last = [s for s in last if s.strip()]
    return bool(last) and last[-1].rstrip("*_` )").endswith("?")


def last_sentence(reply):
    parts = [s for s in re.split(r"(?<=[.!?])\s+|\n+", (reply or "").strip()[-400:]) if s.strip()]
    return parts[-1].strip("*_ ")[:220] if parts else ""


def compute(con):
    sessions = {s["id"]: s for s in con.execute("SELECT * FROM sessions WHERE automated=0")}
    turns = [t for t in con.execute("""SELECT t.session_id, t.idx, t.ts, t.ended, t.prompt, t.reply FROM turns t
                                       WHERE t.dup_of IS NULL ORDER BY t.ts""")
             if t["session_id"] in sessions and not BUILTIN_COMMAND.match(t["prompt"])]
    threads = collections.defaultdict(list)
    for th in con.execute("SELECT id, session_id, first_turn, last_turn, kind, status, title FROM threads"):
        threads[th["session_id"]].append(th)
    vectors = _move_vectors(con)

    now = dt.datetime.now(config.get().tz).astimezone(config.get().tz)
    out = {"generated": now.isoformat(timespec="minutes"), "timezone": config.get().timezone or str(now.tzinfo),
           "n_prompts": len(turns)}
    out["rhythm"] = rhythm(turns)
    out["weeks"] = weeks(con, turns, sessions)
    by_session = collections.defaultdict(list)
    for t in turns:
        by_session[t["session_id"]].append(t)
    out["focus"] = focus(by_session, threads, vectors)
    out["latency"] = latency(by_session)
    out["juggling"] = juggling(turns)
    out["questions"] = questions(con, by_session, threads, vectors)
    out["ideas"] = idea_lifetimes(con)
    out["summary"] = summary(out)
    return out


def summary(h, recent=4):
    """The headline numbers, recent weeks against the first weeks. Every UI shows these same values."""
    def median(xs):
        xs = sorted(x for x in xs if x is not None)
        return xs[len(xs) // 2] if xs else None

    def pct(a, b):
        return round(100 * a / b) if b else 0

    weeks = h["weeks"]

    def share(ws, mode):
        return pct(sum(w["modes"].get(mode, 0) for w in ws), sum(w["prompts"] for w in ws))

    fw, lw = h["focus"]["by_week"], h["latency"]["by_week"]
    outcomes = h["questions"]["outcomes"]
    asked = sum(outcomes.values())
    left = outcomes.get("switched topic", 0) + outcomes.get("session ended", 0)
    with_rec = [p for p in h["questions"]["ask_picks"] if p["context"] == "with recommendation"]
    rec_total = sum(p["n"] for p in with_rec)
    rec_taken = sum(p["n"] for p in with_rec if p["pick"] == "recommended")
    dialogs = h["questions"]["ask_tool"]
    return {
        "focus_minutes": {"recent": median([w["median_minutes"] for w in fw[-recent:]]),
                          "first": median([w["median_minutes"] for w in fw[:recent]])},
        "reply_minutes": {"recent": median([w["median_minutes"] for w in lw[-recent:]]),
                          "first": median([w["median_minutes"] for w in lw[:recent]])},
        "go_on_pct": {"recent": share(weeks[-recent:], "drive"), "first": share(weeks[:recent], "drive")},
        "questions_left_pct": pct(left, asked), "questions_left": left, "questions_asked": asked,
        "recommendation_taken_pct": pct(rec_taken, rec_total), "recommendation_taken": rec_taken,
        "recommendation_offered": rec_total,
        "dialogs_dismissed_pct": pct(dialogs.get("dismissed", 0), sum(dialogs.values())),
    }


def rhythm(turns):
    grid = [[0] * 24 for _ in range(7)]
    by_hour = collections.defaultdict(collections.Counter)
    lengths = collections.defaultdict(list)
    for t in turns:
        lt = local(t["ts"])
        grid[lt.weekday()][lt.hour] += 1
        by_hour[lt.hour][mode_of(t["prompt"])] += 1
        lengths[lt.hour].append(len(t["prompt"]))
    hours = []
    for h in range(24):
        n = sum(by_hour[h].values())
        hours.append({"hour": h, "n": n, "modes": dict(by_hour[h]),
                      "median_chars": int(statistics.median(lengths[h])) if lengths[h] else 0})
    return {"grid": grid, "weekdays": WEEKDAYS, "hours": hours}


def weeks(con, turns, sessions):
    w = collections.defaultdict(lambda: {"prompts": 0, "modes": collections.Counter(), "sessions": set(), "days": set()})
    for t in turns:
        lt = local(t["ts"])
        row = w[week_of(lt)]
        row["prompts"] += 1
        row["modes"][mode_of(t["prompt"])] += 1
        row["sessions"].add(t["session_id"])
        row["days"].add(lt.date())
    ideas = collections.defaultdict(collections.Counter)
    for th in con.execute("SELECT first_ts, status, kind FROM threads WHERE kind NOT IN ('chore','question')"):
        ideas[week_of(local(th["first_ts"]))][th["status"]] += 1
    result = []
    for key in sorted(w):
        row = w[key]
        result.append({"week": key, "prompts": row["prompts"], "modes": dict(row["modes"]),
                       "sessions": len(row["sessions"]), "active_days": len(row["days"]),
                       "new_ideas": sum(ideas[key].values()), "idea_status": dict(ideas[key])})
    return result


def _move_vectors(con):
    """(session_id, turn idx) -> normalized vector of the move containing that turn."""
    out = {}
    for m in con.execute("SELECT m.session_id, m.first_idx, m.last_idx, v.vec FROM moves m JOIN vectors v ON v.text_hash=m.text_hash"):
        v = np.frombuffer(m["vec"], dtype=np.float32)
        v = v / np.linalg.norm(v)
        for i in range(m["first_idx"], m["last_idx"] + 1):
            out[(m["session_id"], i)] = v
    return out


def thread_of(session_threads, idx):
    """The narrowest thread whose turn range covers idx."""
    best = None
    for th in session_threads:
        if th["first_turn"] <= idx <= th["last_turn"]:
            if best is None or th["last_turn"] - th["first_turn"] < best["last_turn"] - best["first_turn"]:
                best = th
    return best


def _same_topic(sid, a, b, session_threads, vectors):
    if is_continuation(b["prompt"]):
        return True
    ta, tb = thread_of(session_threads, a["idx"]), thread_of(session_threads, b["idx"])
    if ta is not None and tb is not None:
        return ta["id"] == tb["id"]
    va, vb = vectors.get((sid, a["idx"])), vectors.get((sid, b["idx"]))
    if va is None or vb is None:
        return True
    return float(va @ vb) >= SWITCH_SIM


def focus(by_session, threads, vectors):
    """Stretches of prompts on one topic. A stretch ends by a switch, a pause, or the end of the session."""
    stretches = []
    for sid, ts_ in by_session.items():
        start = ts_[0]
        prev = ts_[0]
        count = 1
        for t in ts_[1:]:
            gap = (local(t["ts"]) - local(prev["ended"] or prev["ts"])).total_seconds() / 60
            if gap > PAUSE_MIN:
                end = "pause"
            elif not _same_topic(sid, prev, t, threads[sid], vectors):
                end = "switch"
            else:
                prev, count = t, count + 1
                continue
            stretches.append(_stretch(start, prev, count, end))
            start, prev, count = t, t, 1
        stretches.append(_stretch(start, prev, count, "session end"))
    real = [s for s in stretches if s["prompts"] >= 2]
    by_hour = collections.defaultdict(list)
    by_week = collections.defaultdict(list)
    for s in real:
        by_hour[s["hour"]].append(s["minutes"])
        by_week[s["week"]].append(s["minutes"])
    med = lambda xs: round(statistics.median(xs), 1) if xs else 0
    deep = sorted(real, key=lambda s: -s["minutes"])[:12]
    return {
        "n": len(stretches),
        "median_minutes": med([s["minutes"] for s in real]),
        "median_prompts": med([s["prompts"] for s in real]),
        "ends": dict(collections.Counter(s["end"] for s in stretches)),
        "distribution": _histogram([s["minutes"] for s in real], [0, 5, 10, 20, 30, 45, 60, 90, 120, 180, 10_000]),
        "by_hour": [{"hour": h, "median_minutes": med(by_hour[h]), "n": len(by_hour[h])} for h in range(24)],
        "by_week": [{"week": w, "median_minutes": med(v), "n": len(v)} for w, v in sorted(by_week.items())],
        "longest": [{**s, "session": s["session"]} for s in deep],
    }


def _stretch(first, last, count, end):
    a, b = local(first["ts"]), local(last["ended"] or last["ts"])
    return {"session": first["session_id"], "start": a.isoformat(timespec="minutes"), "hour": a.hour,
            "week": week_of(a), "minutes": round((b - a).total_seconds() / 60, 1), "prompts": count, "end": end,
            "prompt": first["prompt"][:160]}


def _histogram(values, edges):
    counts = [0] * (len(edges) - 1)
    for v in values:
        for i in range(len(edges) - 1):
            if edges[i] <= v < edges[i + 1]:
                counts[i] += 1
                break
    labels = [f"{edges[i]}–{edges[i + 1]}" if edges[i + 1] < 10_000 else f"{edges[i]}+" for i in range(len(edges) - 1)]
    return [{"bucket": l, "n": c} for l, c in zip(labels, counts)]


def latency(by_session):
    """Minutes from the end of a reply to the user's next prompt in the same session, pauses excluded."""
    by_mode = collections.defaultdict(list)
    by_hour = collections.defaultdict(list)
    by_week = collections.defaultdict(list)
    for ts_ in by_session.values():
        for a, b in zip(ts_, ts_[1:]):
            minutes = (local(b["ts"]) - local(a["ended"] or a["ts"])).total_seconds() / 60
            if not 0 <= minutes <= PAUSE_MIN:
                continue
            lt = local(b["ts"])
            by_mode[mode_of(b["prompt"])].append(minutes)
            by_hour[lt.hour].append(minutes)
            by_week[week_of(lt)].append(minutes)
    med = lambda xs: round(statistics.median(xs), 2) if xs else None
    return {"by_mode": {m: {"median_minutes": med(v), "n": len(v)} for m, v in by_mode.items()},
            "by_hour": [{"hour": h, "median_minutes": med(by_hour[h]), "n": len(by_hour[h])} for h in range(24)],
            "by_week": [{"week": w, "median_minutes": med(v), "n": len(v)} for w, v in sorted(by_week.items())]}


def juggling(turns):
    """Switching between sessions: consecutive prompts in different sessions within a short gap."""
    per_day = collections.defaultdict(lambda: {"switches": 0, "sessions": set(), "prompts": 0})
    for a, b in zip(turns, turns[1:]):
        day = local(b["ts"]).date().isoformat()
        per_day[day]["prompts"] += 1
        per_day[day]["sessions"].add(b["session_id"])
        gap = (local(b["ts"]) - local(a["ts"])).total_seconds() / 60
        if a["session_id"] != b["session_id"] and gap <= JUGGLE_GAP_MIN:
            per_day[day]["switches"] += 1
    days = [{"day": d, "switches": v["switches"], "sessions": len(v["sessions"]), "prompts": v["prompts"]}
            for d, v in sorted(per_day.items())]
    return {"days": days, "busiest": sorted(days, key=lambda d: -d["switches"])[:8]}


def questions(con, by_session, threads, vectors):
    """What happened after a reply that ended with a question."""
    outcomes = collections.Counter()
    per_week = collections.defaultdict(collections.Counter)
    left = []
    for sid, ts_ in by_session.items():
        for i, t in enumerate(ts_):
            if not ends_with_question(t["reply"]):
                continue
            nxt = ts_[i + 1] if i + 1 < len(ts_) else None
            if nxt is None:
                outcome = "session ended"
            else:
                gap = (local(nxt["ts"]) - local(t["ts"])).total_seconds() / 3600
                if is_continuation(nxt["prompt"]):
                    outcome = "go on"
                elif _same_topic(sid, t, nxt, threads[sid], vectors):
                    outcome = "answered" if gap < 12 else "answered later"
                else:
                    outcome = "switched topic"
            outcomes[outcome] += 1
            per_week[week_of(local(t["ts"]))][outcome] += 1
            if outcome in ("switched topic", "session ended"):
                left.append({"session": sid, "ts": t["ts"], "question": last_sentence(t["reply"]), "outcome": outcome,
                             "next": nxt["prompt"][:140] if nxt else ""})
    asks = collections.Counter()
    picks = collections.Counter()
    for (value,) in con.execute("SELECT value FROM evidence WHERE kind='ask'"):
        d = json.loads(value)
        asks[d["outcome"]] += 1
        for p in d.get("picks", []):
            picks[("with recommendation" if p["had_rec"] else "no recommendation", p["pick"])] += 1
    left.sort(key=lambda x: x["ts"], reverse=True)
    return {
        "outcomes": dict(outcomes),
        "by_week": [{"week": w, **dict(c)} for w, c in sorted(per_week.items())],
        "left_behind": left[:60],
        "ask_tool": dict(asks),
        "ask_picks": [{"context": k[0], "pick": k[1], "n": n} for k, n in sorted(picks.items())],
    }


def idea_lifetimes(con):
    rows = con.execute("""SELECT l.status, l.first_ts, l.last_ts, l.n_sessions FROM lines l""").fetchall()
    if not rows:
        rows = con.execute("SELECT status, first_ts, last_ts, 1 AS n_sessions FROM threads WHERE kind NOT IN ('chore','question')").fetchall()
    by_status = collections.defaultdict(list)
    for r in rows:
        days = (local(r["last_ts"]) - local(r["first_ts"])).total_seconds() / 86400
        by_status[r["status"]].append(days)
    return {
        "n": len(rows),
        "multi_session": sum(1 for r in rows if r["n_sessions"] > 1),
        "by_status": [{"status": s, "n": len(v), "median_days": round(statistics.median(v), 1),
                       "same_day": sum(1 for d in v if d < 1)} for s, v in sorted(by_status.items(), key=lambda x: -len(x[1]))],
    }
