"""A small client for the kifu API. Standard library only, so the plugin adds nothing to Hermes' environment."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8765"


class KifuUnavailable(Exception):
    pass


def base_url(ctx=None) -> str:
    if os.environ.get("KIFU_URL"):
        return os.environ["KIFU_URL"].rstrip("/")
    if ctx is not None:
        try:
            value = ctx.get_config("url", None)
            if value:
                return str(value).rstrip("/")
        except Exception:
            pass
    return _from_hermes_config("url", DEFAULT_URL).rstrip("/")


def _from_hermes_config(key: str, default):
    """plugins.entries.kifu.settings.<key> from Hermes' config.yaml, for code that has no plugin context."""
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    try:
        import yaml  # Hermes ships PyYAML

        cfg = yaml.safe_load((home / "config.yaml").read_text()) or {}
        return (((cfg.get("plugins") or {}).get("entries") or {}).get("kifu") or {}).get("settings", {}).get(key, default)
    except Exception:
        return default


def setting(key: str, default):
    return _from_hermes_config(key, default)


def request(method: str, path: str, params: dict | None = None, body: dict | None = None, url: str | None = None,
            timeout: float = 20):
    base = url or base_url()
    query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
    full = f"{base}{path}" + (f"?{query}" if query else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(full, data=data, method=method,
                                 headers={"content-type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read() or b"null")
        except ValueError:
            detail = None
        return exc.code, detail
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise KifuUnavailable(f"kifu is not reachable at {base} ({getattr(exc, 'reason', exc)}); "
                              f"is `kifu serve` running?") from exc


def get(path, **params):
    return request("GET", path, params=params)
