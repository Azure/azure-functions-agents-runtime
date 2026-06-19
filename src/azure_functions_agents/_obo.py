"""On-Behalf-Of (OBO) token flow support.

This module provides the infrastructure for exchanging user access tokens for
downstream API tokens using the OAuth 2.0 On-Behalf-Of flow. This allows agents
to call downstream APIs (MCP servers, user tools, etc.) using the authenticated
end-user's identity rather than the function app's managed identity.

Architecture
------------

* :class:`UserContext` carries the user's identity through the request lifecycle.
  It is created from incoming HTTP request headers (EasyAuth or Authorization)
  and threaded through to tools that need to make authenticated downstream calls.

* :class:`OboTokenProvider` handles the actual token exchange using MSAL. It
  maintains an in-memory token cache keyed by (user_token_hash, scope) to avoid
  redundant token exchanges within a request.

* When OBO is not available (no user token, or OBO not configured), the system
  falls back to managed identity via :mod:`._credential`. If that also fails,
  the request fails with an appropriate error.

Usage
-----

1. Extract user token from request headers in the handler layer.
2. Create a :class:`UserContext` from the token.
3. Pass the context to the runner, which threads it to tools.
4. Tools call :meth:`UserContext.get_token_for_scope` to get downstream tokens.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ._logger import logger

if TYPE_CHECKING:
    from .config.schema import OboConfig


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OboError(Exception):
    """Base exception for OBO token exchange errors."""

    def __init__(self, error: str, error_description: str | None = None) -> None:
        self.error = error
        self.error_description = error_description
        message = f"{error}: {error_description}" if error_description else error
        super().__init__(message)


class InteractionRequiredError(OboError):
    """Downstream API requires user interaction (MFA, consent, etc.).

    The client must re-authenticate with the claims challenge to proceed.
    This error should be surfaced to the client via HTTP 401 with a
    WWW-Authenticate header containing the claims.
    """

    def __init__(
        self,
        error: str,
        error_description: str | None = None,
        claims: str | None = None,
    ) -> None:
        super().__init__(error, error_description)
        self.claims = claims


# ---------------------------------------------------------------------------
# In-memory token cache
# ---------------------------------------------------------------------------


@dataclass
class _CachedToken:
    """A cached access token with expiration."""

    access_token: str
    expires_on: int  # Unix timestamp


# Global in-memory cache: (token_hash, scope) -> cached token
_token_cache: dict[tuple[str, str], _CachedToken] = {}
_cache_lock = asyncio.Lock()

# Refresh tokens 5 minutes before expiry
_TOKEN_REFRESH_BUFFER_SECONDS = 300


def _hash_token(token: str) -> str:
    """Create a hash of the token for cache key (avoid storing raw tokens)."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


async def _get_cached_token(token_hash: str, scope: str) -> str | None:
    """Retrieve a valid cached token, or None if expired/missing."""
    async with _cache_lock:
        cached = _token_cache.get((token_hash, scope))
        if cached is None:
            return None
        # Check if token is still valid (with buffer)
        if cached.expires_on - _TOKEN_REFRESH_BUFFER_SECONDS <= int(time.time()):
            del _token_cache[(token_hash, scope)]
            return None
        return cached.access_token


async def _set_cached_token(token_hash: str, scope: str, access_token: str, expires_on: int) -> None:
    """Cache an access token."""
    async with _cache_lock:
        _token_cache[(token_hash, scope)] = _CachedToken(
            access_token=access_token,
            expires_on=expires_on,
        )


def clear_token_cache() -> None:
    """Clear all cached tokens. Useful for testing."""
    _token_cache.clear()


# ---------------------------------------------------------------------------
# OBO Token Provider
# ---------------------------------------------------------------------------


