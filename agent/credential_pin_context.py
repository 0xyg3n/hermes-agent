"""Turn-scoped active credential pin.

The ``/cred`` slash command pins a credential pool entry (by label) for a
chat scope; ``gateway/run.py`` applies that pin to the **main** agent's
runtime at turn start (see ``_resolve_turn_agent_config``).  But three
other credential-selection paths run independently and would otherwise
ignore the pin entirely:

  * **Auxiliary LLM calls** — compression, title generation, vision,
    web_extract, session_search, skills_hub, mcp — go through
    ``auxiliary_client._select_pool_entry`` → ``pool.select()`` by
    *strategy*, not the pin.
  * **Credential-pool rotation** on 429 / billing / auth failure
    (``CredentialPool.mark_exhausted_and_rotate``) strategy-selects the
    next entry, so a pinned chat silently drifts off its license the
    moment it hits one rate limit.
  * **Delegated subagents** lease a credential via
    ``CredentialPool.acquire_lease`` and rotate on their own.

This module exposes a :mod:`contextvars` variable carrying the active pin
label per provider for the lifetime of a turn.  Every credential-selection
path consults it so a pinned chat stays locked to the same license across
the whole turn — main agent, auxiliary calls, rotation, and subagents.

Isolation: the gateway runs each turn inside a copied context
(``_run_in_executor_with_context`` → ``contextvars.copy_context()``), so a
pin set inside one turn never leaks into another.  ``ThreadPoolExecutor``
workers (delegated subagents) do **not** inherit contextvars, so
``delegate_tool`` re-installs the pin explicitly in the child threads.

The pin is a *preference*, not a hard lock: if the pinned entry is in
exhaustion cooldown, selection falls back to the configured strategy so a
stale or rate-limited pin can never wedge a conversation.  When the pinned
entry recovers, subsequent selections snap back to it automatically.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# provider (lowercased) -> pinned pool entry label
_active_pins: contextvars.ContextVar[Optional[Dict[str, str]]] = contextvars.ContextVar(
    "hermes_active_credential_pins", default=None
)


def _normalize(pins: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
    if not pins:
        return None
    normalized: Dict[str, str] = {}
    for key, value in pins.items():
        if not key or not value:
            continue
        provider = str(key).strip().lower()
        label = str(value).strip()
        if provider and label:
            normalized[provider] = label
    return normalized or None


def set_active_pins(pins: Optional[Dict[str, str]]) -> contextvars.Token:
    """Install the active provider->label pin map for this context.

    Returns a reset token; pass it to :func:`reset_active_pins` to undo.
    Passing a falsy/empty map clears any pin.
    """
    return _active_pins.set(_normalize(pins))


def set_active_pin(provider: str, label: str) -> contextvars.Token:
    """Convenience: install a single-provider pin for this context.

    An empty provider or label clears the pin.  Returns a reset token.
    """
    provider = (provider or "").strip().lower()
    label = (label or "").strip()
    if not provider or not label:
        return _active_pins.set(None)
    return _active_pins.set({provider: label})


def reset_active_pins(token: contextvars.Token) -> None:
    """Restore the pin map to its value before the matching set_* call."""
    try:
        _active_pins.reset(token)
    except (ValueError, LookupError) as exc:  # token from a different context
        logger.debug("credential_pin_context: reset skipped (%s)", exc)


def get_active_pin(provider: str) -> Optional[str]:
    """Return the pinned pool-entry label for ``provider`` in this context.

    ``None`` when no pin is active for the provider.
    """
    provider = (provider or "").strip().lower()
    if not provider:
        return None
    pins = _active_pins.get()
    if not pins:
        return None
    return pins.get(provider)


def get_active_pins() -> Optional[Dict[str, str]]:
    """Return a copy of the full active provider->label pin map (or None)."""
    pins = _active_pins.get()
    return dict(pins) if pins else None
