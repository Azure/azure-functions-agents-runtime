"""Canonical non-secret Foundry Hosted Agent V0 runtime projection."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, ValidationError

from ..strict_json import DuplicateJsonKeyError, decode_json_object

FHA_RUNTIME_PROJECTION_VERSION = "fha_runtime_projection_v0"
FHA_RUNTIME_PROJECTION_FILENAME = "fha_runtime_projection.json"
FHA_RUNTIME_PROJECTION_DIGEST_PREFIX = "sha256:"
FHA_WRAPPER_DIGEST_DOMAIN = b"azure-functions-agents:fha-wrapper:v0\x00"
MAX_FHA_RUNTIME_PROJECTION_BYTES = 64 * 1024
FHA_STATIC_HEADER_ALLOWLIST = frozenset({"accept", "content-type", "user-agent"})

_UNESCAPED_DOLLAR_PATTERN = re.compile(r"(?<!\$)\$[A-Za-z_][A-Za-z0-9_]*")
_UNESCAPED_PERCENT_PATTERN = re.compile(r"(?<!%)%[A-Za-z_][A-Za-z0-9_]*%")
_PLACEHOLDER_DELIMITER_PATTERN = re.compile(r"\{\{|\}\}|<<|>>")
_SENSITIVE_MATERIAL_PATTERN = re.compile(
    r"authorization|bearer|basic|cookie|credential|password|secret|token|api[-_ ]?key|"
    r"(?:^|[^a-z0-9])key(?:$|[^a-z0-9])",
    re.IGNORECASE,
)


class FhaRuntimeProjectionError(ValueError):
    """The FHA runtime projection is invalid, unsafe, or non-canonical."""


@dataclass(frozen=True, slots=True)
class FhaProjectionCapabilities:
    """The deterministic capability names assigned to an FHA agent."""

    user_tools: tuple[str, ...]
    skills: tuple[str, ...]
    mcp: tuple[str, ...]
    subagents: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        user_tools: Sequence[str] = (),
        skills: Sequence[str] = (),
        mcp: Sequence[str] = (),
        subagents: Sequence[str] = (),
    ) -> FhaProjectionCapabilities:
        return cls(
            user_tools=_normalize_name_sequence(user_tools, "user tools"),
            skills=_normalize_name_sequence(skills, "skills"),
            mcp=_normalize_name_sequence(mcp, "MCP servers"),
            subagents=_normalize_name_sequence(subagents, "subagents"),
        )


@dataclass(frozen=True, slots=True)
class FhaProjectionCatalogEntry:
    """The canonical public-safe summary of one compiled catalog entry."""

    slug: str
    model: str
    trigger: str | None
    builtin_endpoints: tuple[str, ...]
    capabilities: FhaProjectionCapabilities

    @classmethod
    def create(
        cls,
        *,
        slug: str,
        model: str,
        trigger: str | None,
        builtin_endpoints: Sequence[str],
        capabilities: FhaProjectionCapabilities,
    ) -> FhaProjectionCatalogEntry:
        if not isinstance(capabilities, FhaProjectionCapabilities):
            raise FhaRuntimeProjectionError("FHA projection capabilities are invalid.")
        normalized_trigger = (
            None if trigger is None else _require_literal_text(trigger, "trigger", sensitive=False)
        )
        return cls(
            slug=_require_literal_text(slug, "slug", sensitive=False),
            model=_require_literal_text(model, "model", sensitive=True),
            trigger=normalized_trigger,
            builtin_endpoints=_normalize_name_sequence(
                builtin_endpoints,
                "built-in endpoints",
            ),
            capabilities=FhaProjectionCapabilities.create(
                user_tools=capabilities.user_tools,
                skills=capabilities.skills,
                mcp=capabilities.mcp,
                subagents=capabilities.subagents,
            ),
        )


@dataclass(frozen=True, slots=True)
class FhaProjectionMcpServer:
    """The V0-safe subset of one remote MCP server declaration."""

    name: str
    url: str
    allowed_tools: tuple[str, ...]
    auth_scope: str | None
    managed_identity_client_id: str | None
    headers: tuple[tuple[str, str], ...]

    @classmethod
    def create(
        cls,
        *,
        name: str,
        url: str,
        allowed_tools: Sequence[str],
        auth_scope: str | None = None,
        managed_identity_client_id: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> FhaProjectionMcpServer:
        normalized_scope = (
            None
            if auth_scope is None
            else _require_literal_text(auth_scope, "MCP auth scope", sensitive=True)
        )
        normalized_client_id = (
            None
            if managed_identity_client_id is None
            else _require_literal_text(
                managed_identity_client_id,
                "MCP managed-identity client ID",
                sensitive=True,
            )
        )
        if normalized_client_id is not None and normalized_scope is None:
            raise FhaRuntimeProjectionError(
                "MCP managed-identity client ID requires an auth scope."
            )
        if headers is not None and not isinstance(headers, Mapping):
            raise FhaRuntimeProjectionError("FHA MCP static headers are invalid.")
        return cls(
            name=_require_literal_text(name, "MCP server name", sensitive=False),
            url=_normalize_mcp_url(url),
            allowed_tools=_normalize_name_sequence(allowed_tools, "MCP allowed tools"),
            auth_scope=normalized_scope,
            managed_identity_client_id=normalized_client_id,
            headers=_normalize_headers(headers or {}),
        )

    @property
    def header_mapping(self) -> Mapping[str, str]:
        """Return a deterministic copy of safe static headers."""
        return dict(self.headers)


@dataclass(frozen=True, slots=True)
class FhaRuntimeProjection:
    """The exact V0 non-secret configuration consumed by every FHA path."""

    version: str
    project_endpoint: str
    default_model: str
    catalog: tuple[FhaProjectionCatalogEntry, ...]
    mcp_servers: tuple[FhaProjectionMcpServer, ...]

    @classmethod
    def create(
        cls,
        *,
        project_endpoint: str,
        default_model: str,
        catalog: Sequence[FhaProjectionCatalogEntry],
        mcp_servers: Sequence[FhaProjectionMcpServer] = (),
        version: str = FHA_RUNTIME_PROJECTION_VERSION,
    ) -> FhaRuntimeProjection:
        if version != FHA_RUNTIME_PROJECTION_VERSION:
            raise FhaRuntimeProjectionError("FHA runtime projection version is unsupported.")
        if isinstance(catalog, (str, bytes)) or not isinstance(catalog, Sequence):
            raise FhaRuntimeProjectionError("FHA runtime projection catalog is invalid.")
        if isinstance(mcp_servers, (str, bytes)) or not isinstance(mcp_servers, Sequence):
            raise FhaRuntimeProjectionError("FHA runtime projection MCP servers are invalid.")
        if any(not isinstance(entry, FhaProjectionCatalogEntry) for entry in catalog):
            raise FhaRuntimeProjectionError("FHA runtime projection catalog is invalid.")
        if any(not isinstance(server, FhaProjectionMcpServer) for server in mcp_servers):
            raise FhaRuntimeProjectionError("FHA runtime projection MCP servers are invalid.")
        checked_catalog = tuple(
            sorted(
                (
                    FhaProjectionCatalogEntry.create(
                        slug=entry.slug,
                        model=entry.model,
                        trigger=entry.trigger,
                        builtin_endpoints=entry.builtin_endpoints,
                        capabilities=entry.capabilities,
                    )
                    for entry in catalog
                ),
                key=lambda entry: entry.slug,
            )
        )
        checked_mcp = tuple(
            sorted(
                (
                    FhaProjectionMcpServer.create(
                        name=server.name,
                        url=server.url,
                        allowed_tools=server.allowed_tools,
                        auth_scope=server.auth_scope,
                        managed_identity_client_id=server.managed_identity_client_id,
                        headers=server.header_mapping,
                    )
                    for server in mcp_servers
                ),
                key=lambda server: server.name,
            )
        )
        _require_unique((entry.slug for entry in checked_catalog), "FHA catalog slugs")
        _require_unique((server.name for server in checked_mcp), "FHA MCP server names")
        projection = cls(
            version=version,
            project_endpoint=_normalize_project_endpoint(project_endpoint),
            default_model=_require_literal_text(default_model, "default model", sensitive=True),
            catalog=checked_catalog,
            mcp_servers=checked_mcp,
        )
        _ensure_projection_size(_render_projection(projection))
        return projection

    @property
    def digest(self) -> str:
        """Return the SHA-256 digest of this projection's exact canonical bytes."""
        return compute_fha_runtime_projection_digest(self)

    def serialize(self) -> str:
        """Return the only accepted compact JSON representation."""
        return serialize_fha_runtime_projection(self)

    def matches(self, expected: FhaRuntimeProjection | bytes | str) -> bool:
        """Return whether this projection is byte-for-byte canonical-equivalent."""
        return hmac.compare_digest(self.serialize(), coerce_fha_runtime_projection(expected).serialize())


class _ProjectionCapabilitiesPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    user_tools: list[str]
    skills: list[str]
    mcp: list[str]
    subagents: list[str]


class _ProjectionCatalogEntryPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    slug: str
    model: str
    trigger: str | None
    builtin_endpoints: list[str]
    capabilities: _ProjectionCapabilitiesPayload


class _ProjectionAuthPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    scope: str
    client_id: str | None = None


class _ProjectionMcpPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    name: str
    url: str
    allowed_tools: list[str]
    auth: _ProjectionAuthPayload | None = None
    headers: dict[str, str]


class _ProjectionPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    version: Literal["fha_runtime_projection_v0"]
    project_endpoint: str
    default_model: str
    catalog: list[_ProjectionCatalogEntryPayload]
    mcp: list[_ProjectionMcpPayload]


def serialize_fha_runtime_projection(projection: FhaRuntimeProjection) -> str:
    """Serialize a projection in its strict compact canonical form."""
    checked = FhaRuntimeProjection.create(
        version=projection.version,
        project_endpoint=projection.project_endpoint,
        default_model=projection.default_model,
        catalog=projection.catalog,
        mcp_servers=projection.mcp_servers,
    )
    serialized = _render_projection(checked)
    _ensure_projection_size(serialized)
    return serialized


def parse_fha_runtime_projection(payload: bytes | str) -> FhaRuntimeProjection:
    """Parse only an exact canonical FHA runtime projection."""
    try:
        text = _decode_projection_payload(payload)
        _ensure_projection_size(text)
        document = _ProjectionPayload.model_validate(decode_json_object(text))
        projection = FhaRuntimeProjection.create(
            version=document.version,
            project_endpoint=document.project_endpoint,
            default_model=document.default_model,
            catalog=tuple(
                FhaProjectionCatalogEntry.create(
                    slug=entry.slug,
                    model=entry.model,
                    trigger=entry.trigger,
                    builtin_endpoints=entry.builtin_endpoints,
                    capabilities=FhaProjectionCapabilities.create(
                        user_tools=entry.capabilities.user_tools,
                        skills=entry.capabilities.skills,
                        mcp=entry.capabilities.mcp,
                        subagents=entry.capabilities.subagents,
                    ),
                )
                for entry in document.catalog
            ),
            mcp_servers=tuple(
                FhaProjectionMcpServer.create(
                    name=server.name,
                    url=server.url,
                    allowed_tools=server.allowed_tools,
                    auth_scope=server.auth.scope if server.auth is not None else None,
                    managed_identity_client_id=(
                        server.auth.client_id if server.auth is not None else None
                    ),
                    headers=server.headers,
                )
                for server in document.mcp
            ),
        )
        if text != serialize_fha_runtime_projection(projection):
            raise FhaRuntimeProjectionError("FHA runtime projection is non-canonical.")
        return projection
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        DuplicateJsonKeyError,
        ValidationError,
        TypeError,
        ValueError,
    ):
        raise FhaRuntimeProjectionError("FHA runtime projection is invalid.") from None


