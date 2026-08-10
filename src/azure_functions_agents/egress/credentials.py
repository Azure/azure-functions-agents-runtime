"""Compile structurally authored outbound headers into egress transformations."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Annotated

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


class _SecretReferencePayload(BaseModel):
    """The explicit group-secret shape accepted at one header value position."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    secret: _NonEmptyText
    key: _NonEmptyText
    format: str = "{value}"


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
    provider: str | None,
    environment: Mapping[str, str] | None = None,
) -> tuple[SandboxEgressHeader, ...]:
    """Compile only the resolved provider's conventional model-key header."""

    source = os.environ if environment is None else environment
    if provider == "azure_openai":
        azure_openai_key = _app_setting(source, AZURE_OPENAI_API_KEY_ENV)
        return (
            ()
            if not azure_openai_key
            else (compile_static_header("api-key", azure_openai_key),)
        )
    if provider == "openai":
        openai_key = _app_setting(source, OPENAI_API_KEY_ENV)
        return (
            ()
            if not openai_key
            else (compile_static_header("Authorization", f"Bearer {openai_key}"),)
        )
    return ()


def _app_setting(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SandboxProvisioningError("Model API key settings must be strings.")
    return value
