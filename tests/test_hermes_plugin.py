"""The Hermes plugin against a real kifu server on the demo store: tools, /kifu, dashboard routes, digest script."""
import importlib.util
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

PLUGIN = Path(__file__).resolve().parent.parent / "hermes-plugin"
sys.path.insert(0, str(PLUGIN))

import tools  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = _load("kifu_plugin", PLUGIN / "__init__.py")
plugin_api = _load("kifu_plugin_api", PLUGIN / "dashboard" / "plugin_api.py")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(store, monkeypatch):
    from kifu import api
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("KIFU_URL", url)
    plugin_api.client.base_url = lambda ctx=None: url
    yield url
    srv.should_exit = True
    thread.join(timeout=5)


def call(fn, **args):
    return json.loads(tools._wrap(fn)(args))


def test_tools(server):
    ideas = call(tools.ideas, limit=3)
    assert ideas["ok"] and ideas["total_open"] == 6 and ideas["ideas"][0]["title"] == "Weekend low-tide push alerts"
    assert len(json.dumps(ideas)) < 4000, "tool results must stay small"
    offline = call(tools.ideas, query="offline")["ideas"][0]
    trail = call(tools.idea, anchor=offline["anchor"])
    assert len(trail["trail"]) == 2 and all(t["resume"] for t in trail["trail"])
    assert call(tools.mark, anchor=offline["anchor"], state="done")["mark"] == "done"
    assert call(tools.ideas)["total_open"] == 5
    assert call(tools.mark, anchor=offline["anchor"], state="open")["mark"] == "open"
    assert call(tools.idea, anchor="nope")["ok"] is False


def test_tools_explain_when_kifu_is_down(monkeypatch):
    monkeypatch.setenv("KIFU_URL", f"http://127.0.0.1:{free_port()}")
    result = call(tools.ideas)
    assert result["ok"] is False and "kifu serve" in result["error"]


def test_slash_command(server):
    text = plugin._slash("")
    assert text.startswith("kifu · 6 open idea(s)") and "Weekend low-tide push alerts" in text
    assert "matching 'inkwell'" in plugin._slash("inkwell")
    assert "quiet for" in plugin._slash("digest") or "No open idea" in plugin._slash("digest")


def test_dashboard_routes(server):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/kifu")
    c = TestClient(app)
    overview = c.get("/api/plugins/kifu/overview").json()
    assert overview["kifu_url"] == server and len(overview["top"]) == 5 and overview["summary"]["questions_asked"]
    lines = c.get("/api/plugins/kifu/lines", params={"q": "inkwell"}).json()
    assert lines["total"] == 1
    anchor = lines["items"][0]["anchor"]
    assert c.put(f"/api/plugins/kifu/lines/{anchor}/mark", json={"state": "dismissed"}).status_code == 200
    assert c.get("/api/plugins/kifu/lines", params={"marked": "true"}).json()["total"] == 1
    assert c.put(f"/api/plugins/kifu/lines/{anchor}/mark", json={"state": "maybe"}).status_code == 422
    assert c.get("/api/plugins/kifu/lines/nope").status_code == 404
    plugin_api.client.base_url = lambda ctx=None: f"http://127.0.0.1:{free_port()}"
    down = c.get("/api/plugins/kifu/overview")
    assert down.status_code == 502 and "kifu serve" in down.json()["detail"]


def test_digest_script(server, tmp_path):
    script = tmp_path / "kifu_digest.py"
    script.write_text(plugin.DIGEST_SCRIPT.format(url=server, quiet=0))
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and "quiet for 0+ days" in out.stdout
    script.write_text(plugin.DIGEST_SCRIPT.format(url=server, quiet=9999))
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30)
    assert out.stdout == "", "nothing quiet enough means nothing is sent"


# ---- Telegram digest with buttons --------------------------------------------------------------------------

import asyncio  # noqa: E402
import datetime as dt  # noqa: E402

telegram_digest = _load("kifu_telegram_digest", PLUGIN / "telegram_digest.py")


