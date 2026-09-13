"""kifu for Hermes: agent tools, a /kifu command, `hermes kifu` (digest setup), and an Ideas dashboard tab."""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("kifu")

DIGEST_JOB = "kifu-digest"
DIGEST_SCRIPT = """#!/usr/bin/env python3
# Written by `hermes kifu setup` (kifu). Prints the digest of open ideas that went quiet.
# A no_agent cron job delivers stdout; empty stdout means nothing is sent.
import json, sys, urllib.request
URL, QUIET = {url!r}, {quiet}
try:
    with urllib.request.urlopen(f"{{URL}}/api/digest?quiet_days={{QUIET}}&limit=5", timeout=30) as r:
        data = json.load(r)
except Exception as exc:
    print(f"kifu digest: kifu is not reachable at {{URL}} ({{exc}})")
    sys.exit(0)
if data.get("items"):
    print(data["text"])
"""


def _client():
    try:
        from . import kifu_client
    except ImportError:
        import kifu_client  # type: ignore
    return kifu_client


def _slash(raw_args: str = "") -> str:
    client = _client()
    arg = (raw_args or "").strip()
    try:
        if arg == "digest":
            status, data = client.get("/api/digest", quiet_days=client.setting("digest_quiet_days", 21))
            return data["text"] if status == 200 else f"kifu: {data}"
        status, data = client.get("/api/lines", open="true", compact="true", q=arg or None, limit=5)
    except client.KifuUnavailable as exc:
        return str(exc)
    if status != 200:
        return f"kifu answered {status}"
    if not data["items"]:
        return "kifu: no open ideas" + (f" matching '{arg}'" if arg else "")
    out = [f"kifu · {data['total']} open idea(s)" + (f" matching '{arg}'" if arg else "")]
    for i in data["items"]:
        out.append(f"\n• {i['title']} — {i['project']}, {i['status']}, quiet {i['quiet_days']}d")
        out.extend(f"  – {x}" for x in i["loose_ends"][:2])
    return "\n".join(out)


def _cli_setup(parser) -> None:
    sub = parser.add_subparsers(dest="kifu_cmd")
    p = sub.add_parser("setup", help="create or update the weekly digest cron job (no LLM)")
    p.add_argument("--deliver", help="delivery target, e.g. telegram:<chat_id> (default: settings.digest_deliver)")
    p.add_argument("--schedule", help="cron schedule (default: settings.digest_schedule)")
    p.add_argument("--quiet-days", type=int, help="default: settings.digest_quiet_days")
    p.add_argument("--dry-run", action="store_true")
    sub.add_parser("status", help="is the kifu service reachable, what does it hold")
    sub.add_parser("digest", help="print the digest now")


def _cli_handle(args) -> int:
    client = _client()
    cmd = getattr(args, "kifu_cmd", None) or "status"
    if cmd == "digest":
        print(_slash("digest"))
        return 0
    if cmd == "status":
        try:
            status, health = client.get("/api/health")
            _, stats = client.get("/api/stats")
        except client.KifuUnavailable as exc:
            print(exc)
            return 1
        print(f"kifu at {client.base_url()}: {stats['sessions']} sessions, {stats['lines']} ideas, "
              f"{stats['open']} open, latest session {str(health['latest_session'])[:16]}")
        return 0
    return _setup_digest(args, client)


def _setup_digest(args, client) -> int:
    import os
    from pathlib import Path

    deliver = args.deliver or client.setting("digest_deliver", "local")
    schedule = args.schedule or client.setting("digest_schedule", "0 18 * * 0")
    quiet = args.quiet_days or int(client.setting("digest_quiet_days", 21))
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    script = home / "scripts" / "kifu_digest.py"
    print(f"script   {script}\njob      {DIGEST_JOB} · {schedule} · no_agent · deliver {deliver} · quiet {quiet}+ days")
    if args.dry_run:
        return 0
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(DIGEST_SCRIPT.format(url=client.base_url(), quiet=quiet), encoding="utf-8")
    script.chmod(0o755)
    try:
        from cron import jobs as cron_jobs  # type: ignore
        from tools.cronjob_tools import cronjob  # type: ignore  (the entry point `hermes cron` uses)
    except Exception as exc:
        print(f"Hermes cron is not importable ({exc}); create the job with:\n"
              f"  hermes cron create '{schedule}' --name {DIGEST_JOB} --no-agent --script kifu_digest.py --deliver {deliver}")
        return 1
    existing = [j for j in cron_jobs.load_jobs() if j.get("name") == DIGEST_JOB]
    fields = dict(schedule=schedule, deliver=deliver, script="kifu_digest.py", no_agent=True)
    if existing:
        res = json.loads(cronjob(action="update", job_id=existing[0]["id"], **fields))
    else:
        res = json.loads(cronjob(action="create", name=DIGEST_JOB, **fields))
    if not res.get("success"):
        print(f"cron: {res.get('error')}")
        return 1
    print("updated" if existing else "created")
    return 0


def register(ctx) -> None:
    try:
        from . import tools
    except ImportError:
        import tools  # type: ignore
    tools.register_tools(ctx)
    try:
        ctx.register_command("kifu", _slash, description="kifu: open ideas from past Claude Code sessions",
                             args_hint="[words | digest]")
    except Exception as exc:
        logger.debug("kifu: slash command not registered: %s", exc)
    try:
        ctx.register_cli_command("kifu", help="kifu: digest setup and status", setup_fn=_cli_setup,
                                 handler_fn=_cli_handle,
                                 description="The ideas left behind in your Claude Code sessions.")
    except Exception as exc:
        logger.debug("kifu: CLI command not registered: %s", exc)
