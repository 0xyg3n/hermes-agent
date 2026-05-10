#!/usr/bin/env python3
"""
Chat Recall Tool — Latest-First Numbered Message History

Pulls the most recent user/assistant messages from a given chat platform
(telegram, discord, vc-realtime-giannis, etc.), numbered newest→oldest so the
agent can say "let me look at message #3" and the user knows exactly which
turn is meant.

Differs from session_search:
  - session_search returns LLM-summarized session-level recall
  - chat_recall returns RAW recent messages, numbered, no LLM cost

Use when the user asks:
  - "what did we just say"
  - "what was the last thing I told you"
  - "scroll back N messages"
  - "what message #3 again"
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Sources we treat as "chat platforms" for this tool
_CHAT_SOURCES = {
    "telegram",
    "discord",
    "vc-realtime-giannis",
    "matrix",
    "signal",
    "slack",
    "sms",
    "yuanbao",
}

# Roles worth showing — skip tool/system noise by default
_VISIBLE_ROLES = {"user", "assistant"}

# Hard cap on what we'll return in one call (context protection)
_HARD_LIMIT = 50
_DEFAULT_LIMIT = 10
_PREVIEW_CHARS = 400


def _truncate(text: str, n: int = _PREVIEW_CHARS) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"


def _format_ts(ts: Any) -> str:
    if ts is None:
        return ""
    try:
        from datetime import datetime
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
        return str(ts)
    except Exception:
        return str(ts)


def chat_recall(
    platform: Optional[str] = None,
    chat_id: Optional[str] = None,
    limit: int = _DEFAULT_LIMIT,
    include_tool_calls: bool = False,
    db=None,
    current_session_id: Optional[str] = None,
) -> str:
    """
    Return the latest N messages from a chat platform, numbered newest-first.

    Args:
        platform: One of telegram, discord, vc-realtime-giannis, matrix, signal,
                  slack, sms, yuanbao. Defaults to inferring from the current
                  session's source if not given.
        chat_id:  Optional. Restrict to a specific chat/user_id (matches
                  sessions.user_id). When omitted, returns recent messages
                  across ALL chats on that platform.
        limit:    How many messages to return (1..50, default 10).
        include_tool_calls: If True, include tool/system rows too. Default False.

    Output schema:
        {
          "success": true,
          "platform": "telegram",
          "chat_id": "413720629" or null,
          "count": 10,
          "messages": [
              {"n": 1, "role": "user",      "preview": "...", "session_id": "...", "timestamp": "..."},
              {"n": 2, "role": "assistant", "preview": "...", ...},
              ...
          ],
          "note": "Numbered newest-first. #1 is the most recent."
        }
    """
    from tools.registry import tool_error

    if db is None:
        return tool_error("Session database not available.", success=False)

    # Coerce limit
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = _DEFAULT_LIMIT
    limit = max(1, min(limit, _HARD_LIMIT))

    # Infer platform from current session if not provided
    if not platform and current_session_id:
        try:
            sess = db.get_session(current_session_id)
            if sess and sess.get("source"):
                platform = sess["source"]
        except Exception as e:
            logger.debug("Failed to infer platform from current session: %s", e)

    if not platform:
        return tool_error(
            "platform is required (telegram, discord, vc-realtime-giannis, etc.) "
            "and could not be inferred from the current session.",
            success=False,
        )

    platform = platform.strip().lower()
    if platform not in _CHAT_SOURCES:
        return tool_error(
            f"Unknown platform '{platform}'. Known: {sorted(_CHAT_SOURCES)}",
            success=False,
        )

    visible_roles = set(_VISIBLE_ROLES)
    if include_tool_calls:
        visible_roles |= {"tool", "system"}

    # Direct SQL — fastest, avoids loading whole transcripts.
    # We join messages → sessions, filter by source (and optionally user_id),
    # order by message timestamp DESC, take the last N.
    try:
        params: List[Any] = [platform]
        where = "s.source = ?"
        if chat_id:
            where += " AND s.user_id = ?"
            params.append(str(chat_id))

        # Role filter applied in SQL for efficiency
        role_placeholders = ",".join(["?"] * len(visible_roles))
        params_with_roles = list(params) + list(visible_roles) + [limit]

        sql = (
            f"SELECT m.id, m.session_id, m.role, m.content, m.timestamp, "
            f"       s.user_id, s.title "
            f"FROM messages m "
            f"JOIN sessions s ON s.id = m.session_id "
            f"WHERE {where} AND m.role IN ({role_placeholders}) "
            f"  AND m.content IS NOT NULL AND TRIM(m.content) != '' "
            f"ORDER BY m.timestamp DESC, m.id DESC "
            f"LIMIT ?"
        )

        with db._lock:
            cursor = db._conn.execute(sql, params_with_roles)
            rows = cursor.fetchall()
    except Exception as e:
        logger.error("chat_recall SQL failed: %s", e, exc_info=True)
        return tool_error(f"Database query failed: {e}", success=False)

    messages: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows, start=1):
        d = dict(row)
        # Decode content via the same path get_messages uses
        content = d.get("content") or ""
        try:
            content = db._decode_content(content)
        except Exception:
            pass
        messages.append({
            "n": idx,  # newest = 1
            "role": d.get("role", ""),
            "preview": _truncate(str(content)),
            "session_id": d.get("session_id", ""),
            "user_id": d.get("user_id", ""),
            "title": d.get("title", "") or None,
            "timestamp": _format_ts(d.get("timestamp")),
        })

    return json.dumps({
        "success": True,
        "platform": platform,
        "chat_id": chat_id,
        "count": len(messages),
        "messages": messages,
        "note": "Numbered newest-first. #1 is the most recent message; higher numbers are older. "
                "Use limit=N to fetch more (max 50).",
    }, ensure_ascii=False)


CHAT_RECALL_SCHEMA = {
    "name": "recall_chat",
    "description": (
        "Pull the latest N messages from a chat platform (telegram, discord, "
        "voice realtime, etc.), numbered newest-first. Use this when the user "
        "asks 'what did we just say', 'what was message 3', 'scroll back', or "
        "needs to reference a recent turn by number.\n\n"
        "Returns RAW message previews — no LLM summarization, no cost. Newest "
        "message is #1; higher numbers are older.\n\n"
        "Differs from recall_sessions: that one summarizes whole past sessions; "
        "this one shows the actual recent message text in order. Use this for "
        "'what did we just talk about', use recall_sessions for 'find the time "
        "we discussed X weeks ago'.\n\n"
        "Defaults: platform inferred from current session, 10 messages, "
        "user/assistant only (no tool calls)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "platform": {
                "type": "string",
                "description": (
                    "Chat platform source. One of: telegram, discord, "
                    "vc-realtime-giannis, matrix, signal, slack, sms, yuanbao. "
                    "If omitted, inferred from the current session's source."
                ),
            },
            "chat_id": {
                "type": "string",
                "description": (
                    "Optional. Restrict to a specific chat or user_id (matches "
                    "sessions.user_id). For Telegram this is the numeric user_id "
                    "or group chat_id (e.g. '413720629' for Giannis DM, "
                    "'-5298264040' for the BELLA CIAO group). Omit to span all "
                    "chats on the platform."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "How many messages to return. Default 10, max 50.",
                "default": 10,
            },
            "include_tool_calls": {
                "type": "boolean",
                "description": (
                    "Include tool/system rows in addition to user/assistant. "
                    "Default false (chat-style view only)."
                ),
                "default": False,
            },
        },
        "required": [],
    },
}


# --- Registry ---
from tools.registry import registry  # noqa: E402

registry.register(
    name="recall_chat",
    toolset="session_search",
    schema=CHAT_RECALL_SCHEMA,
    handler=lambda args, **kw: chat_recall(
        platform=args.get("platform"),
        chat_id=args.get("chat_id"),
        limit=args.get("limit", _DEFAULT_LIMIT),
        include_tool_calls=bool(args.get("include_tool_calls", False)),
        db=kw.get("db"),
        current_session_id=kw.get("current_session_id"),
    ),
    emoji="💬",
)