def coerce_fha_runtime_projection(
    value: FhaRuntimeProjection | bytes | str,
) -> FhaRuntimeProjection:
    """Return a typed canonical projection from a typed or serialized value."""
    if isinstance(value, FhaRuntimeProjection):
        return FhaRuntimeProjection.create(
            version=value.version,
            project_endpoint=value.project_endpoint,
            default_model=value.default_model,
            catalog=value.catalog,
            mcp_servers=value.mcp_servers,
        )
    return parse_fha_runtime_projection(value)


def compute_fha_runtime_projection_digest(
    projection: FhaRuntimeProjection | bytes | str,
) -> str:
    """Return the canonical SHA-256 digest for one projection."""
    serialized = coerce_fha_runtime_projection(projection).serialize()
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"{FHA_RUNTIME_PROJECTION_DIGEST_PREFIX}{digest}"


def compute_fha_wrapper_digest(
    projection: FhaRuntimeProjection | bytes | str,
    rendered_entrypoint: bytes | str,
) -> str:
    """Return the versioned digest binding a generated host wrapper to its projection."""
    projection_bytes = coerce_fha_runtime_projection(projection).serialize().encode("utf-8")
    if isinstance(rendered_entrypoint, str):
        try:
            entrypoint_bytes = rendered_entrypoint.encode("utf-8")
        except UnicodeEncodeError:
            raise FhaRuntimeProjectionError(
                "FHA generated entrypoint must be valid UTF-8."
            ) from None
    elif isinstance(rendered_entrypoint, bytes):
        entrypoint_bytes = rendered_entrypoint
    else:
        raise FhaRuntimeProjectionError("FHA generated entrypoint is invalid.")
    hasher = hashlib.sha256()
    hasher.update(FHA_WRAPPER_DIGEST_DOMAIN)
    for value in (projection_bytes, entrypoint_bytes):
        hasher.update(len(value).to_bytes(8, "big"))
        hasher.update(value)
    return f"{FHA_RUNTIME_PROJECTION_DIGEST_PREFIX}{hasher.hexdigest()}"