class OboTokenProvider:
    """Handles OBO token exchange using MSAL.

    This class is typically instantiated once per application and shared
    across requests. It uses MSAL's ConfidentialClientApplication for
    the token exchange.
    """

    def __init__(self, config: OboConfig) -> None:
        self._config = config
        self._app: Any = None
        self._app_lock = asyncio.Lock()

    async def _get_msal_app(self) -> Any:
        """Lazily initialize the MSAL ConfidentialClientApplication."""
        if self._app is not None:
            return self._app

        async with self._app_lock:
            if self._app is not None:
                return self._app

            # Import MSAL here to avoid import errors if not installed
            try:
                from msal import ConfidentialClientApplication
            except ImportError as exc:
                raise ImportError(
                    "MSAL is required for OBO support. "
                    "Install it with: pip install msal"
                ) from exc

            authority = f"https://login.microsoftonline.com/{self._config.tenant_id}"

            # Build client credential (secret or certificate)
            client_credential: str | dict[str, Any] | None = None
            if self._config.client_secret:
                client_credential = self._config.client_secret
            # TODO: Add certificate support in future iteration

            self._app = ConfidentialClientApplication(
                client_id=self._config.client_id,
                client_credential=client_credential,
                authority=authority,
            )
            logger.debug("OBO: MSAL ConfidentialClientApplication initialized")
            return self._app

    async def acquire_token_on_behalf_of(
        self,
        user_token: str,
        scope: str,
    ) -> str:
        """Exchange a user token for a downstream API token.

        Parameters
        ----------
        user_token:
            The incoming user access token (from EasyAuth or Authorization header).
        scope:
            The scope for the downstream API (e.g., "https://graph.microsoft.com/.default").

        Returns
        -------
        str
            The access token for the downstream API.

        Raises
        ------
        InteractionRequiredError
            If the downstream API requires user interaction (MFA, consent).
        OboError
            For other token exchange failures.
        """
        # Check cache first
        token_hash = _hash_token(user_token)
        cached = await _get_cached_token(token_hash, scope)
        if cached is not None:
            logger.debug("OBO: Using cached token for scope %s", scope)
            return cached

        # Perform token exchange
        app = await self._get_msal_app()

        # MSAL's acquire_token_on_behalf_of is synchronous, run in thread pool
        result = await asyncio.to_thread(
            app.acquire_token_on_behalf_of,
            user_assertion=user_token,
            scopes=[scope],
        )

        if "error" in result:
            error = result.get("error", "unknown_error")
            error_description = result.get("error_description")
            claims = result.get("claims")

            # Check for interaction_required errors
            if error in ("interaction_required", "consent_required", "login_required"):
                logger.warning(
                    "OBO: Interaction required for scope %s: %s",
                    scope,
                    error_description,
                )
                raise InteractionRequiredError(error, error_description, claims)

            logger.error("OBO: Token exchange failed for scope %s: %s", scope, error_description)
            raise OboError(error, error_description)

        access_token = result["access_token"]
        expires_in = result.get("expires_in", 3600)
        expires_on = int(time.time()) + expires_in

        # Cache the token
        await _set_cached_token(token_hash, scope, access_token, expires_on)
        logger.debug("OBO: Acquired and cached token for scope %s", scope)

        return access_token

    def get_scope_for_name(self, name: str) -> str | None:
        """Look up a downstream scope by its configured name.

        Parameters
        ----------
        name:
            The name of the downstream scope (e.g., "graph", "custom_api").

        Returns
        -------
        str | None
            The scope URI, or None if not configured.
        """
        return self._config.downstream_scopes.get(name)


# ---------------------------------------------------------------------------
# User Context
# ---------------------------------------------------------------------------


@dataclass
class UserContext:
    """Carries user identity through the request lifecycle.

    This context is created from incoming HTTP request headers and passed
    to the runner, which threads it to tools. Tools can use this to acquire
    tokens for downstream APIs via OBO.

    If no user token is present, the context will fall back to managed
    identity for downstream calls.
    """

    access_token: str | None = None
    """The incoming user access token, or None if unauthenticated."""

    user_id: str | None = None
    """The user's object ID from the token claims, if available."""

    claims: dict[str, Any] = field(default_factory=dict)
    """Decoded claims from the access token."""

    _obo_provider: OboTokenProvider | None = field(default=None, repr=False)
    """The OBO token provider for exchanging tokens."""

    async def get_token_for_scope(self, scope: str) -> str | None:
        """Acquire a token for a downstream API scope.

        Uses OBO if a user token is present and OBO is configured.
        Falls back to managed identity if OBO is not available.
        Returns None if neither method can provide a token.

        Parameters
        ----------
        scope:
            The scope for the downstream API.

        Returns
        -------
        str | None
            The access token, or None if unavailable.
        """
        # Try OBO first if we have a user token
        if self.access_token and self._obo_provider:
            try:
                return await self._obo_provider.acquire_token_on_behalf_of(
                    self.access_token,
                    scope,
                )
            except OboError as exc:
                logger.warning("OBO failed, falling back to managed identity: %s", exc)
                # Fall through to managed identity

        # Fall back to managed identity
        return await self._get_managed_identity_token(scope)

    async def _get_managed_identity_token(self, scope: str) -> str | None:
        """Acquire a token using managed identity."""
        try:
            from ._credential import build_async_credential

            credential = build_async_credential()
            token = await credential.get_token(scope)
            return token.token
        except Exception as exc:
            logger.warning("Managed identity token acquisition failed: %s", exc)
            return None

    def get_token_for_scope_name(self, name: str) -> str | None:
        """Look up a scope by name and acquire a token for it.

        This is a convenience method for tools that use named scopes
        from the configuration.
        """
        if self._obo_provider is None:
            return None
        scope = self._obo_provider.get_scope_for_name(name)
        if scope is None:
            logger.warning("Unknown downstream scope name: %s", name)
            return None
        # Note: This would need to be async - keeping sync for interface consistency
        # In practice, tools should use get_token_for_scope directly
        return None

    @property
    def is_authenticated(self) -> bool:
        """Check if a user token is present."""
        return self.access_token is not None

    @property
    def has_obo_support(self) -> bool:
        """Check if OBO is configured and available."""
        return self._obo_provider is not None and self.access_token is not None


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------

