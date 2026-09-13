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
    assert ideas["ok"] and ideas["total_open"] == 5 and ideas["ideas"][0]["title"] == "Weekend low-tide push alerts"
    assert len(json.dumps(ideas)) < 4000, "tool results must stay small"
    offline = call(tools.ideas, query="offline")["ideas"][0]
    trail = call(tools.idea, anchor=offline["anchor"])
    assert len(trail["trail"]) == 2 and all(t["resume"] for t in trail["trail"])
    assert call(tools.mark, anchor=offline["anchor"], state="done")["mark"] == "done"
    assert call(tools.ideas)["total_open"] == 4
    assert call(tools.mark, anchor=offline["anchor"], state="open")["mark"] == "open"
    assert call(tools.idea, anchor="nope")["ok"] is False


def test_tools_explain_when_kifu_is_down(monkeypatch):
    monkeypatch.setenv("KIFU_URL", f"http://127.0.0.1:{free_port()}")
    result = call(tools.ideas)
    assert result["ok"] is False and "kifu serve" in result["error"]


def test_slash_command(server):
    text = plugin._slash("")
    assert text.startswith("kifu · 5 open idea(s)") and "Weekend low-tide push alerts" in text
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
