"""Compile structurally authored outbound headers into egress transformations."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from ..config.env import substitute_env_vars_in_value
from ..transport.transport_models import (
    SandboxEgressHeader,
    SandboxEgressSecretRef,
    SandboxProvisioningError,
)

type _NonEmptyText = Annotated[str, StringConstraints(min_length=1)]

AZURE_OPENAI_API_KEY_ENV = "AZURE_OPENAI_API_KEY"
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
_MISSING = object()


class _SecretReferencePayload(BaseModel):
    """The explicit group-secret shape accepted at one header value position."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    secret: _NonEmptyText
    key: _NonEmptyText
    format: str = "{value}"


class McpIdentityConfigurationError(SandboxProvisioningError):
    """An authenticated MCP server cannot select a Sandbox Group identity."""


class McpIdentityDefinition(Protocol):
    """The discovery metadata shape needed for native MCP identity validation."""

    auth: Mapping[str, object]


def compile_mcp_headers(headers: Mapping[str, object] | None) -> tuple[SandboxEgressHeader, ...]:
    """Compile header-shaped MCP configuration without inferring credential names."""

    if headers is None:
        return ()
    compiled: list[SandboxEgressHeader] = []
    for name, value in headers.items():
        if not isinstance(name, str) or not name.strip():
            raise SandboxProvisioningError("MCP header names must be non-empty strings.")
        compiled.append(_compile_header_value(name, value))
    return tuple(compiled)


def _compile_header_value(name: str, value: object) -> SandboxEgressHeader:
    if isinstance(value, str):
        return SandboxEgressHeader.create(
            operation="Set",
            name=name,
            value=substitute_env_vars_in_value(value),
        )
    if not isinstance(value, Mapping) or set(value) != {"secretRef"}:
        raise SandboxProvisioningError(
            "MCP header values must be strings or a secretRef object."
        )
    try:
        payload = _SecretReferencePayload.model_validate(value["secretRef"])
    except (KeyError, ValidationError, TypeError, ValueError):
        raise SandboxProvisioningError("MCP header secretRef is invalid.") from None
    secret_ref = SandboxEgressSecretRef.create(
        secret_id=payload.secret,
        secret_key=payload.key,
        format=payload.format,
    )
    return SandboxEgressHeader.create(operation="Set", name=name, secret_ref=secret_ref)


def compile_static_header(
    name: str,
    value: str,
) -> SandboxEgressHeader:
    """Compile an app-setting-backed static header value for one sandbox policy."""

    if not isinstance(value, str):
        raise SandboxProvisioningError("Static egress header values must be strings.")
    return SandboxEgressHeader.create(operation="Set", name=name, value=value)


def compile_model_key_headers(
    environment: Mapping[str, str] | None = None,
) -> tuple[SandboxEgressHeader, ...]:
    """Compile existing Function App model keys into proxy-only header values."""

    source = os.environ if environment is None else environment
    headers: list[SandboxEgressHeader] = []
    azure_openai_key = _app_setting(source, AZURE_OPENAI_API_KEY_ENV)
    if azure_openai_key:
        headers.append(compile_static_header("api-key", azure_openai_key))
    openai_key = _app_setting(source, OPENAI_API_KEY_ENV)
    if openai_key:
        headers.append(compile_static_header("Authorization", f"Bearer {openai_key}"))
    return tuple(headers)


def _app_setting(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SandboxProvisioningError("Model API key settings must be strings.")
    return value


def validate_mcp_identity_requirements(
    servers: Mapping[str, Mapping[str, object] | McpIdentityDefinition],
    group_client_ids: Iterable[str],
) -> None:
    """Validate native MCP token selection against known group identities."""

    normalized_group_ids = {
        client_id.casefold()
        for client_id in group_client_ids
        if isinstance(client_id, str) and client_id.strip()
    }
    for name, server in servers.items():
        if not isinstance(name, str):
            raise McpIdentityConfigurationError("MCP server names must be strings.")
        auth = _mcp_auth(server)
        if auth is None:
            continue
        scope = auth.get("scope")
        if not isinstance(scope, str) or not scope.strip():
            continue
        if not normalized_group_ids:
            raise McpIdentityConfigurationError(
                "An authenticated MCP server requires a Sandbox Group identity."
            )
        client_id = auth.get("client_id")
        if client_id is not None and (not isinstance(client_id, str) or not client_id.strip()):
            raise McpIdentityConfigurationError(
                "An authenticated MCP client_id must be a non-empty string."
            )
        if isinstance(client_id, str) and client_id.strip():
            if client_id.casefold() not in normalized_group_ids:
                raise McpIdentityConfigurationError(
                    "An authenticated MCP client_id is not available to the Sandbox Group."
                )
        elif len(normalized_group_ids) > 1:
            raise McpIdentityConfigurationError(
                "An authenticated MCP server must select one Sandbox Group identity."
            )


def _mcp_auth(server: object) -> Mapping[str, object] | None:
    if isinstance(server, Mapping):
        candidate = server.get("auth")
    else:
        candidate = getattr(server, "auth", _MISSING)
        if candidate is _MISSING:
            raise McpIdentityConfigurationError("MCP discovery metadata is invalid.")
    if candidate is None:
        return None
    if not isinstance(candidate, Mapping):
        raise McpIdentityConfigurationError("MCP server auth must be an object.")
    return candidate
