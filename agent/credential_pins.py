"""Per-chat credential pin store.

Lets the gateway pin a specific credential pool entry (by label) for a
given platform+chat scope or globally.  Used by the ``/cred`` slash
command so Giannis can route a Telegram or Discord conversation to a
specific Anthropic OAuth account (giannis_x20 / seaverse_year /
vivi_max).

Persistence layout (``~/.hermes/credential_pins.json``):

    {
        "anthropic": {
            "telegram:413720629": "giannis_x20",
            "discord:1484916957957586974": "vivi_max",
            "__global__": "seaverse_year"
        }
    }

The store is intentionally tiny and file-only — no migration, no schema,
no locking beyond the kernel-atomic write.  A pinned label that no
longer exists in the pool is treated as "no pin" (the caller falls back
to the configured selection strategy) so dead pins never wedge a chat.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

GLOBAL_SCOPE = "__global__"


def _pins_path() -> Path:
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return Path(home) / "credential_pins.json"


def _load() -> Dict[str, Dict[str, str]]:
    path = _pins_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            # normalize: value must be {scope: label}
            return {
                str(k): {str(sk): str(sv) for sk, sv in (v or {}).items() if isinstance(sv, str) and sv}
                for k, v in data.items()
                if isinstance(v, dict)
            }
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("credential_pins: failed to read %s (%s); treating as empty", path, exc)
    return {}


def _save(pins: Dict[str, Dict[str, str]]) -> None:
    path = _pins_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".credential_pins.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(pins, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def scope_key(platform: Optional[str], chat_id: Optional[str]) -> str:
    """Build the canonical scope key for a (platform, chat_id) pair.

    Falls back to ``GLOBAL_SCOPE`` when either field is missing — used by
    CLI / non-platform contexts that still want to honor a globally-set
    pin.
    """

    p = (platform or "").strip().lower()
    c = (chat_id or "").strip()
    if not p or not c:
        return GLOBAL_SCOPE
    return f"{p}:{c}"


def get_pin(provider: str, platform: Optional[str], chat_id: Optional[str]) -> Optional[str]:
    """Return the pinned label for this scope, or None.

    Resolution order: chat-scoped pin → global pin → None.
    """

    if not provider:
        return None
    pins = _load().get(provider) or {}
    if not pins:
        return None
    sk = scope_key(platform, chat_id)
    if sk in pins:
        return pins[sk]
    if sk != GLOBAL_SCOPE and GLOBAL_SCOPE in pins:
        return pins[GLOBAL_SCOPE]
    return None


def set_pin(provider: str, platform: Optional[str], chat_id: Optional[str], label: str) -> None:
    """Pin ``label`` for this provider+scope.  Empty label clears the pin."""

    if not provider:
        return
    label = (label or "").strip()
    pins = _load()
    bucket = pins.setdefault(provider, {})
    sk = scope_key(platform, chat_id)
    if not label:
        bucket.pop(sk, None)
    else:
        bucket[sk] = label
    if not bucket:
        pins.pop(provider, None)
    _save(pins)


def clear_pin(provider: str, platform: Optional[str], chat_id: Optional[str]) -> bool:
    """Remove the pin for this scope.  Returns True if a pin was removed."""

    if not provider:
        return False
    pins = _load()
    bucket = pins.get(provider) or {}
    sk = scope_key(platform, chat_id)
    if sk not in bucket:
        return False
    bucket.pop(sk, None)
    if not bucket:
        pins.pop(provider, None)
    _save(pins)
    return True


def list_pins(provider: Optional[str] = None) -> Dict[str, Dict[str, str]]:
    """Return all pins, optionally filtered to a single provider."""

    pins = _load()
    if provider:
        return {provider: pins.get(provider, {})}
    return pins