def load_fha_runtime_projection(path: Path) -> FhaRuntimeProjection:
    """Load one staged canonical FHA V0 projection without environment resolution."""
    try:
        return parse_fha_runtime_projection(Path(path).read_bytes())
    except (OSError, FhaRuntimeProjectionError):
        raise FhaRuntimeProjectionError("FHA runtime projection is unavailable.") from None


def validate_fha_runtime_projection_match(
    projection: FhaRuntimeProjection,
    expected_projection: FhaRuntimeProjection | bytes | str,
) -> None:
    """Fail closed unless the compiled projection matches the expected projection."""
    expected = coerce_fha_runtime_projection(expected_projection)
    if not hmac.compare_digest(projection.serialize(), expected.serialize()):
        raise FhaRuntimeProjectionError(
            "FHA runtime projection does not match the expected projection."
        )


def _render_projection(projection: FhaRuntimeProjection) -> str:
    payload: dict[str, object] = {
        "version": projection.version,
        "project_endpoint": projection.project_endpoint,
        "default_model": projection.default_model,
        "catalog": [
            {
                "slug": entry.slug,
                "model": entry.model,
                "trigger": entry.trigger,
                "builtin_endpoints": list(entry.builtin_endpoints),
                "capabilities": {
                    "user_tools": list(entry.capabilities.user_tools),
                    "skills": list(entry.capabilities.skills),
                    "mcp": list(entry.capabilities.mcp),
                    "subagents": list(entry.capabilities.subagents),
                },
            }
            for entry in projection.catalog
        ],
        "mcp": [
            {
                "name": server.name,
                "url": server.url,
                "allowed_tools": list(server.allowed_tools),
                "auth": (
                    {
                        "scope": server.auth_scope,
                        **(
                            {"client_id": server.managed_identity_client_id}
                            if server.managed_identity_client_id is not None
                            else {}
                        ),
                    }
                    if server.auth_scope is not None
                    else None
                ),
                "headers": dict(server.headers),
            }
            for server in projection.mcp_servers
        ],
    }
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _normalize_project_endpoint(value: str) -> str:
    text = _require_literal_text(value, "project endpoint", sensitive=False)
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        raise FhaRuntimeProjectionError("FHA project endpoint is invalid.") from None
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname is None
        or parsed.query
        or parsed.fragment
    ):
        raise FhaRuntimeProjectionError("FHA project endpoint is invalid.")
    path = parsed.path.rstrip("/")
    path_parts = path.split("/")
    if not path or any(part in {"", ".", ".."} for part in path_parts[1:]):
        raise FhaRuntimeProjectionError("FHA project endpoint is invalid.")
    hostname = parsed.hostname.casefold()
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and port != 443:
        netloc = f"{netloc}:{port}"
    return urlunsplit(("https", netloc, path, "", ""))


