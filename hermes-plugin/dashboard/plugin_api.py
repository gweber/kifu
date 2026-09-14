"""kifu dashboard backend, mounted at /api/plugins/kifu/ by the Hermes dashboard.

It holds no data: every route forwards to the kifu service, so the browser only ever talks to the dashboard
(and its authentication), and kifu can stay bound to 127.0.0.1.
"""
# No `from __future__ import annotations`: the dashboard loads this file by path, outside sys.modules, and
# pydantic could not resolve the request models from string annotations.
import importlib.util
from pathlib import Path
from typing import Literal

try:
    from fastapi import APIRouter, HTTPException
    from pydantic import BaseModel
except Exception:  # importable without FastAPI, for standalone checks
    class APIRouter:  # type: ignore
        def __getattr__(self, _name):
            return lambda *_a, **_k: (lambda fn: fn)

    class HTTPException(Exception):  # type: ignore
        def __init__(self, status_code=500, detail=""):
            super().__init__(detail)
            self.status_code, self.detail = status_code, detail

    class BaseModel:  # type: ignore
        pass

# The dashboard loads this file by path, not as a package; the plugin directory may be a symlink.
_spec = importlib.util.spec_from_file_location("_kifu_client", Path(__file__).resolve().parent.parent / "kifu_client.py")
client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(client)  # type: ignore[union-attr]

router = APIRouter()


def _call(method, path, params=None, body=None):
    try:
        status, data = client.request(method, path, params=params, body=body)
    except client.KifuUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    if status >= 400:
        detail = data.get("detail") if isinstance(data, dict) else data
        raise HTTPException(status, detail or f"kifu answered {status}")
    return data


class Mark(BaseModel):
    state: Literal["done", "dismissed", "open"]
    note: str = ""


class Job(BaseModel):
    kind: Literal["pull", "run"]


@router.get("/overview")
def overview():
    data = _call("GET", "/api/overview", params={"top": 5})
    data["kifu_url"] = client.base_url()
    return data


@router.get("/lines")
def lines(q: str = "", area: str = "", tool: str = "", marked: str = "", limit: int = 50, offset: int = 0):
    params = {"compact": "true", "q": q, "area": area, "tool": tool, "limit": limit, "offset": offset}
    if marked == "true":
        params["marked"] = "true"
    else:
        params["open"] = "true"
    return _call("GET", "/api/lines", params=params)


@router.get("/lines/{anchor}")
def line(anchor: str):
    return _call("GET", f"/api/lines/{anchor}")


@router.put("/lines/{anchor}/mark")
def mark(anchor: str, body: Mark):
    return _call("PUT", f"/api/lines/{anchor}/mark", body={"state": body.state, "note": body.note})


@router.post("/jobs")
def start_job(body: Job):
    return _call("POST", "/api/jobs", body={"kind": body.kind})


@router.get("/jobs/{job_id}")
def job(job_id: str):
    return _call("GET", f"/api/jobs/{job_id}", params={"tail": 1})
