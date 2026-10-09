"""Exact-origin policy helpers for the optional durable-chat standalone host."""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Self
from urllib.parse import urlsplit

from azurefunctions.extensions.http.fastapi import Request

_ORIGIN_SCHEMES = frozenset({"http", "https"})
_LOOPBACK_HOSTNAMES = frozenset({"localhost"})
_HOST_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_CORS_HEADER_NAMES = {
    "accept": "Accept",
    "content-type": "Content-Type",
    "idempotency-key": "Idempotency-Key",
    "last-event-id": "Last-Event-ID",
}
_PREFLIGHT_VARY = "Origin, Access-Control-Request-Method, Access-Control-Request-Headers"


class DurableChatOriginError(ValueError):
    """The optional durable-chat origin configuration is invalid."""


@dataclass(frozen=True, slots=True)
class DurableChatOriginPolicy:
    """The fixed cross-origin allowlist for one anonymous durable-chat app."""

    allowed_origins: frozenset[str]

    @classmethod
    def disabled(cls) -> Self:
        """Create the same-origin-only policy."""
        return cls(frozenset())

    @classmethod
    def create(cls, origins: Iterable[str]) -> Self:
        """Validate and freeze canonical exact origins."""
        normalized = tuple(normalize_durable_chat_origin(origin) for origin in origins)
        if len(set(normalized)) != len(normalized):
            raise DurableChatOriginError("durable-chat allowed origins must be unique")
        return cls(frozenset(normalized))

    @property
    def enabled(self) -> bool:
        """Return whether cross-origin standalone access is configured."""
        return bool(self.allowed_origins)

    def allowed_origin(self, value: str | None) -> str | None:
        """Return the configured canonical origin for one allowed request."""
        if not self.enabled or value is None:
            return None
        try:
            canonical = normalize_durable_chat_origin(value)
        except DurableChatOriginError:
            return None
        return canonical if canonical in self.allowed_origins else None


@dataclass(frozen=True, slots=True)
class DurableChatRequestOriginDecision:
    """The origin authorization and optional CORS response for one request."""

    permitted: bool
    cors_origin: str | None = None


@dataclass(frozen=True, slots=True)
class DurableChatCorsPreflight:
    """One validated, route-scoped CORS preflight request."""

    origin: str
    requested_method: str
    requested_headers: tuple[str, ...]


def parse_durable_chat_allowed_origins(value: str | None) -> DurableChatOriginPolicy:
    """Parse the private JSON origin list without registration side effects."""
    if value is None or not value.strip():
        return DurableChatOriginPolicy.disabled()
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DurableChatOriginError(
            "durable-chat allowed origins must be a JSON array"
        ) from exc
    if not isinstance(parsed, list) or any(
        not isinstance(origin, str) or not origin for origin in parsed
    ):
        raise DurableChatOriginError(
            "durable-chat allowed origins must be a JSON array of strings"
        )
    return DurableChatOriginPolicy.create(parsed)