def test_digest_schedule():
    berlin = dt.timezone(dt.timedelta(hours=2))
    now = dt.datetime(2026, 9, 14, 12, 0, tzinfo=berlin)                    # a Monday
    assert telegram_digest.next_run("0 18 * * 0", now) == dt.datetime(2026, 9, 20, 18, 0, tzinfo=berlin)
    assert telegram_digest.next_run("30 9 * * *", now) == dt.datetime(2026, 9, 15, 9, 30, tzinfo=berlin)
    assert telegram_digest.next_run("0 18 * * 1,5", now) == dt.datetime(2026, 9, 14, 18, 0, tzinfo=berlin)
    assert telegram_digest.next_run("*/5 * * * *", now) is None
    slot = {"next": "2026-09-18T16:00:00+00:00"}
    assert telegram_digest.next_run("auto", now, slot) == dt.datetime(2026, 9, 18, 16, 0, tzinfo=dt.UTC)
    assert telegram_digest.next_run("auto", now, None) is None


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kw):
        self.sent.append(kw)


class _Button:
    def __init__(self, text, callback_data):
        self.text, self.callback_data = text, callback_data


class _Markup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


def test_digest_buttons_mark_ideas_and_answer_only_in_the_digest_chat(server, tmp_path, monkeypatch):
    import types
    # python-telegram-bot ships with Hermes, not with kifu: the two classes the digest uses are enough here.
    monkeypatch.setitem(sys.modules, "telegram", types.SimpleNamespace(InlineKeyboardButton=_Button,
                                                                       InlineKeyboardMarkup=_Markup))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    telegram_digest.save_state({"mode": "buttons", "deliver": "telegram:4242", "schedule": "auto", "quiet_days": 0})
    bot = FakeBot()
    sent = asyncio.run(telegram_digest.send_digest(bot, "4242", quiet_days=0, limit=3))
    assert sent == 3 and bot.sent[0]["text"].startswith("kifu · ")
    datas = [b.callback_data for m in bot.sent[1:] for row in m["reply_markup"].inline_keyboard for b in row]
    assert len(datas) == 9 and all(len(d.encode()) <= 64 and d.startswith("kifu:") for d in datas)
    state = telegram_digest.load_state()
    done_data = datas[0]
    anchor = state["tokens"][done_data.split(":")[-1]]

    assert asyncio.run(telegram_digest.handle_button(done_data, "9999", state)) == (None, None), "other chats get nothing"
    assert asyncio.run(telegram_digest.handle_button(done_data, "4242", state)) == ("✓ done", None)
    import urllib.parse
    import urllib.request
    with urllib.request.urlopen(f"{server}/api/lines/{urllib.parse.quote(anchor, safe='')}") as r:
        assert json.load(r)["mark"]["state"] == "done"
    _, brief = asyncio.run(telegram_digest.handle_button(datas[2].replace(datas[2].split(":")[-1], datas[5].split(":")[-1]),
                                                         "4242", state))
    assert brief.startswith("# ") and len(brief) <= telegram_digest.TELEGRAM_LIMIT
    assert asyncio.run(telegram_digest.handle_button("kifu:d:000000000000", "4242", state))[1].startswith("kifu: this button")


def test_habits_slot_and_brief_endpoints(server):
    import urllib.request
    with urllib.request.urlopen(f"{server}/api/habits/slot") as r:
        slot = json.load(r)
    assert slot["weekday"] in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun") and slot["cron"].startswith("0 ")
    assert dt.datetime.fromisoformat(slot["next"]) and "coding sessions" in slot["reason"]


def test_decision_and_why_tools(server, tmp_path):
    found = call(tools.decisions, query="svelte")
    assert found["ok"] and found["items"][0]["because"] == "the bundle stays small on a phone at the beach"
    assert call(tools.decisions, kind="promise")["ok"]
    assert len(json.dumps(call(tools.decisions, limit=20))) < 8000, "tool results must stay small"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=A", "-c", "user.email=a@example.org", "commit", "-qm", "one"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    why = call(tools.why, file=str(repo / "a.py"), start=1)
    assert why["ok"] and why["ranges"][0]["commit"]["summary"] == "one" and why["ranges"][0]["prompt"] is None
    assert call(tools.why, file=str(repo / "missing.py"))["ok"] is False
