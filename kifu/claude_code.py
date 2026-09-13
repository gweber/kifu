"""`kifu install claude`: wire kifu into Claude Code for all projects.

Adds two hooks to the user settings (SessionStart shows the project's open ideas, SessionEnd queues the session
for analysis) and registers `kifu mcp` as a user-scope MCP server. Backs up settings.json before changing it,
touches only kifu's own entries, and is safe to run again. `--uninstall` removes exactly those entries.
"""
import datetime as dt
import json
import os
import shlex
import shutil
import subprocess
import sys


def claude_dir():
    return os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")


def kifu_command():
    """How Claude Code should start kifu: the installed launcher, or this interpreter with -m kifu."""
    found = shutil.which("kifu")
    return [found] if found else [sys.executable, "-m", "kifu"]


HOOKS = {
    "SessionStart": {"matcher": "startup", "args": ["hook", "session-start"], "timeout": 10},
    "SessionEnd": {"matcher": None, "args": ["hook", "session-end"], "timeout": 5},
}


def _is_kifu_hook(entry, event):
    marker = " ".join(HOOKS[event]["args"])
    return any(marker in h.get("command", "") and "kifu" in h.get("command", "") for h in entry.get("hooks", []))


def plan_settings(settings, uninstall=False):
    """The settings with kifu's hooks added (or removed). Pure: returns a new dict and a list of changes."""
    settings = json.loads(json.dumps(settings))
    hooks = settings.setdefault("hooks", {})
    changes = []
    base = kifu_command()
    for event, spec in HOOKS.items():
        entries = hooks.get(event, [])
        kept = [e for e in entries if not _is_kifu_hook(e, event)]
        if uninstall:
            if len(kept) != len(entries):
                changes.append(f"remove {event} hook")
            hooks[event] = kept
            if not kept:
                hooks.pop(event)
            continue
        entry = {"hooks": [{"type": "command", "command": shlex.join(base + spec["args"]), "timeout": spec["timeout"]}]}
        if spec["matcher"]:
            entry = {"matcher": spec["matcher"], **entry}
        if entry not in entries:
            changes.append(f"{'update' if len(kept) != len(entries) else 'add'} {event} hook: {entry['hooks'][0]['command']}")
            hooks[event] = kept + [entry]
    if not hooks:
        settings.pop("hooks")
    return settings, changes


def _mcp_registered():
    try:
        r = subprocess.run(["claude", "mcp", "get", "kifu"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else ""


def install(uninstall=False, dry_run=False, log=print):
    path = os.path.join(claude_dir(), "settings.json")
    current = {}
    if os.path.exists(path):
        with open(path) as fh:
            current = json.load(fh)
    new, changes = plan_settings(current, uninstall=uninstall)
    for change in changes:
        log(("would " if dry_run else "") + change)
    if changes and not dry_run:
        if os.path.exists(path):
            backup = f"{path}.bak-pre-kifu-{dt.date.today().isoformat()}"
            if not os.path.exists(backup):
                shutil.copy2(path, backup)
                log(f"backup: {backup}")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(new, fh, indent=2)
            fh.write("\n")
    if not changes:
        log("hooks: nothing to change")

    registered = _mcp_registered()
    if registered is None:
        log("mcp: the claude CLI is not on PATH; register it yourself: claude mcp add -s user kifu -- "
            + shlex.join(kifu_command() + ["mcp"]))
        return
    command = kifu_command() + ["mcp"]
    if uninstall:
        if registered:
            log(("would " if dry_run else "") + "remove MCP server kifu")
            if not dry_run:
                subprocess.run(["claude", "mcp", "remove", "-s", "user", "kifu"], capture_output=True, text=True)
        return
    if registered and all(part in registered for part in command):
        log("mcp: kifu already registered")
        return
    log(("would " if dry_run else "") + f"register MCP server kifu (user scope): {shlex.join(command)}")
    if not dry_run:
        if registered:
            subprocess.run(["claude", "mcp", "remove", "-s", "user", "kifu"], capture_output=True, text=True)
        r = subprocess.run(["claude", "mcp", "add", "-s", "user", "kifu", "--", *command], capture_output=True, text=True)
        log(r.stdout.strip() or r.stderr.strip())
