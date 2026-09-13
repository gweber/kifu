"""Dump the dashboard plugin's API responses from a demo store, for hermes-plugin/dashboard/render_check.js.

    python tests/dump_plugin_fixtures.py OUT.json
"""
import json
import sys
import tempfile

from fastapi.testclient import TestClient

from kifu import api, config, demo

out_path = sys.argv[1]
cfg_path = demo.build(tempfile.mkdtemp(), log=lambda m: None)
config.set_current(config.load(cfg_path))
demo.install()
c = TestClient(api.app)

overview = c.get("/api/overview", params={"top": 5}).json()
overview["kifu_url"] = "http://127.0.0.1:8765"
open_lines = c.get("/api/lines", params={"open": "true", "compact": "true", "limit": 100}).json()
first = open_lines["items"][0]["anchor"]
c.put(f"/api/lines/{open_lines['items'][-1]['anchor']}/mark", json={"state": "dismissed"})
marked = c.get("/api/lines", params={"marked": "true", "compact": "true", "limit": 100}).json()

fixtures = {
    "/overview": overview,
    "/lines": open_lines,
    "/lines?marked": marked,
    "/lines/": {i["anchor"]: c.get(f"/api/lines/{i['anchor']}").json() for i in open_lines["items"]},
    "first_anchor": first,
}
with open(out_path, "w") as fh:
    json.dump(fixtures, fh)
print(f"wrote {out_path}: {open_lines['total']} open ideas, {marked['total']} marked")
