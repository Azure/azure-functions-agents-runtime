"""Pure resolution of a custom HTTP trigger's effective inbound auth policy."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import ValidationError

from .schema import EndpointAuthConfig

_LEGACY_AUTH_LEVELS = frozenset({"anonymous", "function", "admin"})


def resolve_http_trigger_auth(trigger_args: Mapping[str, object]) -> EndpointAuthConfig:
    """Apply current nested-auth, legacy-level, then default precedence exactly once."""
    raw_auth = trigger_args.get("http_auth")
    if raw_auth is not None:
        try:
            return EndpointAuthConfig.model_validate(raw_auth)
        except ValidationError as exc:
            detail = exc.errors()[0].get("msg", "invalid value") if exc.errors() else "invalid value"
            raise ValueError(f"invalid http_trigger 'http_auth': {detail}") from exc
    raw_level = trigger_args.get("auth_level")
    if raw_level is None:
        return EndpointAuthConfig()
    level = str(raw_level).strip().casefold()
    if level not in _LEGACY_AUTH_LEVELS:
        valid = ", ".join(sorted(_LEGACY_AUTH_LEVELS))
        raise ValueError(f"invalid auth_level {level!r}. Must be one of: {valid}")
    return EndpointAuthConfig.model_validate({"mode": level})
