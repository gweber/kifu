"""A synthetic data set: made-up sessions in Claude Code's file format, a scripted analyzer, a hashing embedder.

`kifu demo DIR` builds a complete store from it, so the web app, the API and the Hermes plugin can be tried
(and screenshotted, and tested) without a single real conversation or model call.

The persona is Ada, working on five side projects on a laptop and a studio machine over ten weeks.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import uuid

import numpy as np

from . import config

HOME = "/home/ada"

# ---- the story ----------------------------------------------------------------------------------------------
# Each session: host, project, when (days before the demo's "now", hour), title, turns.
# A turn: (prompt, reply, tools, minutes the assistant works). Tools: "write:<path>", "commit:<msg>",
# "ask:recommended" | "ask:option" | "ask:dismissed", "sub:write:<path>", "gap" (hours of autonomous work).
# Each session also says which threads a careful reader would find, keyed by the prompt text they start at.

SESSIONS = [
    dict(host="laptop", project="tidepool", days=68, hour=9, title="Tide prediction API",
         turns=[
             ("I want a small web app that predicts tides for my local beach from the NOAA harmonics. "
              "Start with a FastAPI backend that returns the next 48 hours.",
              "The backend is in place: /tides returns 48 hours of predictions computed from the harmonic "
              "constituents. I added a cache so the constituents are loaded once.",
              ["write:tidepool/api/main.py", "write:tidepool/api/harmonics.py", "commit:api: 48h tide predictions"], 35),
             ("the NOAA endpoint wants a key, use NOAA_API_KEY=9f3c2a7e41b8d6f0c5e2 for now",
              "Configured through an environment variable; the key is not committed.",
              ["write:tidepool/api/settings.py"], 5),
             ("go on", "Tests for the harmonic sum are in, and the CI workflow runs them.",
              ["write:tidepool/tests/test_harmonics.py", "commit:tests for harmonic sum"], 20),
             ("could it work offline on the beach? there is no signal down there",
              "Yes: a service worker can cache the last predictions and the app shell. The tricky part is "
              "invalidation, since predictions go stale after 48 hours. Want me to sketch it?", [], 4),
             ("later, first the frontend", "Understood. I scaffolded a small Svelte frontend with the chart.",
              ["write:tidepool/web/src/App.svelte", "commit:web: tide chart"], 40),
         ],
         threads=[
             dict(at="small web app that predicts tides", to="go on", title="Tide prediction backend", kind="project",
                  status="shipped", quote="I want a small web app that predicts tides for my local beach",
                  loose=[], next="", keywords=["tidepool", "fastapi", "harmonics", "backend"]),
             dict(at="could it work offline", to="later, first the frontend", title="Offline mode for tidepool",
                  kind="idea", status="parked", summary='The beach has no signal, so the app should work from cached predictions. Parked for the frontend.', quote="could it work offline on the beach? there is no signal down there",
                  loose=["Sketch the service worker cache and its 48-hour invalidation"],
                  next="Sketch the service worker and decide how stale predictions are marked",
                  keywords=["tidepool", "offline", "service worker", "cache", "invalidation"]),
         ]),
    dict(host="laptop", project="sensor-hub", days=64, hour=20, title="Garden sensors on ESP32",
         turns=[
             ("the soil moisture sensors in the garden report over MQTT. build a hub that stores readings "
              "and shows the last week per bed",
              "The hub subscribes to garden/+/moisture, stores readings in SQLite and serves a weekly chart.",
              ["write:sensor-hub/hub.py", "write:sensor-hub/store.py", "commit:hub: store and chart moisture"], 50),
             ("add battery voltage too, the sensors die without warning",
              "Battery voltage is stored and the chart shows a warning line at 3.3 V.",
              ["write:sensor-hub/hub.py", "commit:battery telemetry"], 25),
             ("ok", "Anything else for the hub?", [], 1),
         ],
         threads=[
             dict(at="soil moisture sensors", to="soil moisture sensors", title="Sensor hub for garden moisture",
                  kind="project", status="shipped", quote="build a hub that stores readings and shows the last week per bed",
                  loose=[], next="", keywords=["sensor-hub", "mqtt", "moisture", "garden"]),
             dict(at="add battery voltage", to="ok", title="Battery telemetry for sensors", kind="feature",
                  status="shipped", quote="add battery voltage too, the sensors die without warning", loose=[], next="",
                  keywords=["sensor-hub", "battery", "voltage", "telemetry"]),
         ]),
    dict(host="laptop", project="inkwell", days=60, hour=11, title="Blog engine import",
         turns=[
             ("inkwell should import my markdown vault, front matter and wiki links included",
              "I wrote an importer for front matter; wiki links still need a resolver for [[page]] syntax.",
              ["write:inkwell/importer.py", "commit:importer: front matter"], 45),
             ("how should broken wiki links show up?",
              "Three options: drop them, render them as plain text, or keep them with a dashed underline "
              "and a list on the dashboard.", ["ask:recommended"], 3),
             ("do the dashed underline", "Done for rendering; the dashboard list is not built yet.",
              ["write:inkwell/render.py", "commit:render broken wiki links"], 30),
         ],
         threads=[
             dict(at="import my markdown vault", to="do the dashed underline", title="Import markdown vault into inkwell",
                  kind="feature", status="started", summary='Front matter imports; wiki links render as broken but have no resolver, and the dashboard list is missing.', quote="inkwell should import my markdown vault, front matter and wiki links included",
                  loose=["Resolver for [[page]] wiki links", "Dashboard list of broken links"],
                  next="Write the [[page]] resolver against the vault's file names",
                  keywords=["inkwell", "import", "markdown", "vault", "wiki links"]),
         ]),
    dict(host="studio", project="chess-lab", days=55, hour=22, title="Engine evaluation experiments",
         turns=[
             ("for chess-lab: could a tiny NNUE evaluation beat the handcrafted eval at bullet time controls?",
              "Probably at longer controls; at bullet the incremental update cost matters. I can set up a "
              "gauntlet with 200 games per setting.", [], 5),
             ("yes run it", "The gauntlet runs overnight.", ["write:chess-lab/gauntlet.py", "gap:7"], 30),
             ("results?", "Handcrafted eval wins at 1+0 by 38 Elo; NNUE wins at 5+0 by 12 Elo, inside the error bars.",
              [], 6),
             ("then drop nnue for now, not worth it",
              "Dropped. The gauntlet script stays if you want to revisit.", ["commit:gauntlet script"], 3),
         ],
         threads=[
             dict(at="tiny NNUE evaluation", to="then drop nnue", title="NNUE evaluation for chess-lab", kind="research",
                  status="dropped", quote="could a tiny NNUE evaluation beat the handcrafted eval at bullet time controls?",
                  loose=[], next="", keywords=["chess-lab", "nnue", "evaluation", "gauntlet"]),
         ]),
    dict(host="laptop", project="dotfiles", days=50, hour=8, title="New laptop bootstrap",
         turns=[
             ("write a bootstrap script so a new laptop gets my shell, editor and keys in one go",
              "bootstrap.sh installs packages, links the dotfiles and restores SSH keys from the password manager.",
              ["write:dotfiles/bootstrap.sh", "commit:bootstrap script"], 30),
             ("go on", "Tested in a fresh container; it is idempotent now.", ["commit:idempotent bootstrap"], 15),
         ],
         threads=[
             dict(at="bootstrap script", to="go on", title="One-step laptop bootstrap", kind="project", status="shipped",
                  quote="write a bootstrap script so a new laptop gets my shell, editor and keys in one go", loose=[],
                  next="", keywords=["dotfiles", "bootstrap", "laptop"]),
         ]),
    dict(host="laptop", project="tidepool", days=41, hour=14, title="Tide alerts",
         turns=[
             ("idea: tidepool could send a push notification when low tide falls on a weekend morning",
              "That fits well: a daily job checks the next 7 days and web push delivers it. Want me to build "
              "the scheduler first or the push subscription?", [], 3),
             ("/model sonnet", "", [], 0),
             ("wait, the chart is wrong after daylight saving time, fix that first",
              "Fixed: predictions were computed in UTC and rendered as local time twice.",
              ["write:tidepool/web/src/chart.ts", "commit:fix DST offset in chart"], 25),
         ],
         threads=[
             dict(at="push notification when low tide", to="push notification when low tide",
                  title="Weekend low-tide push alerts", summary='A daily check for weekend-morning low tides with a web push. Offered, then pushed aside by a DST bug.', kind="idea", status="proposed",
                  quote="tidepool could send a push notification when low tide falls on a weekend morning",
                  loose=["Daily job that checks the next 7 days", "Web push subscription"],
                  next="Build the daily check and log matches before adding push",
                  keywords=["tidepool", "alerts", "push", "low tide", "weekend"]),
             dict(at="chart is wrong after daylight", to="chart is wrong after daylight", title="Fix DST offset in tide chart",
                  kind="fix", status="shipped", quote="the chart is wrong after daylight saving time", loose=[], next="",
                  keywords=["tidepool", "chart", "dst"]),
         ]),
    dict(host="studio", project="sensor-hub", days=36, hour=19, title="Move MQTT broker",
         turns=[
             ("the hub on the laptop sleeps at night. move the MQTT broker and the hub to the studio machine",
              "The broker config and a systemd unit for the hub are ready; the sensors still point at the old address.",
              ["write:sensor-hub/deploy/mosquitto.conf", "write:sensor-hub/deploy/hub.service", "commit:deploy on studio"], 40),
             ("should the sensors use mDNS or a fixed IP?",
              "mDNS is nicer but the ESP32 library is flaky with it; a DHCP reservation is the robust choice.",
              ["ask:option"], 2),
             ("fixed ip then", "I documented the reservation; reflashing the sensors is still to do.",
              ["write:sensor-hub/docs/network.md"], 10),
         ],
         threads=[
             dict(at="move the MQTT broker", to="fixed ip then", title="Run MQTT broker and hub on studio", summary='Broker and hub run on the always-on machine; the sensors still point at the laptop.', kind="ops",
                  status="started", quote="move the MQTT broker and the hub to the studio machine",
                  loose=["Reflash the sensors with the reserved IP"],
                  next="Reflash one sensor and watch its readings arrive on the studio broker",
                  keywords=["sensor-hub", "mqtt", "broker", "studio", "deploy"]),
         ]),
    dict(host="laptop", project="tidepool", days=30, hour=10, title="Offline mode",
         turns=[
             ("back to offline mode for tidepool: the beach still has no signal",
              "Service worker caches the shell and the last predictions; stale data is greyed out after 48 hours.",
              ["write:tidepool/web/src/sw.ts", "commit:offline shell and predictions"], 45),
             ("what happens when the cache is older than a week?",
              "Right now it shows the greyed chart anyway. I can show a banner instead. Should I?", [], 3),
         ],
         threads=[
             dict(at="back to offline mode", to="what happens when the cache", title="Offline mode for tidepool",
                  kind="feature", status="started", summary='Service worker caches the shell and predictions; what to show for week-old data is still open.', quote="back to offline mode for tidepool: the beach still has no signal",
                  loose=["Banner when cached predictions are older than a week"],
                  next="Show a banner instead of a greyed chart when the cache is a week old",
                  keywords=["tidepool", "offline", "service worker", "cache", "stale"]),
         ]),
    dict(host="laptop", project="inkwell", days=22, hour=21, title="Themes",
         turns=[
             ("inkwell needs a dark theme that follows the system setting",
              "Dark theme is in, driven by prefers-color-scheme with a manual toggle.",
              ["write:inkwell/theme.css", "commit:dark theme"], 25),
             ("go on", "Contrast checked for code blocks too.", ["commit:code block contrast"], 10),
             ("let's pause inkwell for a while", "Paused. The vault importer is still half done.", [], 1),
         ],
         threads=[
             dict(at="dark theme", to="go on", title="Dark theme for inkwell", kind="feature", status="shipped",
                  quote="inkwell needs a dark theme that follows the system setting", loose=[], next="",
                  keywords=["inkwell", "theme", "dark"]),
             dict(at="pause inkwell", to="pause inkwell", title="Import markdown vault into inkwell", kind="feature",
                  status="parked", summary='Inkwell was paused with the vault importer half done.', quote="let's pause inkwell for a while",
                  loose=["Resolver for [[page]] wiki links", "Dashboard list of broken links"],
                  next="Write the [[page]] resolver against the vault's file names",
                  keywords=["inkwell", "import", "markdown", "vault", "wiki links"]),
         ]),
    dict(host="studio", project="chess-lab", days=12, hour=23, title="Opening book",
         turns=[
             ("chess-lab: build an opening book from my own online games, only lines I actually play",
              "The book builder reads PGN exports and keeps moves played at least 5 times.",
              ["write:chess-lab/book.py", "sub:write:chess-lab/tests/test_book.py", "commit:opening book from own games"], 40),
             ("how do we weight moves by result?", "Weighted by score with a floor so rare wins are not overrated. "
              "Want me to add it to the engine's move ordering too?", ["ask:dismissed"], 3),
         ],
         threads=[
             dict(at="opening book from my own", to="how do we weight moves", title="Opening book from own games", summary='A book built from lines actually played, weighted by result. Using it in move ordering was offered, not answered.',
                  kind="feature", status="started", quote="build an opening book from my own online games, only lines I actually play",
                  loose=["Use the book in the engine's move ordering"], next="Wire the weighted book into move ordering",
                  keywords=["chess-lab", "opening book", "pgn"]),
         ]),
    dict(host="laptop", project="tidepool", days=4, hour=15, title="Release prep",
         turns=[
             ("prepare tidepool for a first public release: readme, license, screenshots",
              "README with screenshots and MIT license are in. The offline banner is still missing.",
              ["write:tidepool/README.md", "write:tidepool/LICENSE", "commit:release prep"], 35),
             ("go on", "Tagged v0.1.0.", ["commit:v0.1.0"], 5),
         ],
         threads=[
             dict(at="first public release", to="go on", title="First public release of tidepool", kind="chore",
                  status="shipped", quote="prepare tidepool for a first public release", loose=[], next="",
                  keywords=["tidepool", "release", "readme"]),
         ]),
]


# ---- writing session files ----------------------------------------------------------------------------------

def _ts(t):
    return t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def write_sessions(root, now=None):
    """Write the story as Claude Code session files under root/<host>/projects. Returns the session ids."""
    now = now or dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)
    ids = []
    for n, spec in enumerate(SESSIONS):
        sid = str(uuid.UUID(hashlib.md5(f"kifu-demo-{n}".encode()).hexdigest()))
        ids.append(sid)
        cwd = f"{HOME}/code/{spec['project']}"
        folder = os.path.join(root, spec["host"], "projects", cwd.replace("/", "-"))
        os.makedirs(folder, exist_ok=True)
        start = (now - dt.timedelta(days=spec["days"])).replace(hour=spec["hour"])
        records, sub_records, t = [], [], start
        base = {"sessionId": sid, "cwd": cwd, "entrypoint": "cli", "gitBranch": "main", "isSidechain": False}

        def rec(kind, when, records=records, base=base, **fields):
            records.append({"type": kind, "uuid": str(uuid.uuid4()), "timestamp": _ts(when), **base, **fields})

        for prompt, reply, tools, minutes in spec["turns"]:
            rec("user", t, message={"role": "user", "content": prompt}, origin={"kind": "human"})
            t += dt.timedelta(seconds=30)
            content = []
            for tool in tools:
                kind, _, arg = tool.partition(":")
                tid = "toolu_" + uuid.uuid4().hex[:20]
                if kind == "write":
                    content.append({"type": "tool_use", "id": tid, "name": "Write",
                                    "input": {"file_path": f"{HOME}/code/{arg}", "content": "..."}})
                elif kind == "commit":
                    content.append({"type": "tool_use", "id": tid, "name": "Bash",
                                    "input": {"command": f'git add -A && git commit -m "{arg}"'}})
                elif kind == "gap":
                    t += dt.timedelta(hours=int(arg))
                elif kind == "sub":
                    _, _, path = arg.partition(":")
                    sub_records.append({"type": "assistant", "timestamp": _ts(t), "sessionId": sid, "isSidechain": True,
                                        "message": {"content": [{"type": "tool_use", "id": tid, "name": "Write",
                                                                 "input": {"file_path": f"{HOME}/code/{path}"}}]}})
                elif kind == "ask":
                    q = {"question": "Which approach?", "options": [
                        {"label": "The first one (Recommended)"}, {"label": "The second one"}]}
                    records.append({"type": "assistant", "uuid": str(uuid.uuid4()), "timestamp": _ts(t), **base,
                                    "message": {"content": [{"type": "tool_use", "id": tid, "name": "AskUserQuestion",
                                                             "input": {"questions": [q]}}]}})
                    t += dt.timedelta(minutes=1)
                    if arg == "dismissed":
                        result = {"type": "tool_result", "tool_use_id": tid, "is_error": True,
                                  "content": "The user doesn't want to proceed with this tool use."}
                    else:
                        pick = q["options"][0 if arg == "recommended" else 1]["label"]
                        result = {"type": "tool_result", "tool_use_id": tid,
                                  "content": f'Your questions have been answered: "{q["question"]}"="{pick}". '
                                             f"You can now continue with these answers in mind."}
                    rec("user", t, message={"role": "user", "content": [result]})
            t += dt.timedelta(minutes=max(minutes - 1, 0))
            if reply:
                content.append({"type": "text", "text": reply})
            if content:
                rec("assistant", t, message={"role": "assistant", "content": content})
            t += dt.timedelta(minutes=3 + (len(prompt) % 7))       # the user reads and types
        records.append({"type": "ai-title", "aiTitle": spec["title"], "sessionId": sid})
        with open(os.path.join(folder, f"{sid}.jsonl"), "w") as fh:
            fh.writelines(json.dumps(r) + "\n" for r in records)
        if sub_records:
            sub_dir = os.path.join(folder, sid, "subagents")
            os.makedirs(sub_dir, exist_ok=True)
            with open(os.path.join(sub_dir, "agent-demo.jsonl"), "w") as fh:
                fh.writelines(json.dumps(r) + "\n" for r in sub_records)
    return ids


# ---- scripted analyzer and embedder -------------------------------------------------------------------------

def _by_title():
    return {s["title"]: s for s in SESSIONS}


def fixture_backend(system, user, schema):
    if "same_idea" in schema.get("properties", {}):
        return _consolidate(user)
    if "settled" in schema.get("properties", {}):
        return _judge(user)
    title = re.match(r"Session: (.*?) \|", user).group(1)
    spec = _by_title()[title]
    moves = re.findall(r"\[M(\d+) [^\]]*\]\nUSER: (.*?)\nASSISTANT", user, re.S)

    def move_of(text):
        for number, prompt in moves:
            if text.lower() in prompt.lower():
                return int(number)
        return 1

    return {"session_gist": title, "threads": [
        {"title": t["title"], "kind": t["kind"], "summary": t.get("summary") or f"Came up in the “{title}” session.",
         "status": t["status"],
         "first_move": move_of(t["at"]), "last_move": max(move_of(t["at"]), move_of(t["to"])), "quote": t["quote"],
         "loose_ends": t["loose"], "next_step": t["next"], "keywords": t["keywords"]} for t in spec["threads"]]}


def _judge(user):
    """A loose end counts as settled when a commit subject contains most of its words."""
    loose = re.findall(r"^(\d+)\. (.*)$", user.split("Later commits")[0], re.M)
    subjects = " ".join(re.findall(r"^- \S+ (.*)$", user, re.M)).lower()
    settled = []
    for number, text in loose:
        words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 3]
        if words and sum(w in subjects for w in words) >= 0.6 * len(words):
            settled.append(int(number))
    return {"settled": settled, "note": f"{len(settled)} of {len(loose)} loose ends appear in later commits."}


def _consolidate(user):
    entries = re.findall(r"\n  (.+?) \((\w+), (\w+)\)\n  (.*?)\n", "\n" + user)
    loose = re.findall(r"    - (.*)", user.split("\n\n")[-1])
    titles = [e[0] for e in entries]
    same = len(set(titles)) == 1
    latest = entries[-1]
    return {"same_idea": same, "title": latest[0], "status": latest[2],
            "summary": latest[3],
            "loose_ends": loose, "next_step": "", "verdict": "Worth picking up again: it keeps coming back." if loose else ""}


def hashing_embedder(texts, dim=256):
    """Bag of words hashed into a fixed vector: similar wording, similar vector. Good enough for a demo.

    For thread texts ("Title. Summary Keywords: a, b") only the title and keywords count: two summaries of the
    same idea use different words, which a real embedding model sees through and a bag of words does not.
    """
    out = np.zeros((len(texts), dim), dtype=np.float32)
    for i, text in enumerate(texts):
        if "Keywords:" in text:
            text = text.split(". ", 1)[0] + " " + text.rsplit("Keywords:", 1)[1]
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            out[i, int(hashlib.md5(word.encode()).hexdigest(), 16) % dim] += 1.0
        norm = np.linalg.norm(out[i])
        out[i] /= norm if norm else 1.0
    return out


def install():
    """Route analysis and embeddings to the scripted versions."""
    from . import analyze, embed
    analyze.fixture = fixture_backend
    embed.embedder = hashing_embedder


# ---- building a demo store ----------------------------------------------------------------------------------

def build(directory, log=print, now=None):
    """Write sessions, a config, and a fully analyzed database under directory. Returns the config path."""
    from . import analyze, db, embed, extract, link

    directory = os.path.abspath(directory)
    sources_root = os.path.join(directory, "sources")
    write_sessions(sources_root, now=now)
    cfg_path = os.path.join(directory, "config.toml")
    with open(cfg_path, "w") as fh:
        fh.write(f'''# kifu demo: synthetic sessions, scripted analysis, no model calls
demo = true
user = "Ada"
timezone = "Europe/Lisbon"
data_dir = "{directory}/data"
project_roots = ["{HOME}/code"]

[analysis]
backend = "fixture"
workers = 2

[[sources]]
host = "laptop"
path = "{sources_root}/laptop/projects"

[[sources]]
host = "studio"
path = "{sources_root}/studio/projects"
''')
    cfg = config.load(cfg_path)
    config.set_current(cfg)
    install()
    con = db.connect(cfg.db_path)
    for src in cfg.source_list():
        extract.scan(con, os.path.expanduser(src.path), host=src.host, log=lambda m: None)
    embed.build_moves(con)
    embed.embed_moves(con, log=lambda m: None)
    analyze.analyze(lambda: db.connect(cfg.db_path), backend="fixture", workers=1, log=lambda m: None)
    link.build_lines(con, backend="fixture", workers=1, log=lambda m: None)
    log(f"demo store: {cfg.db_path}")
    return cfg_path