def normalize_durable_chat_origin(value: str) -> str:
    """Return the canonical form of one HTTPS or loopback HTTP origin."""
    if (
        not value
        or value != value.strip()
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise DurableChatOriginError("durable-chat origin is malformed")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise DurableChatOriginError("durable-chat origin is malformed") from exc
    scheme = parsed.scheme.casefold()
    if (
        scheme not in _ORIGIN_SCHEMES
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.hostname is None
        or parsed.netloc.endswith(":")
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise DurableChatOriginError("durable-chat origin is malformed")
    host, is_loopback = _canonical_host(parsed.hostname)
    if scheme == "http" and not is_loopback:
        raise DurableChatOriginError(
            "durable-chat HTTP origins must use an exact loopback host"
        )
    effective_port = 443 if scheme == "https" else 80
    rendered_host = f"[{host}]" if ":" in host else host
    rendered_port = "" if port is None or port == effective_port else f":{port}"
    return f"{scheme}://{rendered_host}{rendered_port}"


def durable_chat_request_origin(
    req: Request,
    policy: DurableChatOriginPolicy,
) -> DurableChatRequestOriginDecision:
    """Authorize a selected durable-chat request before any durable access."""
    origin = req.headers.get("Origin")
    if origin is None or not policy.enabled:
        return DurableChatRequestOriginDecision(permitted=True)
    allowed_origin = policy.allowed_origin(origin)
    if allowed_origin is not None:
        return DurableChatRequestOriginDecision(
            permitted=True,
            cors_origin=allowed_origin,
        )
    return DurableChatRequestOriginDecision(
        permitted=same_origin_durable_chat_request(req, origin)
    )


def durable_chat_mutation_origin(
    req: Request,
    policy: DurableChatOriginPolicy,
) -> DurableChatRequestOriginDecision:
    """Authorize an existing durable-loop mutation with the shared policy."""
    origin = req.headers.get("Origin")
    if origin is None:
        return DurableChatRequestOriginDecision(permitted=True)
    allowed_origin = policy.allowed_origin(origin)
    if allowed_origin is not None:
        return DurableChatRequestOriginDecision(
            permitted=True,
            cors_origin=allowed_origin,
        )
    return DurableChatRequestOriginDecision(
        permitted=same_origin_durable_chat_request(req, origin)
    )


def same_origin_durable_chat_request(req: Request, origin: str) -> bool:
    """Return whether an Origin header exactly matches the Functions proxy origin."""
    host = req.headers.get("X-Forwarded-Host", req.headers.get("Host"))
    if not host or "," in host or any(character.isspace() for character in host):
        return False
    forwarded = req.headers.get("X-Forwarded-Proto")
    expected_scheme = forwarded.strip().casefold() if forwarded else _request_scheme(req)
    try:
        parsed = urlsplit(origin)
        expected_host = urlsplit(f"//{host}")
        origin_port = parsed.port
        expected_port = expected_host.port
    except ValueError:
        return False
    return (
        expected_scheme in _ORIGIN_SCHEMES
        and parsed.scheme in _ORIGIN_SCHEMES
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
        and expected_host.hostname is not None
        and expected_host.username is None
        and expected_host.password is None
        and not expected_host.path
        and not expected_host.query
        and not expected_host.fragment
        and parsed.hostname is not None
        and parsed.hostname.casefold() == expected_host.hostname.casefold()
        and _effective_port(parsed.scheme, origin_port)
        == _effective_port(expected_scheme, expected_port)
        and parsed.scheme.casefold() == expected_scheme
    )


def durable_chat_cors_preflight(
    req: Request,
    policy: DurableChatOriginPolicy,
    allowed_methods: frozenset[str],
) -> DurableChatCorsPreflight | None:
    """Validate one selected route's preflight without invoking its handler."""
    origin = policy.allowed_origin(req.headers.get("Origin"))
    requested_method = req.headers.get("Access-Control-Request-Method")
    if origin is None or requested_method is None:
        return None
    method = requested_method.strip().upper()
    if not method or method not in allowed_methods:
        return None
    try:
        requested_headers = _requested_cors_headers(
            req.headers.get("Access-Control-Request-Headers")
        )
    except DurableChatOriginError:
        return None
    return DurableChatCorsPreflight(
        origin=origin,
        requested_method=method,
        requested_headers=requested_headers,
    )


def durable_chat_cors_headers(
    decision: DurableChatRequestOriginDecision,
) -> dict[str, str]:
    """Build exact CORS headers for one approved actual request."""
    if decision.cors_origin is None:
        return {}
    return {
        "Access-Control-Allow-Origin": decision.cors_origin,
        "Vary": "Origin",
    }


def durable_chat_preflight_headers(
    preflight: DurableChatCorsPreflight,
    allowed_methods: frozenset[str],
) -> dict[str, str]:
    """Build exact CORS headers for one approved route-scoped preflight."""
    headers = {
        "Access-Control-Allow-Origin": preflight.origin,
        "Access-Control-Allow-Methods": ", ".join(sorted(allowed_methods)),
        "Vary": _PREFLIGHT_VARY,
    }
    if preflight.requested_headers:
        headers["Access-Control-Allow-Headers"] = ", ".join(
            preflight.requested_headers
        )
    return headers


def _canonical_host(host: str) -> tuple[str, bool]:
    if not host or "%" in host or "*" in host:
        raise DurableChatOriginError(
            "durable-chat origin host is malformed"
        ) from None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            canonical = host.encode("idna").decode("ascii").casefold()
        except UnicodeError as exc:
            raise DurableChatOriginError(
                "durable-chat origin host is malformed"
            ) from exc
        if (
            canonical == "null"
            or canonical.endswith(".")
            or len(canonical) > 253
            or any(
                _HOST_LABEL_PATTERN.fullmatch(label) is None
                for label in canonical.split(".")
            )
        ):
            raise DurableChatOriginError(
                "durable-chat origin host is malformed"
            ) from None
        return canonical, canonical in _LOOPBACK_HOSTNAMES
    return address.compressed.casefold(), address.is_loopback


def _requested_cors_headers(value: str | None) -> tuple[str, ...]:
    if value is None or not value.strip():
        return ()
    headers: list[str] = []
    for raw_name in value.split(","):
        name = raw_name.strip().casefold()
        canonical = _CORS_HEADER_NAMES.get(name)
        if canonical is None or canonical in headers:
            raise DurableChatOriginError("durable-chat preflight headers are invalid")
        headers.append(canonical)
    return tuple(headers)


def _request_scheme(req: Request) -> str:
    url = getattr(req, "url", None)
    scheme = getattr(url, "scheme", None)
    if isinstance(scheme, str) and scheme.casefold() in _ORIGIN_SCHEMES:
        return scheme.casefold()
    return "https"


def _effective_port(scheme: str, port: int | None) -> int:
    if port is not None:
        return port
    return 443 if scheme.casefold() == "https" else 80


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, item in pairs:
        if key in result:
            raise DurableChatOriginError(
                "durable-chat allowed origins JSON contains duplicate keys"
            )
        result[key] = item
    return result


def _reject_json_constant(_value: str) -> None:
    raise DurableChatOriginError(
        "durable-chat allowed origins JSON contains an unsupported value"
    )