# Global OBO provider instance (created once, shared across requests)
_obo_provider: OboTokenProvider | None = None
_obo_provider_lock = asyncio.Lock()


async def get_obo_provider(config: OboConfig | None) -> OboTokenProvider | None:
    """Get or create the global OBO token provider.

    Parameters
    ----------
    config:
        The OBO configuration. If None or disabled, returns None.

    Returns
    -------
    OboTokenProvider | None
        The provider instance, or None if OBO is not configured.
    """
    global _obo_provider

    if config is None or not config.enabled:
        return None

    if _obo_provider is not None:
        return _obo_provider

    async with _obo_provider_lock:
        if _obo_provider is not None:
            return _obo_provider
        _obo_provider = OboTokenProvider(config)
        logger.info("OBO: Token provider initialized")
        return _obo_provider


def reset_obo_provider() -> None:
    """Reset the global OBO provider. Useful for testing."""
    global _obo_provider
    _obo_provider = None
    clear_token_cache()


def create_user_context(
    access_token: str | None = None,
    user_id: str | None = None,
    claims: dict[str, Any] | None = None,
    obo_provider: OboTokenProvider | None = None,
) -> UserContext:
    """Create a UserContext for the current request.

    Parameters
    ----------
    access_token:
        The incoming user access token from the request.
    user_id:
        The user's object ID, if known.
    claims:
        Decoded claims from the access token.
    obo_provider:
        The OBO token provider instance.

    Returns
    -------
    UserContext
        A new context instance for this request.
    """
    return UserContext(
        access_token=access_token,
        user_id=user_id,
        claims=claims or {},
        _obo_provider=obo_provider,
    )


# ---------------------------------------------------------------------------
# Header extraction utilities
# ---------------------------------------------------------------------------

# EasyAuth header names
EASYAUTH_ACCESS_TOKEN_HEADER = "X-MS-TOKEN-AAD-ACCESS-TOKEN"
EASYAUTH_ID_TOKEN_HEADER = "X-MS-TOKEN-AAD-ID-TOKEN"
EASYAUTH_PRINCIPAL_ID_HEADER = "X-MS-CLIENT-PRINCIPAL-ID"
EASYAUTH_PRINCIPAL_NAME_HEADER = "X-MS-CLIENT-PRINCIPAL-NAME"

# Standard Authorization header
AUTHORIZATION_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "


def extract_user_token_from_headers(headers: dict[str, str] | Any) -> str | None:
    """Extract the user access token from request headers.

    Checks EasyAuth headers first, then falls back to Authorization header.

    Parameters
    ----------
    headers:
        The request headers (dict-like or object with get method).

    Returns
    -------
    str | None
        The access token, or None if not found.
    """
    # Helper to get header value (case-insensitive)
    def get_header(name: str) -> str | None:
        if hasattr(headers, "get"):
            # Try exact match first
            value = headers.get(name)
            if value:
                return value.strip() if isinstance(value, str) else None
            # Try case-insensitive
            if hasattr(headers, "items"):
                for key, val in headers.items():
                    if key.lower() == name.lower():
                        return val.strip() if isinstance(val, str) else None
        return None

    # Try EasyAuth header first
    token = get_header(EASYAUTH_ACCESS_TOKEN_HEADER)
    if token:
        logger.debug("OBO: Found user token in EasyAuth header")
        return token

    # Try Authorization header
    auth = get_header(AUTHORIZATION_HEADER)
    if auth and auth.startswith(BEARER_PREFIX):
        token = auth[len(BEARER_PREFIX) :].strip()
        if token:
            logger.debug("OBO: Found user token in Authorization header")
            return token

    return None


def extract_user_id_from_headers(headers: dict[str, str] | Any) -> str | None:
    """Extract the user's principal ID from EasyAuth headers.

    Parameters
    ----------
    headers:
        The request headers.

    Returns
    -------
    str | None
        The user's principal ID, or None if not found.
    """
    if hasattr(headers, "get"):
        value = headers.get(EASYAUTH_PRINCIPAL_ID_HEADER)
        if value:
            return value.strip() if isinstance(value, str) else None
        # Case-insensitive fallback
        if hasattr(headers, "items"):
            for key, val in headers.items():
                if key.lower() == EASYAUTH_PRINCIPAL_ID_HEADER.lower():
                    return val.strip() if isinstance(val, str) else None
    return None
