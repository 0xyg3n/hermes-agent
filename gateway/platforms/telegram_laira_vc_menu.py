"""Laira VC inline-menu handler for the Telegram gateway platform.

A sidecar module that intercepts `/lvc` (laira-vc) commands and `lvc:*`
callback queries BEFORE they reach the agent. Renders an inline keyboard
that calls `laira-vc` CLI subcommands and the page-server `/steer` endpoint.

Two integration points in `gateway/platforms/telegram.py`:

  1. In `_handle_command` (or _handle_text_message), at the very top:
        from gateway.platforms.telegram_laira_vc_menu import try_handle_command
        if await try_handle_command(update, context):
            return

  2. In `_handle_callback_query`, before the existing prefix matchers:
        from gateway.platforms.telegram_laira_vc_menu import try_handle_callback
        if await try_handle_callback(query, context):
            return

Both helpers are no-ops for non-laira-vc traffic, so they're safe to leave
in place permanently.

Allowed user: Giannis (Telegram ID 413720629). All other users get a polite
refusal with no menu rendered.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger("hermes.telegram.laira-vc-menu")

# ─────────────────────── config ───────────────────────

LAIRA_VC = os.environ.get("LAIRA_VC_BIN", "/home/node/.local/bin/laira-vc")
PAGE_SERVER = os.environ.get("LAIRA_PAGE_URL", "https://127.0.0.1:8765")
STEER_TOKEN_PATH = Path("/home/node/.config/laira-vc/steer.env")
PENDING_DIR = Path("/tmp/laira-vc-menu-pending")
AUDIT_LOG = Path("/home/node/.local/state/laira-vc-menu.audit.jsonl")

GIANNIS_TG_ID = 413720629
ALLOWED_USERS = {GIANNIS_TG_ID}

CHANNEL_PRESETS = [
    ("1499544122065813647", "Lock In (OpenOmega)"),
    ("1484916957957586974", "Lounge (OpenCheeks)"),
]

PENDING_DIR.mkdir(parents=True, exist_ok=True)
try:
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
except OSError:
    pass


# ─────────────────────── audit ───────────────────────

def _audit(user_id: int, action: str, **extra: Any) -> None:
    rec = {"ts": time.time(), "user_id": user_id, "action": action, **extra}
    try:
        AUDIT_LOG.open("a").write(json.dumps(rec) + "\n")
    except OSError:
        pass


# ─────────────────────── shell + steer ───────────────────────

def _run_laira_vc(*args: str, timeout: int = 60) -> tuple[int, str]:
    if not Path(LAIRA_VC).exists():
        return 1, f"laira-vc CLI not found at {LAIRA_VC}"
    try:
        out = subprocess.run(
            [LAIRA_VC, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return out.returncode, (out.stdout + out.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"laira-vc {' '.join(args)} timed out after {timeout}s"
    except OSError as exc:
        return 1, f"failed to run laira-vc: {exc}"


def _post_steer(text: str, speak: bool = True) -> tuple[bool, str]:
    if not STEER_TOKEN_PATH.exists():
        return False, "steer token file missing"
    token = ""
    for line in STEER_TOKEN_PATH.read_text().splitlines():
        if line.strip().startswith("LAIRA_STEER_TOKEN="):
            token = line.split("=", 1)[1].strip().strip('"').strip("'")
            break
    if not token:
        return False, "LAIRA_STEER_TOKEN not in steer.env"
    try:
        with httpx.Client(verify=False, timeout=10.0) as cl:
            r = cl.post(
                f"{PAGE_SERVER}/steer",
                json={
                    "id": "1085530082803716118",
                    "sender": "giannis-via-tg-menu",
                    "text": text,
                    "speak": speak,
                },
                headers={"X-Steer-Token": token},
            )
        if r.status_code == 200:
            return True, "steered"
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as exc:
        return False, f"steer error: {exc}"


def _is_muted() -> bool:
    """Check if openai_output sink is muted (the realtime brain's audio path)."""
    try:
        r = subprocess.run(
            ["pactl", "get-sink-mute", "openai_output"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0 and "yes" in r.stdout.lower():
            return True
    except Exception:
        pass
    # Fallback: legacy file (covers Anthropic/Piper path if brain is down)
    return Path("/tmp/laira-voice-muted").exists()


def _set_muted(yes: bool) -> tuple[bool, str]:
    """Mute/unmute the realtime brain's outgoing audio.

    Sets pactl mute on openai_output sink (cuts Chromium → stay.js → Discord).
    Also touches/removes /tmp/laira-voice-muted for the legacy fallback path.
    Returns (success, info).
    """
    flag = "1" if yes else "0"
    try:
        r = subprocess.run(
            ["pactl", "set-sink-mute", "openai_output", flag],
            capture_output=True, text=True, timeout=5,
        )
        ok = r.returncode == 0
        info = r.stderr.strip() or r.stdout.strip() or ("muted" if yes else "unmuted")
    except Exception as exc:
        ok = False
        info = f"pactl error: {exc}"

    # Also handle the legacy file for the Anthropic/Piper fallback
    p = Path("/tmp/laira-voice-muted")
    if yes:
        try: p.touch()
        except OSError: pass
    else:
        try: p.unlink()
        except FileNotFoundError: pass

    return ok, info


def _get_log_tail(component: str, n: int = 40) -> str:
    paths = {
        "page": Path("/home/node/.local/state/laira-vc-page.log"),
        "browser": Path("/home/node/.local/state/laira-vc-browser.log"),
        "supervisor": Path("/home/node/.local/state/laira-vc-supervisor.log"),
        "fix-mic": Path("/tmp/laira-vc-fix-mic.log"),
    }
    if component == "stay":
        candidates = sorted(
            Path("/tmp").glob("stay-voice-*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            return "(no stay log yet)"
        path = candidates[0]
    else:
        path = paths.get(component)
        if not path:
            return f"(unknown component: {component})"
    if not path.exists():
        return f"(log not found: {path})"
    try:
        with path.open("r", errors="replace") as f:
            lines = f.readlines()[-n:]
        return "".join(lines) or "(empty)"
    except OSError as exc:
        return f"(read error: {exc})"


# ─────────────────────── pending input state ───────────────────────

def _set_pending(user_id: int, kind: str) -> None:
    PENDING_DIR.joinpath(f"{user_id}.json").write_text(
        json.dumps({"kind": kind, "ts": time.time()})
    )


def _take_pending(user_id: int) -> Optional[dict]:
    p = PENDING_DIR.joinpath(f"{user_id}.json")
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        try: p.unlink()
        except FileNotFoundError: pass
        return None
    if time.time() - data.get("ts", 0) > 300:  # 5 min expiry
        try: p.unlink()
        except FileNotFoundError: pass
        return None
    try: p.unlink()
    except FileNotFoundError: pass
    return data


# ─────────────────────── keyboards (telegram InlineKeyboardMarkup-shaped dicts) ───────────────────────

def _main_menu_kb() -> dict:
    muted_label = "🔊 Unmute" if _is_muted() else "🔇 Mute"
    return {
        "inline_keyboard": [
            [{"text": "📊 Status",   "callback_data": "lvc:status"},
             {"text": "🩹 Heal",     "callback_data": "lvc:heal"}],
            [{"text": "⬆️ Up",      "callback_data": "lvc:up"},
             {"text": "⬇️ Down",    "callback_data": "lvc:down"},
             {"text": "🔄 Restart", "callback_data": "lvc:restart_confirm"}],
            [{"text": "🚪 Leave VC",  "callback_data": "lvc:leave"},
             {"text": muted_label,  "callback_data": "lvc:mute_toggle"}],
            [{"text": "🎤 Channel ▾", "callback_data": "lvc:channel_menu"},
             {"text": "📋 Logs ▾",    "callback_data": "lvc:logs_menu"}],
            [{"text": "🎯 Join Giannis VC", "callback_data": "lvc:join_giannis"}],
            [{"text": "🤖 Talk to Laira", "callback_data": "lvc:steer_prompt"}],
            [{"text": "🔁 Reload", "callback_data": "lvc:menu"}],
        ]
    }


def _channel_menu_kb() -> dict:
    rows = [
        [{"text": f"→ {label}", "callback_data": f"lvc:switch:{cid}"}]
        for cid, label in CHANNEL_PRESETS
    ]
    rows.append([{"text": "⌨️ Custom channel ID", "callback_data": "lvc:switch_prompt"}])
    rows.append([{"text": "← back", "callback_data": "lvc:menu"}])
    return {"inline_keyboard": rows}


def _logs_menu_kb() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "page",       "callback_data": "lvc:log:page"},
             {"text": "browser",    "callback_data": "lvc:log:browser"}],
            [{"text": "stay.js",    "callback_data": "lvc:log:stay"},
             {"text": "supervisor", "callback_data": "lvc:log:supervisor"}],
            [{"text": "fix-mic",    "callback_data": "lvc:log:fix-mic"}],
            [{"text": "← back",     "callback_data": "lvc:menu"}],
        ]
    }


def _confirm_kb(action: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "✅ Yes, do it", "callback_data": f"lvc:confirm:{action}"},
             {"text": "❌ Cancel",      "callback_data": "lvc:menu"}],
        ]
    }


