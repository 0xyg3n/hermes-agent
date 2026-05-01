"""Provider-profile shortcuts for gateway and CLI slash commands."""

from __future__ import annotations

from typing import Any


GPT_PROVIDER = "openai-codex"
GPT_MODEL = "gpt-5.5"
ANTHROPIC_PROVIDER = "anthropic"
ANTHROPIC_MODEL = "claude-opus-4-7"


def resolve_profile_target(profile: str) -> tuple[str, str]:
    """Return ``(provider, model)`` for a shortcut profile command."""
    normalized = (profile or "").strip().lower().lstrip("/")

    if normalized in {"openai", "gpt"}:
        return GPT_PROVIDER, GPT_MODEL
    if normalized == "anthropic":
        return ANTHROPIC_PROVIDER, ANTHROPIC_MODEL
    raise ValueError(f"Unknown auth profile: {profile}")


def _valid_fallback_entries(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or "").strip()
        model = str(item.get("model") or "").strip()
        if provider and model:
            entries.append(dict(item))
    return entries


def _dedupe_fallback_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    result: list[dict[str, Any]] = []
    for entry in entries:
        key = (
            str(entry.get("provider") or "").strip(),
            str(entry.get("model") or "").strip(),
            str(entry.get("base_url") or "").strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(entry)
    return result


def _remove_matching_fallback(
    entries: list[dict[str, Any]],
    provider: str,
    model: str,
) -> list[dict[str, Any]]:
    return [
        entry for entry in entries
        if not (
            str(entry.get("provider") or "").strip() == provider
            and str(entry.get("model") or "").strip() == model
        )
    ]


def _pin_codex_auxiliary(config: dict[str, Any], model: str) -> None:
    """Keep Laira's helper calls on Codex when the GPT shortcut is selected."""
    auxiliary = config.setdefault("auxiliary", {})
    if isinstance(auxiliary, dict):
        for value in auxiliary.values():
            if not isinstance(value, dict):
                continue
            value["provider"] = GPT_PROVIDER
            value["model"] = model

    memory = config.setdefault("memory", {})
    if isinstance(memory, dict):
        memory["provider"] = GPT_PROVIDER

    delegation = config.setdefault("delegation", {})
    if isinstance(delegation, dict):
        delegation["provider"] = GPT_PROVIDER
        if not delegation.get("model"):
            delegation["model"] = ""


def set_active_auth_provider(provider: str) -> None:
    """Set ``auth.json.active_provider`` without deleting credentials."""
    from hermes_cli.auth import _auth_store_lock, _load_auth_store, _save_auth_store

    with _auth_store_lock():
        auth_store = _load_auth_store()
        auth_store["active_provider"] = provider
        _save_auth_store(auth_store)


def reset_provider_credential_status(provider: str) -> int:
    """Clear stale exhaustion state for a provider's credential pool."""
    try:
        from agent.credential_pool import load_pool

        return load_pool(provider).reset_statuses()
    except Exception:
        return 0


def finalize_profile_config(profile: str, model: str | None = None) -> dict[str, Any]:
    """Persist provider-profile details that ``/model`` does not own.

    ``/model`` handles runtime credential resolution and session overrides.
    This function handles the durable auth/fallback policy:

    - ``/openai`` makes Codex/GPT-5.5 the active auth provider and removes a
      same-model fallback loop.
    - ``/anthropic`` makes Anthropic active and prepends Codex/GPT-5.5 as the
      fallback for Anthropic rate limits.
    """
    from hermes_cli.config import read_raw_config, save_config

    provider, selected_model = resolve_profile_target(profile)

    config = read_raw_config()
    model_cfg = config.get("model")
    if not isinstance(model_cfg, dict):
        model_cfg = {}
    else:
        model_cfg = dict(model_cfg)

    model_cfg["provider"] = provider
    model_cfg["default"] = selected_model
    model_cfg.pop("base_url", None)
    model_cfg.pop("api_key", None)
    model_cfg.pop("api_mode", None)
    config["model"] = model_cfg

    chain = _valid_fallback_entries(config.get("fallback_providers"))
    chain = _remove_matching_fallback(chain, provider, selected_model)

    if provider == ANTHROPIC_PROVIDER:
        gpt_entry = {"provider": GPT_PROVIDER, "model": GPT_MODEL}
        chain = [gpt_entry] + _remove_matching_fallback(chain, GPT_PROVIDER, GPT_MODEL)
        reset_provider_credential_status(provider)
    elif provider == GPT_PROVIDER:
        _pin_codex_auxiliary(config, selected_model)

    config["fallback_providers"] = _dedupe_fallback_entries(chain)

    set_active_auth_provider(provider)
    save_config(config)

    return {
        "provider": provider,
        "model": selected_model,
        "fallback_providers": config.get("fallback_providers") or [],
    }