def _normalize_mcp_url(value: str) -> str:
    text = _require_literal_text(value, "MCP URL", sensitive=True)
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        raise FhaRuntimeProjectionError("FHA MCP URL is invalid.") from None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname is None
        or _contains_sensitive_material(parsed.query)
    ):
        raise FhaRuntimeProjectionError("FHA MCP URL is invalid.")
    path = parsed.path or "/"
    if any(part in {".", ".."} for part in path.split("/")):
        raise FhaRuntimeProjectionError("FHA MCP URL is invalid.")
    hostname = parsed.hostname.casefold()
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and port not in {80, 443}:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme.casefold(), netloc, path, parsed.query, ""))


def _normalize_headers(headers: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    normalized: dict[str, tuple[str, str]] = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise FhaRuntimeProjectionError("FHA MCP static headers are invalid.")
        normalized_name = name.strip().casefold()
        if normalized_name not in FHA_STATIC_HEADER_ALLOWLIST:
            raise FhaRuntimeProjectionError("FHA MCP static header is not allowlisted.")
        if _contains_unsafe_placeholder(value) or _contains_sensitive_material(name) or _contains_sensitive_material(value):
            raise FhaRuntimeProjectionError("FHA MCP static header contains unsafe material.")
        canonical_name = {
            "accept": "Accept",
            "content-type": "Content-Type",
            "user-agent": "User-Agent",
        }[normalized_name]
        normalized_value = value.strip()
        if (
            not normalized_value
            or normalized_name in normalized
            or _contains_control_character(normalized_value)
        ):
            raise FhaRuntimeProjectionError("FHA MCP static headers are invalid.")
        normalized[normalized_name] = (canonical_name, normalized_value)
    return tuple(normalized[name] for name in sorted(normalized))


def _normalize_name_sequence(values: Sequence[str], label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise FhaRuntimeProjectionError(f"FHA projection {label} is invalid.")
    normalized = tuple(
        sorted(_require_literal_text(value, label, sensitive=False) for value in values)
    )
    _require_unique(normalized, label)
    return normalized


def _require_unique(values: Iterable[str], label: str) -> None:
    items = tuple(values)
    if len(items) != len(set(items)):
        raise FhaRuntimeProjectionError(f"FHA projection {label} must be unique.")


def _require_literal_text(value: str, label: str, *, sensitive: bool) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or _contains_control_character(value)
    ):
        raise FhaRuntimeProjectionError(f"FHA projection {label} is invalid.")
    if _contains_unsafe_placeholder(value) or (sensitive and _contains_sensitive_material(value)):
        raise FhaRuntimeProjectionError(f"FHA projection {label} contains unsafe material.")
    return value


def _contains_unsafe_placeholder(value: str) -> bool:
    return bool(
        _UNESCAPED_DOLLAR_PATTERN.search(value)
        or _UNESCAPED_PERCENT_PATTERN.search(value)
        or _PLACEHOLDER_DELIMITER_PATTERN.search(value)
    )


def _contains_sensitive_material(value: str) -> bool:
    return bool(_SENSITIVE_MATERIAL_PATTERN.search(value))


def _contains_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _decode_projection_payload(payload: bytes | str) -> str:
    if isinstance(payload, bytes):
        return payload.decode("utf-8")
    if isinstance(payload, str):
        return payload
    raise FhaRuntimeProjectionError("FHA runtime projection is invalid.")


def _ensure_projection_size(serialized: str) -> None:
    try:
        size = len(serialized.encode("utf-8"))
    except UnicodeEncodeError:
        raise FhaRuntimeProjectionError("FHA runtime projection must be valid UTF-8.") from None
    if size > MAX_FHA_RUNTIME_PROJECTION_BYTES:
        raise FhaRuntimeProjectionError("FHA runtime projection exceeds the size limit.")
