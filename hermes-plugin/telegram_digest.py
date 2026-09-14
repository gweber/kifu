"""The digest on Telegram with buttons: Done, Dismiss and Brief under each idea, answered inside the gateway.

A cron job can only deliver text, so with buttons the plugin sends the digest itself: at connect, Hermes hands the
plugin its Telegram application; the plugin adds a button handler scoped to "kifu:" callbacks (Hermes' own buttons
keep working) and a small scheduler that sends the digest at the configured time.

`hermes kifu setup --buttons` turns this on: it writes the state file below and removes the text-only cron job, so
the digest is never sent twice. The schedule is a cron expression ("0 18 * * 0", minute and hour with a weekday or
*) or "auto": kifu's /api/habits/slot, the hour you most often start coding sessions.

Buttons answer only in the chat the digest goes to.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import urllib.parse
from pathlib import Path

logger = logging.getLogger("kifu")

PREFIX = "kifu:"
STATE_NAME = "kifu_digest.json"
ACTIONS = {"d": "done", "x": "dismissed", "b": "brief"}
TELEGRAM_LIMIT = 4000

_task = None


def _client():
    try:
        from . import kifu_client
    except ImportError:
        import kifu_client  # type: ignore
    return kifu_client


def state_path() -> Path:
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "plugins-data" / STATE_NAME


def load_state() -> dict:
    try:
        return json.loads(state_path().read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(path)


def chat_of(deliver: str | None) -> str | None:
    """telegram:<chat id>[:<thread>] -> chat id."""
    if not deliver or not deliver.startswith("telegram:"):
        return None
    return deliver.split(":")[1] or None


def token(anchor: str) -> str:
    """Callback data is limited to 64 bytes; anchors can be longer. Tokens map back through the state file."""
    return hashlib.sha1(anchor.encode()).hexdigest()[:12]


def next_run(schedule: str, now: dt.datetime, slot: dict | None = None) -> dt.datetime | None:
    """The next time a "m h * * dow" (dow a number, a list, or *) schedule fires after now (aware, local time);
    for "auto", kifu's slot. Other cron forms are not supported and return None."""
    if schedule == "auto":
        if not slot or not slot.get("next"):
            return None
        at = dt.datetime.fromisoformat(slot["next"])
        return at if at > now else at + dt.timedelta(days=7)
    parts = schedule.split()
    if len(parts) != 5 or parts[2:4] != ["*", "*"] or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    minute, hour = int(parts[0]), int(parts[1])
    days = {int(d) % 7 for d in parts[4].split(",")} if parts[4] != "*" else set(range(7))
    base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    for offset in range(8):
        at = base + dt.timedelta(days=offset)
        if at > now and (at.isoweekday() % 7) in days:        # cron: 0 = Sunday
            return at
    return None


def build_messages(digest: dict) -> tuple[str, list[dict]]:
    """The header text, and one {text, buttons: [(label, data)]} per idea."""
    items = digest.get("items") or []
    header = "kifu · " + ((digest.get("text") or "").splitlines() or [f"{len(items)} idea(s) went quiet"])[0]
    out = []
    for i in items:
        lines = [f"• {i['title']}", f"{i['project']} · {i['status']} · quiet {i['quiet_days']} days"]
        lines += [f"  – {x}" for x in (i.get("loose_ends") or [])[:2]]
        t = token(i["anchor"])
        out.append({"anchor": i["anchor"], "text": "\n".join(lines),
                    "buttons": [("✓ Done", f"{PREFIX}d:{t}"), ("✗ Dismiss", f"{PREFIX}x:{t}"), ("📄 Brief", f"{PREFIX}b:{t}")]})
    return header, out


async def send_digest(bot, chat_id: str, quiet_days: int, limit: int = 5) -> int:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    client = _client()
    status, digest = await asyncio.to_thread(client.get, "/api/digest", quiet_days=quiet_days, limit=limit)
    if status != 200 or not digest.get("items"):
        return 0
    header, messages = build_messages(digest)
    state = load_state()
    tokens = state.setdefault("tokens", {})
    await bot.send_message(chat_id=chat_id, text=header)
    for m in messages:
        tokens[token(m["anchor"])] = m["anchor"]
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data) for label, data in m["buttons"]]])
        await bot.send_message(chat_id=chat_id, text=m["text"], reply_markup=keyboard)
    if len(tokens) > 500:
        state["tokens"] = dict(list(tokens.items())[-500:])
    save_state(state)
    return len(messages)


