"""Telegram inline-menu handler for the /cred command.

Intercepts /cred BEFORE the gateway's text dispatcher so the user gets a
button picker instead of a plaintext response. Also intercepts
``cred:*`` callback queries from the buttons.

Wiring (mirror of telegram_laira_vc_menu hooks):

  In gateway.platforms.telegram._handle_callback_query (early):
      from gateway.platforms.telegram_cred_menu import try_handle_callback
      if await try_handle_callback(query, context):
          return

  In gateway.platforms.telegram._handle_text_message (early):
      from gateway.platforms.telegram_cred_menu import try_handle_command
      if await try_handle_command(update, context):
          return

Owner-gated: only Giannis (Telegram 413720629) sees the menu.  Everyone
else gets a polite refusal.
"""

from __future__ import annotations

import logging
from typing import Any, List, Tuple

logger = logging.getLogger("hermes.telegram.cred-menu")

GIANNIS_TG_ID = 413720629


def _to_telegram_markup(rows: List[List[Tuple[str, str]]]):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    kb_rows = []
    for row in rows:
        kb_rows.append([InlineKeyboardButton(text, callback_data=cb) for text, cb in row])
    return InlineKeyboardMarkup(kb_rows)


async def _render_menu(target, *, chat_id: str, global_scope: bool, edit: bool = False) -> None:
    from gateway.cred_menu import build_menu_payload

    text, rows, _ = build_menu_payload(
        platform="telegram",
        chat_id=str(chat_id),
        global_scope=global_scope,
    )
    markup = _to_telegram_markup(rows)
    try:
        if edit:
            await target.edit_message_text(text=text, reply_markup=markup, parse_mode="Markdown")
        else:
            await target.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    except Exception:
        logger.exception("telegram /cred render failed")


async def try_handle_command(update, context) -> bool:
    """Return True if this update was a /cred command and we handled it."""
    msg = getattr(update, "message", None)
    if not msg or not msg.text:
        return False
    text = msg.text.strip()

    if not (text == "/cred" or text.startswith("/cred ") or text.startswith("/cred@")):
        return False

    user = getattr(msg, "from_user", None)
    uid = getattr(user, "id", None) if user else None
    if uid != GIANNIS_TG_ID:
        try:
            await msg.reply_text("🔒 /cred is restricted to Giannis.")
        except Exception:
            pass
        return True

    chat_id = str(getattr(msg, "chat_id", "") or "")
    await _render_menu(msg, chat_id=chat_id, global_scope=False, edit=False)
    return True


async def try_handle_callback(query, context) -> bool:
    """Return True if this callback_data starts with 'cred:' and we handled it."""
    data = getattr(query, "data", None) or ""
    if not data.startswith("cred:"):
        return False

    user = getattr(query, "from_user", None)
    uid = getattr(user, "id", None) if user else None
    if uid != GIANNIS_TG_ID:
        try:
            await query.answer(text="denied", show_alert=True)
        except Exception:
            pass
        return True

    chat_id = str(getattr(getattr(query, "message", None), "chat_id", "") or "")

    from gateway.cred_menu import apply_callback
    toast, global_after, ok = apply_callback(
        payload=data,
        platform="telegram",
        chat_id=chat_id,
    )

    try:
        await query.answer(text=toast or ("ok" if ok else "error"))
    except Exception:
        pass

    # Re-render the menu in the new scope.
    await _render_menu(query, chat_id=chat_id, global_scope=global_after, edit=True)
    return True
