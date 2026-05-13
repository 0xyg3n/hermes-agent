"""Shared helpers for the /cred inline menu (Telegram + Discord).

Builds the message body and button layout that both platform sidecars
use, so the picker looks and behaves identically across Telegram and
Discord.

Callback-data format (kept short, <64 bytes for Telegram):

    cred:set:<provider>:<label>      → set chat-scoped pin
    cred:setg:<provider>:<label>     → set global pin
    cred:reset:<provider>            → clear chat pin
    cred:resetg:<provider>           → clear global pin
    cred:refresh:<provider>          → re-render current scope
    cred:scope:<provider>            → toggle chat<->global view
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

logger = logging.getLogger("hermes.gateway.cred_menu")

# Numeric IDs allowed to use the menu. Must match
# GatewayRunner._CRED_OWNER_IDS in gateway/run.py.
GIANNIS_TELEGRAM_ID = "413720629"
GIANNIS_DISCORD_ID = "1085530082803716118"


def is_owner(platform: str, user_id: object) -> bool:
    """Return True only if user_id is Giannis on a recognised platform."""
    p = (platform or "").strip().lower()
    uid = str(user_id or "").strip()
    if not p or not uid:
        return False
    if p == "telegram":
        return uid == GIANNIS_TELEGRAM_ID
    if p == "discord":
        return uid == GIANNIS_DISCORD_ID
    return False


def resolve_active_provider() -> str:
    """Pull the active inference provider for the running gateway profile."""
    try:
        from gateway.run import _resolve_runtime_agent_kwargs  # type: ignore
        runtime = _resolve_runtime_agent_kwargs()
        return (runtime.get("provider") or "").strip().lower()
    except Exception as exc:
        logger.debug("resolve_active_provider failed: %s", exc)
        return ""


def build_menu_payload(
    *,
    platform: str,
    chat_id: str,
    global_scope: bool = False,
) -> Tuple[str, List[List[Tuple[str, str]]], str]:
    """Build the menu body and rows for the active provider.

    Returns ``(text, rows, provider)`` where ``rows`` is a list of rows
    of ``(label, callback_data)`` tuples.  Empty provider or empty pool
    surfaces as a plain status message with just a Refresh button.
    """
    from agent.credential_pool import load_pool
    from agent import credential_pins

    provider = resolve_active_provider()
    if not provider:
        text = "⚠️ Could not resolve the active provider. Check `~/.hermes/config.yaml` model section."
        return text, [[("🔄 Refresh", "cred:refresh:_")]], ""

    try:
        pool = load_pool(provider)
    except Exception as exc:
        text = f"⚠️ Could not load credential pool for `{provider}`: {exc}"
        return text, [[("🔄 Refresh", f"cred:refresh:{provider}")]], provider

    entries = pool.entries() if hasattr(pool, "entries") else []
    if not entries:
        text = f"No credential pool entries for `{provider}`. Run `hermes auth add {provider}` first."
        return text, [[("🔄 Refresh", f"cred:refresh:{provider}")]], provider

    scope_label = "global" if global_scope else f"{platform}:{chat_id}"
    scope_platform = None if global_scope else platform
    scope_chat = None if global_scope else chat_id
    current_pin = credential_pins.get_pin(provider, scope_platform, scope_chat)

    lines = [
        f"💳 *Credential picker — {provider}*",
        f"Scope: `{scope_label}`",
        f"Current pin: " + (f"`{current_pin}`" if current_pin else "_none — strategy selects automatically_"),
        "",
        "Tap a label to pin it. Toggle scope to switch chat / global.",
    ]
    text = "\n".join(lines)

    set_prefix = "cred:setg" if global_scope else "cred:set"
    reset_cb = f"cred:resetg:{provider}" if global_scope else f"cred:reset:{provider}"

    rows: List[List[Tuple[str, str]]] = []
    for entry in entries:
        label = (getattr(entry, "label", "") or "").strip()
        if not label:
            continue
        marker = "● " if current_pin and current_pin.lower() == label.lower() else "○ "
        rows.append([(f"{marker}{label}", f"{set_prefix}:{provider}:{label}")])

    rows.append([
        ("🚫 Reset (round-robin)", reset_cb),
    ])
    rows.append([
        (
            "🌍 Switch to global" if not global_scope else "💬 Switch to this chat",
            f"cred:scope:{provider}" if not global_scope else f"cred:scope:{provider}:chat",
        ),
        ("🔄 Refresh", f"cred:refresh:{provider}" if not global_scope else f"cred:refreshg:{provider}"),
    ])

    return text, rows, provider


def apply_callback(
    *,
    payload: str,
    platform: str,
    chat_id: str,
) -> Tuple[str, bool, bool]:
    """Mutate the pin store based on a callback payload.

    Returns ``(toast, global_scope_after, ok)``.  ``toast`` is a short
    status string suitable for a Telegram callback answer / Discord
    interaction response.  ``global_scope_after`` is the scope the menu
    should re-render in.  ``ok`` is False on hard failures (no provider,
    bad payload).
    """
    from agent import credential_pins

    parts = (payload or "").split(":")
    # Strip leading "cred:" if caller didn't.
    if parts and parts[0] == "cred":
        parts = parts[1:]
    if not parts:
        return ("bad payload", False, False)

    op = parts[0]
    provider = parts[1] if len(parts) > 1 else ""

    # Defaults
    is_global = False

    if op == "set" and len(parts) >= 3:
        label = ":".join(parts[2:])
        credential_pins.set_pin(provider, platform, chat_id, label)
        return (f"pinned {label}", False, True)

    if op == "setg" and len(parts) >= 3:
        label = ":".join(parts[2:])
        credential_pins.set_pin(provider, None, None, label)
        return (f"pinned {label} globally", True, True)

    if op == "reset" and len(parts) >= 2:
        credential_pins.clear_pin(provider, platform, chat_id)
        return ("cleared chat pin", False, True)

    if op == "resetg" and len(parts) >= 2:
        credential_pins.clear_pin(provider, None, None)
        return ("cleared global pin", True, True)

    if op == "scope" and len(parts) >= 2:
        # If a 3rd part is "chat" → switch to chat scope, else flip to global.
        target = parts[2] if len(parts) >= 3 else ""
        if target == "chat":
            return ("scope: chat", False, True)
        return ("scope: global", True, True)

    if op == "refresh":
        return ("refreshed", False, True)
    if op == "refreshg":
        return ("refreshed (global)", True, True)

    return ("unknown action", False, False)