async def handle_button(data: str, chat_id: str, state: dict) -> tuple[str | None, str | None]:
    """What a tap does: (text replacing the idea's message or None, a reply to send or None)."""
    if chat_id != chat_of(state.get("deliver")):
        return None, None
    _, _, rest = data.partition(PREFIX)
    code, _, tok = rest.partition(":")
    action = ACTIONS.get(code)
    anchor = state.get("tokens", {}).get(tok)
    if not action or not anchor:
        return None, "kifu: this button is from an older digest"
    client = _client()
    path = f"/api/lines/{urllib.parse.quote(anchor, safe='')}"
    if action == "brief":
        status, out = await asyncio.to_thread(client.get, f"{path}/brief")
        if status != 200:
            return None, f"kifu: no brief ({status})"
        text = out["brief"]
        return None, text if len(text) <= TELEGRAM_LIMIT else text[:TELEGRAM_LIMIT - 1] + "…"
    status, out = await asyncio.to_thread(client.request, "PUT", f"{path}/mark",
                                          body={"state": action, "note": "from the Telegram digest"})
    if status != 200:
        return None, f"kifu: could not mark it ({status})"
    return ("✓ done" if action == "done" else "✗ dismissed"), None


async def on_button(update, context) -> None:
    query = update.callback_query
    message = query.message
    replaced, reply = await handle_button(query.data or "", str(message.chat.id), load_state())
    await query.answer()
    if replaced:
        await query.edit_message_text(text=f"{message.text}\n\n{replaced}", reply_markup=None)
    if reply:
        await context.bot.send_message(chat_id=message.chat.id, text=reply, reply_to_message_id=message.message_id)


async def scheduler(bot) -> None:
    """Sleep until the next digest time, send, repeat. Reads the state each round, so setup changes apply."""
    client = _client()
    while True:
        state = load_state()
        chat = chat_of(state.get("deliver"))
        if state.get("mode") != "buttons" or not chat:
            await asyncio.sleep(3600)
            continue
        now = dt.datetime.now(dt.UTC).astimezone()
        slot = None
        if state.get("schedule") == "auto":
            status, slot = await asyncio.to_thread(client.get, "/api/habits/slot")
            slot = slot if status == 200 else None
        at = next_run(state.get("schedule") or "0 18 * * 0", now, slot)
        if at is None:
            logger.warning("kifu: digest schedule %r is not understood; retrying in an hour", state.get("schedule"))
            await asyncio.sleep(3600)
            continue
        await asyncio.sleep(min(max(0.0, (at - dt.datetime.now(dt.UTC)).total_seconds()), 6 * 3600))
        if dt.datetime.now(dt.UTC) < at:
            continue                        # woke early to pick up changes; not time yet
        last = state.get("last_sent")
        if last and dt.datetime.fromisoformat(last) > at - dt.timedelta(hours=1):
            continue                        # already sent for this slot (a restart near the time)
        try:
            sent = await send_digest(bot, chat, int(state.get("quiet_days") or 21))
            state = load_state()
            state["last_sent"] = dt.datetime.now(dt.UTC).isoformat()
            save_state(state)
            logger.info("kifu: digest sent, %d idea(s)", sent)
        except Exception as exc:  # the next slot tries again
            logger.warning("kifu: digest failed: %s", exc)
            await asyncio.sleep(600)


def telegram_factory(app, adapter=None) -> None:
    """Called by Hermes at each Telegram connect: register the scoped button handler and start the scheduler once."""
    global _task
    from telegram.ext import CallbackQueryHandler

    app.add_handler(CallbackQueryHandler(on_button, pattern=f"^{PREFIX}"))
    if _task is None or _task.done():
        try:
            _task = asyncio.get_running_loop().create_task(scheduler(app.bot))
        except RuntimeError:
            logger.debug("kifu: no running loop at connect; digest scheduler not started")