def _to_telegram_markup(kb: dict):
    """Convert dict-shape keyboard to python-telegram-bot InlineKeyboardMarkup."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    rows = []
    for row in kb["inline_keyboard"]:
        rows.append([InlineKeyboardButton(b["text"], callback_data=b["callback_data"]) for b in row])
    return InlineKeyboardMarkup(rows)


# ─────────────────────── public entry points ───────────────────────

async def try_handle_command(update, context) -> bool:
    """Return True if this update was a /lvc or /laira command and we handled it."""
    msg = getattr(update, "message", None)
    if not msg or not msg.text:
        return False
    text = msg.text.strip()

    # Match /laira <text> — quick steer shortcut (skips the menu).
    # Forms: "/laira hello", "/laira@bot hello". Empty body shows usage.
    if text == "/laira" or text.startswith("/laira ") or text.startswith("/laira@"):
        user = getattr(msg, "from_user", None)
        uid = getattr(user, "id", None) if user else None
        if uid not in ALLOWED_USERS:
            try:
                await msg.reply_text("this command is private. ask Giannis.")
            except Exception:
                pass
            _audit(uid or 0, "denied_laira_cmd", chat_id=msg.chat_id, text=text[:80])
            return True

        # Strip the command word + optional @bot suffix
        body = text
        if body.startswith("/laira@"):
            # /laira@botname rest
            parts = body.split(" ", 1)
            body = parts[1] if len(parts) > 1 else ""
        elif body.startswith("/laira "):
            body = body[len("/laira "):]
        else:
            body = ""
        body = body.strip()

        if not body:
            try:
                await msg.reply_text(
                    "*usage:* `/laira <message>`\n_realtime Laira will speak / act on it._",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
            _audit(uid, "laira_cmd_empty")
            return True

        _audit(uid, "laira_cmd", text=body[:200])
        ok, info = _post_steer(body, speak=True)
        verdict = "✅ steered" if ok else f"❌ {info}"
        try:
            await msg.reply_text(
                f"{verdict}\n\n_text:_ {body[:300]}",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return True

    # Match /lvc, /lvc@bot, /lvc <args>, /lvc@bot <args>
    if not (text == "/lvc" or text.startswith("/lvc ") or text.startswith("/lvc@")):
        return False

    user = getattr(msg, "from_user", None)
    uid = getattr(user, "id", None) if user else None

    if uid not in ALLOWED_USERS:
        try:
            await msg.reply_text("this command is private. ask Giannis.")
        except Exception:
            pass
        _audit(uid or 0, "denied", chat_id=msg.chat_id, text=text[:80])
        return True

    _audit(uid, "menu_open")

    # Render main menu
    try:
        from telegram.constants import ParseMode
        await msg.reply_text(
            "*Laira realtime VC control*\n_pick an action:_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_to_telegram_markup(_main_menu_kb()),
        )
    except Exception:
        logger.exception("failed to render lvc menu")
    return True


async def try_handle_callback(query, context) -> bool:
    """Return True if this callback_data starts with 'lvc:' and we handled it."""
    data = getattr(query, "data", None) or ""
    if not data.startswith("lvc:"):
        return False

    user = getattr(query, "from_user", None)
    uid = getattr(user, "id", None) if user else None

    if uid not in ALLOWED_USERS:
        try:
            await query.answer(text="denied", show_alert=True)
        except Exception:
            pass
        _audit(uid or 0, "denied_cb", data=data)
        return True

    _audit(uid, "callback", data=data)

    payload = data[len("lvc:"):]  # strip prefix
    try:
        await _dispatch_callback(query, payload)
    except Exception:
        logger.exception("lvc callback failed: %s", payload)
        try:
            await query.answer(text="error — see hermes logs", show_alert=False)
        except Exception:
            pass
    return True


# Also intercept text messages that follow a "give me input" prompt.
async def try_handle_pending_input(update, context) -> bool:
    """Return True if this text was the answer to a pending lvc prompt and we consumed it."""
    msg = getattr(update, "message", None)
    if not msg or not msg.text:
        return False
    user = getattr(msg, "from_user", None)
    uid = getattr(user, "id", None) if user else None
    if uid not in ALLOWED_USERS:
        return False

    pending = _take_pending(uid)
    if not pending:
        return False

    text = msg.text.strip()
    kind = pending["kind"]

    if kind == "switch":
        if not text.isdigit():
            try: await msg.reply_text("channel id should be all digits. cancelled.")
            except Exception: pass
            return True
        _audit(uid, "switch_typed", channel_id=text)
        try: await msg.reply_text(f"switching to `{text}`…", parse_mode="Markdown")
        except Exception: pass
        rc, out = _run_laira_vc("switch", text, timeout=90)
        try:
            from telegram.constants import ParseMode
            await msg.reply_text(
                f"```\n{out[-3500:]}\n```\n_(rc={rc})_",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_to_telegram_markup(_main_menu_kb()),
            )
        except Exception:
            pass
        return True

    if kind == "steer":
        _audit(uid, "steer_typed", text=text[:200])
        ok, info = _post_steer(text, speak=True)
        verdict = "✅ steered" if ok else f"❌ {info}"
        try:
            await msg.reply_text(
                f"{verdict}\n\n_text:_ {text[:300]}",
                parse_mode="Markdown",
                reply_markup=_to_telegram_markup(_main_menu_kb()),
            )
        except Exception:
            pass
        return True

    return False


# ─────────────────────── callback dispatcher ───────────────────────

async def _dispatch_callback(query, payload: str) -> None:
    from telegram.constants import ParseMode

    async def _edit(text: str, kb: Optional[dict] = None):
        markup = _to_telegram_markup(kb) if kb else None
        try:
            if len(text) > 4000:
                text = text[:3990] + "\n…(truncated)"
            await query.edit_message_text(
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=markup,
                disable_web_page_preview=True,
            )
        except Exception:
            logger.exception("edit_message_text failed")

    async def _ack(text: str = "", alert: bool = False):
        try:
            await query.answer(text=text[:200], show_alert=alert)
        except Exception:
            pass

    user = getattr(query, "from_user", None)
    uid = getattr(user, "id", None) if user else None

    # Main menu
    if payload == "menu":
        await _ack()
        await _edit("*Laira realtime VC control*\n_pick an action:_", _main_menu_kb())
        return

    if payload == "status":
        await _ack("checking…")
        rc, out = _run_laira_vc("status")
        await _edit(f"```\n{out[-3500:]}\n```", _main_menu_kb())
        return

    if payload == "heal":
        await _ack("healing…")
        loop = asyncio.get_event_loop()
        rc, out = await loop.run_in_executor(None, _run_laira_vc, "heal", 120)
        await _edit(f"*heal (rc={rc}):*\n```\n{out[-3500:]}\n```", _main_menu_kb())
        return

    if payload == "up":
        await _ack("bringing up…")
        loop = asyncio.get_event_loop()
        rc, out = await loop.run_in_executor(None, _run_laira_vc, "up", 120)
        await _edit(f"*up (rc={rc}):*\n```\n{out[-3500:]}\n```", _main_menu_kb())
        return

    if payload == "down":
        await _ack("shutting down…")
        rc, out = _run_laira_vc("down", timeout=60)
        await _edit(f"*down (rc={rc}):*\n```\n{out[-3500:]}\n```", _main_menu_kb())
        return

    if payload == "leave":
        await _ack("leaving VC…")
        rc, out = _run_laira_vc("leave", timeout=30)
        await _edit(f"*leave (rc={rc}):*\n```\n{out[-3500:]}\n```", _main_menu_kb())
        return

    if payload == "where":
        await _ack("looking up…")
        rc, out = _run_laira_vc("where", timeout=15)
        await _edit(f"*where (rc={rc}):*\n```\n{out[-3500:]}\n```", _main_menu_kb())
        return

    if payload == "join_giannis":
        await _ack("finding Giannis…")
        rc, out = _run_laira_vc("where", timeout=15)
        # Parse "FOUND: ... channel=<name> (<channel_id>) ..."
        import re
        m = re.search(r'channel=.*?\((\d+)\)', out)
        if not m:
            await _edit(
                f"❌ Giannis isn't in any voice channel I can see.\n\n```\n{out[-2000:]}\n```",
                _main_menu_kb(),
            )
            return
        target_channel = m.group(1)
        # Skip if already there
        from pathlib import Path as _P
        roster = _P("/tmp/discord-voice-roster.json")
        current = ""
        if roster.exists():
            try:
                current = json.loads(roster.read_text()).get("channel_id", "")
            except Exception:
                pass
        if current == target_channel:
            await _edit(
                f"✅ Already in his channel (`{target_channel}`).\n\n```\n{out[-1500:]}\n```",
                _main_menu_kb(),
            )
            return
        await _edit(f"found him in `{target_channel}` — switching…", _main_menu_kb())
        loop = asyncio.get_event_loop()
        rc2, out2 = await loop.run_in_executor(None, _run_laira_vc, "switch", target_channel, 90)
        await _edit(
            f"*joined Giannis (rc={rc2}):*\n```\n{out2[-3000:]}\n```",
            _main_menu_kb(),
        )
        return

    if payload == "mute_toggle":
        cur = _is_muted()
        ok, info = _set_muted(not cur)
        verdict = ("muted 🔇" if not cur else "unmuted 🔊") if ok else f"⚠️ failed: {info}"
        await _ack(verdict)
        await _edit(
            f"*{verdict}*\n\n_(openai_output sink {'mute=1' if not cur and ok else 'mute=0' if ok else 'unchanged'})_",
            _main_menu_kb(),
        )
        return

    # Confirmations
    if payload == "restart_confirm":
        await _ack()
        await _edit("*Restart the entire stack?*\n_(this is a hard restart)_", _confirm_kb("restart"))
        return

    if payload == "confirm:restart":
        await _ack("restarting…")
        loop = asyncio.get_event_loop()
        rc, out = await loop.run_in_executor(None, _run_laira_vc, "restart", 180)
        await _edit(f"*restart (rc={rc}):*\n```\n{out[-3500:]}\n```", _main_menu_kb())
        return

    # Channel submenu
    if payload == "channel_menu":
        await _ack()
        await _edit("*Switch channel:*", _channel_menu_kb())
        return

    if payload.startswith("switch:"):
        cidn = payload[len("switch:"):]
        await _ack(f"switching to {cidn[:8]}…")
        loop = asyncio.get_event_loop()
        rc, out = await loop.run_in_executor(None, _run_laira_vc, "switch", cidn, 90)
        await _edit(f"*switch (rc={rc}):*\n```\n{out[-3500:]}\n```", _main_menu_kb())
        return

    if payload == "switch_prompt":
        if uid:
            _set_pending(uid, "switch")
        await _ack("waiting for channel id…")
        await _edit(
            "send the *channel id* as your next message (digits only). 5 min timeout.",
            _main_menu_kb(),
        )
        return

    # Logs submenu
    if payload == "logs_menu":
        await _ack()
        await _edit("*Tail logs:*", _logs_menu_kb())
        return

    if payload.startswith("log:"):
        comp = payload[len("log:"):]
        await _ack(f"loading {comp}…")
        body = _get_log_tail(comp, n=40)
        await _edit(f"*log: {comp} (last 40 lines)*\n```\n{body[-3500:]}\n```", _logs_menu_kb())
        return

    # Steer prompt
    if payload == "steer_prompt":
        if uid:
            _set_pending(uid, "steer")
        await _ack("waiting for steer text…")
        await _edit(
            "*Talk to Laira:*\n_send your next message and Laira will speak it / act on it via /steer (speak=true). 5 min timeout._",
            _main_menu_kb(),
        )
        return

    # Unknown
    await _ack("unknown action")
