"""Pure resolution of a custom HTTP trigger's effective inbound auth policy."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import ValidationError

from .schema import EndpointAuthConfig

_DEPRECATED_AUTH_LEVELS = frozenset({"anonymous", "function", "admin"})


def resolve_http_trigger_auth(trigger_args: Mapping[str, object]) -> EndpointAuthConfig:
    """Apply current nested-auth, legacy-level, then default precedence exactly once."""
    raw_auth = trigger_args.get("http_auth")
    if raw_auth is not None:
        try:
            return EndpointAuthConfig.model_validate(raw_auth, strict=True)
        except ValidationError as exc:
            detail = exc.errors()[0].get("msg", "invalid value") if exc.errors() else "invalid value"
            raise ValueError(f"invalid http_trigger 'http_auth': {detail}") from exc
    raw_level = trigger_args.get("auth_level")
    if raw_level is None:
        return EndpointAuthConfig()
    if not isinstance(raw_level, str):
        raise ValueError("invalid auth_level. Must be a string")
    level = raw_level.strip().casefold()
    if level not in _DEPRECATED_AUTH_LEVELS:
        valid = ", ".join(sorted(_DEPRECATED_AUTH_LEVELS))
        raise ValueError(f"invalid auth_level {level!r}. Must be one of: {valid}")
    return EndpointAuthConfig.model_validate({"mode": level}, strict=True)


def resolve_aca_submission_auth(
    *,
    builtin_auth: EndpointAuthConfig | None,
    trigger_args: Mapping[str, object] | None,
) -> EndpointAuthConfig | None:
    """Resolve the one exact ACA submission policy across enabled surfaces."""
    trigger_auth = (
        None if trigger_args is None else resolve_http_trigger_auth(trigger_args)
    )
    if (
        builtin_auth is not None
        and trigger_auth is not None
        and builtin_auth.model_dump(mode="json") != trigger_auth.model_dump(mode="json")
    ):
        raise ValueError(
            "Custom http_trigger and built-in chat require identical resolved auth policies "
            "when session_runtime.aca_sandbox is configured"
        )
    return builtin_auth or trigger_auth
