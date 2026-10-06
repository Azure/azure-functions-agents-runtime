"""Lazy Azure authentication headers shared by MCP adapters."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from ._credential import build_credential, build_credential_with_client_id
from .config.env import has_unresolved_placeholders
from .discovery.mcp import MCPServerDescriptor

if TYPE_CHECKING:
    from azure.core.credentials import AccessToken, TokenCredential

type MCPHeaderProvider = Callable[[dict[str, object]], dict[str, str]]

_DEFAULT_TOKEN_REFRESH_OFFSET_SECONDS = 300


def _build_mcp_credential(client_id: str | None) -> TokenCredential:
    selected_client_id = (client_id or "").strip()
    if not selected_client_id or has_unresolved_placeholders(selected_client_id):
        return build_credential()
    return build_credential_with_client_id(selected_client_id)


def build_mcp_header_provider(server: MCPServerDescriptor) -> MCPHeaderProvider | None:
    """Build a lazy header provider with per-request token refresh."""
    static_headers = dict(server.headers)

    def static_header_provider(_ctx: dict[str, object]) -> dict[str, str]:
        return dict(static_headers)

    scope = server.auth_scope
    if scope is None:
        return static_header_provider if static_headers else None
    scope = scope.strip()
    if not scope:
        return static_header_provider if static_headers else None

    client_id = server.client_id
    credential: TokenCredential | None = None
    cached_token: AccessToken | None = None

    def credential_header_provider(_ctx: dict[str, object]) -> dict[str, str]:
        nonlocal credential, cached_token
        now = int(time.time())
        if (
            cached_token is None
            or not cached_token.token
            or cached_token.expires_on - _DEFAULT_TOKEN_REFRESH_OFFSET_SECONDS <= now
        ):
            if credential is None:
                credential = _build_mcp_credential(client_id)
            token = credential.get_token(scope)
            if not token.token.strip():
                raise ValueError("MCP authentication returned an empty token")
            cached_token = token

        result = {
            key: value for key, value in static_headers.items() if key.lower() != "authorization"
        }
        result["Authorization"] = f"Bearer {cached_token.token}"
        return result

    return credential_header_provider
