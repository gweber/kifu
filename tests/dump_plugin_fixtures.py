"""Dump the dashboard plugin's API responses from a demo store, for hermes-plugin/dashboard/render_check.js.

    python tests/dump_plugin_fixtures.py OUT.json
"""
import json
import sys
import tempfile

from fastapi.testclient import TestClient

from kifu import api, config, db, demo

out_path = sys.argv[1]
cfg_path = demo.build(tempfile.mkdtemp(), log=lambda m: None)
config.set_current(config.load(cfg_path))
demo.install()
c = TestClient(api.app, base_url="http://127.0.0.1:8765")

# One idea with git activity, so the render check draws that part of a card too.
con = db.connect(config.get().db_path)
anchor = con.execute("SELECT anchor FROM lines WHERE score > 0 ORDER BY score DESC LIMIT 1").fetchone()[0]
con.execute("""INSERT OR REPLACE INTO checks(anchor, checked, host, repo, files, missing, commits, settled, note, error)
               VALUES (?, '2026-09-13', 'laptop', '/home/ada/code/tidepool', 2, 0, ?, '[1]', ?, NULL)""",
            (anchor, json.dumps([{"sha": "8f1b58c", "date": "2026-08-12T14:00:00+00:00",
                                  "subject": "add daily job that checks the next 7 days"}]),
             "The daily check landed; web push has not."))
con.commit()

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
