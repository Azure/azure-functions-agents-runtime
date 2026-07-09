"""Tests for On-Behalf-Of (OBO) authentication support."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from azure_functions_agents._obo import (
    BIGMAC_ACCESS_TOKEN_HEADER,
    BIGMAC_HOOKS_SESSION_TOKEN_HEADER,
    EASYAUTH_ID_TOKEN_HEADER,
    InteractionRequiredError,
    OboError,
    OboTokenProvider,
    clear_token_cache,
    create_user_context,
    extract_hooks_session_token_from_headers,
    extract_user_id_from_headers,
    extract_user_token_from_headers,
    get_obo_provider,
    reset_obo_provider,
)
from azure_functions_agents.config.schema import AuthConfig, OboConfig

# ---------------------------------------------------------------------------
# OboConfig tests
# ---------------------------------------------------------------------------


class TestOboConfig:
    """Tests for OboConfig schema validation."""

    def test_valid_obo_config(self) -> None:
        """OboConfig should accept valid configuration."""
        config = OboConfig(
            enabled=True,
            client_id="test-client-id",
            client_secret="test-secret",
            tenant_id="test-tenant-id",
            downstream_scopes={"graph": "https://graph.microsoft.com/.default"},
        )
        assert config.enabled is True
        assert config.client_id == "test-client-id"
        assert config.client_secret == "test-secret"
        assert config.tenant_id == "test-tenant-id"
        assert config.downstream_scopes == {"graph": "https://graph.microsoft.com/.default"}

    def test_obo_config_defaults(self) -> None:
        """OboConfig should have sensible defaults."""
        config = OboConfig(
            client_id="test-client-id",
            tenant_id="test-tenant-id",
        )
        assert config.enabled is True
        assert config.client_secret is None
        assert config.downstream_scopes == {}

    def test_obo_config_empty_client_id_rejected(self) -> None:
        """OboConfig should reject empty client_id."""
        with pytest.raises(ValueError, match="value must be non-empty"):
            OboConfig(
                client_id="  ",
                tenant_id="test-tenant-id",
            )

    def test_obo_config_empty_tenant_id_rejected(self) -> None:
        """OboConfig should reject empty tenant_id."""
        with pytest.raises(ValueError, match="value must be non-empty"):
            OboConfig(
                client_id="test-client-id",
                tenant_id="",
            )


class TestAuthConfig:
    """Tests for AuthConfig schema."""

    def test_auth_config_with_obo(self) -> None:
        """AuthConfig should wrap OboConfig."""
        obo = OboConfig(
            client_id="test-client-id",
            tenant_id="test-tenant-id",
        )
        config = AuthConfig(obo=obo)
        assert config.obo is not None
        assert config.obo.client_id == "test-client-id"

    def test_auth_config_without_obo(self) -> None:
        """AuthConfig should allow None obo."""
        config = AuthConfig()
        assert config.obo is None


# ---------------------------------------------------------------------------
# Header extraction tests
# ---------------------------------------------------------------------------


class TestHeaderExtraction:
    """Tests for token extraction from headers."""

    def test_extract_from_easyauth_header(self) -> None:
        """Should extract token from EasyAuth header."""
        headers = {"X-MS-TOKEN-AAD-ACCESS-TOKEN": "test-token-123"}
        token = extract_user_token_from_headers(headers)
        assert token == "test-token-123"

    def test_extract_from_bigmac_access_token_header(self) -> None:
        """Should extract token from BigMac access token header."""
        headers = {BIGMAC_ACCESS_TOKEN_HEADER: "bigmac-token-123"}
        token = extract_user_token_from_headers(headers)
        assert token == "bigmac-token-123"

    def test_extract_from_authorization_header(self) -> None:
        """Should extract token from Authorization header."""
        headers = {"Authorization": "Bearer test-token-456"}
        token = extract_user_token_from_headers(headers)
        assert token == "test-token-456"

    def test_easyauth_takes_precedence(self) -> None:
        """EasyAuth header should take precedence over Authorization."""
        headers = {
            "X-MS-TOKEN-AAD-ACCESS-TOKEN": "easyauth-token",
            "Authorization": "Bearer auth-token",
        }
        token = extract_user_token_from_headers(headers)
        assert token == "easyauth-token"

    def test_bigmac_takes_precedence(self) -> None:
        """BigMac access token header should take precedence over other token headers."""
        headers = {
            BIGMAC_ACCESS_TOKEN_HEADER: "bigmac-token",
            "X-MS-TOKEN-AAD-ACCESS-TOKEN": "easyauth-token",
            "Authorization": "Bearer auth-token",
        }
        token = extract_user_token_from_headers(headers)
        assert token == "bigmac-token"

    def test_extract_id_token_fallback(self) -> None:
        """Should fall back to EasyAuth ID token when access token is missing."""
        headers = {EASYAUTH_ID_TOKEN_HEADER: "id-token-value"}
        token = extract_user_token_from_headers(headers)
        assert token == "id-token-value"

    def test_no_token_returns_none(self) -> None:
        """Should return None when no token is present."""
        headers = {"Content-Type": "application/json"}
        token = extract_user_token_from_headers(headers)
        assert token is None

    def test_case_insensitive_header_lookup(self) -> None:
        """Should handle case-insensitive header names."""
        headers = {"x-ms-token-aad-access-token": "lower-case-token"}
        token = extract_user_token_from_headers(headers)
        assert token == "lower-case-token"

    def test_extract_user_id_from_headers(self) -> None:
        """Should extract user ID from EasyAuth principal header."""
        headers = {"X-MS-CLIENT-PRINCIPAL-ID": "user-object-id-123"}
        user_id = extract_user_id_from_headers(headers)
        assert user_id == "user-object-id-123"

    def test_extract_hooks_session_token_from_headers(self) -> None:
        """Should extract hooks session token from BigMac header."""
        headers = {BIGMAC_HOOKS_SESSION_TOKEN_HEADER: "hooks-session-123"}
        token = extract_hooks_session_token_from_headers(headers)
        assert token == "hooks-session-123"

    def test_extract_hooks_session_token_case_insensitive(self) -> None:
        """Should extract hooks session token with case-insensitive lookup."""
        headers = {"x-ms-hooks-session-token": "hooks-session-456"}
        token = extract_hooks_session_token_from_headers(headers)
        assert token == "hooks-session-456"

    def test_user_id_missing_returns_none(self) -> None:
        """Should return None when user ID header is missing."""
        headers = {}
        user_id = extract_user_id_from_headers(headers)
        assert user_id is None


# ---------------------------------------------------------------------------
# UserContext tests
# ---------------------------------------------------------------------------


class TestUserContext:
    """Tests for UserContext creation and behavior."""

    def test_create_user_context_with_token(self) -> None:
        """Should create context with access token."""
        context = create_user_context(
            access_token="test-token",
            hooks_session_token="hooks-token",
            user_id="user-123",
        )
        assert context.access_token == "test-token"
        assert context.hooks_session_token == "hooks-token"
        assert context.user_id == "user-123"
        assert context.is_authenticated is True

    def test_create_user_context_without_token(self) -> None:
        """Should create context without access token."""
        context = create_user_context()
        assert context.access_token is None
        assert context.is_authenticated is False

    def test_has_obo_support_requires_both(self) -> None:
        """has_obo_support should require both token and provider."""
        # No token, no provider
        context = create_user_context()
        assert context.has_obo_support is False

        # Token but no provider
        context = create_user_context(access_token="test-token")
        assert context.has_obo_support is False

        # Both token and provider
        mock_provider = MagicMock()
        context = create_user_context(
            access_token="test-token",
            obo_provider=mock_provider,
        )
        assert context.has_obo_support is True


# ---------------------------------------------------------------------------
# OboTokenProvider tests
# ---------------------------------------------------------------------------


class TestOboTokenProvider:
    """Tests for OBO token exchange."""

    def setup_method(self) -> None:
        """Clear token cache before each test."""
        clear_token_cache()

    @pytest.fixture
    def obo_config(self) -> OboConfig:
        """Create a test OBO config."""
        return OboConfig(
            client_id="test-client-id",
            client_secret="test-secret",
            tenant_id="test-tenant-id",
            downstream_scopes={"graph": "https://graph.microsoft.com/.default"},
        )

    @pytest.fixture
    def provider(self, obo_config: OboConfig) -> OboTokenProvider:
        """Create a test OBO provider."""
        return OboTokenProvider(obo_config)

    def test_get_scope_for_name(self, provider: OboTokenProvider) -> None:
        """Should return scope for configured name."""
        scope = provider.get_scope_for_name("graph")
        assert scope == "https://graph.microsoft.com/.default"

    def test_get_scope_for_unknown_name(self, provider: OboTokenProvider) -> None:
        """Should return None for unknown scope name."""
        scope = provider.get_scope_for_name("unknown")
        assert scope is None

    @pytest.mark.asyncio
    async def test_acquire_token_success(self, provider: OboTokenProvider) -> None:
        """Should successfully acquire token via OBO."""
        mock_result = {
            "access_token": "downstream-token",
            "expires_in": 3600,
        }

        with patch.object(provider, "_get_msal_app") as mock_get_app:
            mock_app = MagicMock()
            mock_app.acquire_token_on_behalf_of.return_value = mock_result
            mock_get_app.return_value = mock_app

            token = await provider.acquire_token_on_behalf_of(
                user_token="user-token",
                scope="https://api.example.com/.default",
            )

            assert token == "downstream-token"

    @pytest.mark.asyncio
    async def test_acquire_token_interaction_required(self, provider: OboTokenProvider) -> None:
        """Should raise InteractionRequiredError when consent needed."""
        mock_result = {
            "error": "interaction_required",
            "error_description": "User consent required",
            "claims": '{"claim": "value"}',
        }

        with patch.object(provider, "_get_msal_app") as mock_get_app:
            mock_app = MagicMock()
            mock_app.acquire_token_on_behalf_of.return_value = mock_result
            mock_get_app.return_value = mock_app

            with pytest.raises(InteractionRequiredError) as exc_info:
                await provider.acquire_token_on_behalf_of(
                    user_token="user-token",
                    scope="https://api.example.com/.default",
                )

            assert exc_info.value.error == "interaction_required"
            assert exc_info.value.error_description == "User consent required"

    @pytest.mark.asyncio
    async def test_acquire_token_generic_error(self, provider: OboTokenProvider) -> None:
        """Should raise OboError for other failures."""
        mock_result = {
            "error": "invalid_grant",
            "error_description": "Token expired",
        }

        with patch.object(provider, "_get_msal_app") as mock_get_app:
            mock_app = MagicMock()
            mock_app.acquire_token_on_behalf_of.return_value = mock_result
            mock_get_app.return_value = mock_app

            with pytest.raises(OboError) as exc_info:
                await provider.acquire_token_on_behalf_of(
                    user_token="user-token",
                    scope="https://api.example.com/.default",
                )

            assert exc_info.value.error == "invalid_grant"


# ---------------------------------------------------------------------------
# Token caching tests
# ---------------------------------------------------------------------------


class TestTokenCaching:
    """Tests for token caching behavior."""

    def setup_method(self) -> None:
        """Clear token cache before each test."""
        clear_token_cache()

    @pytest.fixture
    def obo_config(self) -> OboConfig:
        """Create a test OBO config."""
        return OboConfig(
            client_id="test-client-id",
            client_secret="test-secret",
            tenant_id="test-tenant-id",
        )

    @pytest.mark.asyncio
    async def test_token_is_cached(self, obo_config: OboConfig) -> None:
        """Second call should use cached token."""
        provider = OboTokenProvider(obo_config)
        call_count = 0

        def mock_acquire(*args: Any, **kwargs: Any) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return {
                "access_token": f"token-{call_count}",
                "expires_in": 3600,
            }

        with patch.object(provider, "_get_msal_app") as mock_get_app:
            mock_app = MagicMock()
            mock_app.acquire_token_on_behalf_of.side_effect = mock_acquire
            mock_get_app.return_value = mock_app

            # First call
            token1 = await provider.acquire_token_on_behalf_of(
                user_token="user-token",
                scope="https://api.example.com/.default",
            )

            # Second call should use cache
            token2 = await provider.acquire_token_on_behalf_of(
                user_token="user-token",
                scope="https://api.example.com/.default",
            )

            assert token1 == "token-1"
            assert token2 == "token-1"  # Same cached token
            assert call_count == 1  # Only one MSAL call


# ---------------------------------------------------------------------------
# Error types tests
# ---------------------------------------------------------------------------


class TestErrorTypes:
    """Tests for OBO error types."""

    def test_obo_error_message(self) -> None:
        """OboError should format error message correctly."""
        error = OboError("invalid_grant", "Token has expired")
        assert str(error) == "invalid_grant: Token has expired"

    def test_obo_error_without_description(self) -> None:
        """OboError should handle missing description."""
        error = OboError("unknown_error")
        assert str(error) == "unknown_error"

    def test_interaction_required_error(self) -> None:
        """InteractionRequiredError should carry claims."""
        error = InteractionRequiredError(
            "consent_required",
            "Admin consent needed",
            '{"claims": "data"}',
        )
        assert error.error == "consent_required"
        assert error.error_description == "Admin consent needed"
        assert error.claims == '{"claims": "data"}'


# ---------------------------------------------------------------------------
# Global provider tests
# ---------------------------------------------------------------------------


class TestGlobalProvider:
    """Tests for global OBO provider management."""

    def setup_method(self) -> None:
        """Reset global provider before each test."""
        reset_obo_provider()

    @pytest.mark.asyncio
    async def test_get_obo_provider_creates_instance(self) -> None:
        """get_obo_provider should create provider when config is valid."""
        config = OboConfig(
            client_id="test-client-id",
            tenant_id="test-tenant-id",
        )
        provider = await get_obo_provider(config)
        assert provider is not None
        assert isinstance(provider, OboTokenProvider)

    @pytest.mark.asyncio
    async def test_get_obo_provider_returns_none_when_disabled(self) -> None:
        """get_obo_provider should return None when OBO is disabled."""
        config = OboConfig(
            enabled=False,
            client_id="test-client-id",
            tenant_id="test-tenant-id",
        )
        provider = await get_obo_provider(config)
        assert provider is None

    @pytest.mark.asyncio
    async def test_get_obo_provider_returns_none_for_none_config(self) -> None:
        """get_obo_provider should return None for None config."""
        provider = await get_obo_provider(None)
        assert provider is None

    @pytest.mark.asyncio
    async def test_get_obo_provider_returns_same_instance(self) -> None:
        """get_obo_provider should return the same instance on subsequent calls."""
        config = OboConfig(
            client_id="test-client-id",
            tenant_id="test-tenant-id",
        )
        provider1 = await get_obo_provider(config)
        provider2 = await get_obo_provider(config)
        assert provider1 is provider2
