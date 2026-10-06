from __future__ import annotations

import logging
from unittest.mock import Mock

import pytest
from azure.core.credentials import AccessToken

import azure_functions_agents._mcp_auth as mcp_auth
from azure_functions_agents.discovery.mcp import MCPServerDescriptor

_SCOPE = "https://resource.example/.default"


def _server(
    *,
    headers: dict[str, str] | None = None,
    scope: str | None = None,
    client_id: str | None = None,
) -> MCPServerDescriptor:
    return MCPServerDescriptor.create(
        name="demo",
        url="https://example.com/mcp",
        headers=headers or {},
        auth_scope=scope,
        client_id=client_id,
    )


def _credential_with_close() -> Mock:
    credential = Mock()
    credential.get_token.return_value = AccessToken("test-token", 9999999999)
    credential.close = Mock()
    return credential


@pytest.fixture
def credentials(monkeypatch: pytest.MonkeyPatch) -> tuple[Mock, Mock, Mock]:
    credential = _credential_with_close()
    default_builder = Mock(return_value=credential)
    client_builder = Mock(return_value=credential)
    monkeypatch.setattr(mcp_auth, "build_credential", default_builder)
    monkeypatch.setattr(mcp_auth, "build_credential_with_client_id", client_builder)
    return credential, default_builder, client_builder


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-Test": "yes"}, {"Authorization": "static-value", "X-Test": "yes"}],
)
def test_materialize_static_headers_without_auth(
    headers: dict[str, str], credentials: tuple[Mock, Mock, Mock]
) -> None:
    credential, default_builder, client_builder = credentials
    server = _server(headers=headers)

    first = mcp_auth.materialize_mcp_headers(server)
    second = mcp_auth.materialize_mcp_headers(server)
    first["X-Added"] = "changed"

    assert second == headers
    assert dict(server.headers) == headers
    credential.get_token.assert_not_called()
    credential.close.assert_not_called()
    default_builder.assert_not_called()
    client_builder.assert_not_called()


@pytest.mark.parametrize("scope", ["", " \t\n "])
@pytest.mark.parametrize("headers", [{}, {"Authorization": "static-value", "X-Test": "yes"}])
def test_empty_scope_warns_and_uses_only_static_headers(
    scope: str,
    headers: dict[str, str],
    credentials: tuple[Mock, Mock, Mock],
    caplog: pytest.LogCaptureFixture,
) -> None:
    credential, default_builder, client_builder = credentials
    with caplog.at_level(logging.WARNING):
        server = _server(headers=headers, scope=scope)
        assert [record.getMessage() for record in caplog.records] == [
            "MCP server auth requires a non-empty 'scope'"
        ]
        result = mcp_auth.materialize_mcp_headers(server)
        assert mcp_auth.materialize_mcp_headers(server) == headers
        provider = mcp_auth.build_mcp_header_provider(server)
        assert (provider({}) if provider is not None else {}) == headers

    assert result == headers
    assert [record.getMessage() for record in caplog.records] == [
        "MCP server auth requires a non-empty 'scope'"
    ]
    assert "static-value" not in caplog.text
    credential.get_token.assert_not_called()
    credential.close.assert_not_called()
    default_builder.assert_not_called()
    client_builder.assert_not_called()


@pytest.mark.parametrize("authorization_key", ["Authorization", "authorization", "AUTHORIZATION"])
def test_generated_authorization_overrides_static_headers(
    authorization_key: str,
    credentials: tuple[Mock, Mock, Mock],
    caplog: pytest.LogCaptureFixture,
) -> None:
    credential, default_builder, client_builder = credentials
    with caplog.at_level(logging.DEBUG):
        result = mcp_auth.materialize_mcp_headers(
            _server(headers={authorization_key: "static-value", "X-Test": "yes"}, scope=_SCOPE)
        )

    assert result == {"Authorization": "******", "X-Test": "yes"}
    credential.get_token.assert_called_once_with(_SCOPE)
    credential.close.assert_called_once_with()
    default_builder.assert_called_once_with()
    client_builder.assert_not_called()
    assert "test-token" not in caplog.text
    assert "static-value" not in caplog.text


@pytest.mark.parametrize("client_id", [None, "", "  ", "$MCP_CLIENT_MISSING", "%MCP_CLIENT_MISSING%"])
def test_missing_or_unresolved_client_id_uses_default_credential(
    client_id: str | None, credentials: tuple[Mock, Mock, Mock]
) -> None:
    credential, default_builder, client_builder = credentials
    result = mcp_auth.materialize_mcp_headers(_server(scope=_SCOPE, client_id=client_id))

    assert result == {"Authorization": "******"}
    credential.get_token.assert_called_once_with(_SCOPE)
    credential.close.assert_called_once_with()
    default_builder.assert_called_once_with()
    client_builder.assert_not_called()


def test_resolved_client_id_selects_client_id_credential(
    credentials: tuple[Mock, Mock, Mock],
) -> None:
    credential, default_builder, client_builder = credentials
    result = mcp_auth.materialize_mcp_headers(_server(scope=_SCOPE, client_id=" client-123 "))

    assert result == {"Authorization": "******"}
    credential.get_token.assert_called_once_with(_SCOPE)
    credential.close.assert_called_once_with()
    default_builder.assert_not_called()
    client_builder.assert_called_once_with("client-123")


def test_header_provider_acquires_credential_only_when_used(
    credentials: tuple[Mock, Mock, Mock],
) -> None:
    credential, default_builder, client_builder = credentials
    provider = mcp_auth.build_mcp_header_provider(_server(scope=_SCOPE))

    assert provider is not None
    credential.get_token.assert_not_called()
    credential.close.assert_not_called()
    default_builder.assert_not_called()
    client_builder.assert_not_called()

    assert provider({}) == {"Authorization": "******"}
    default_builder.assert_called_once_with()
    credential.get_token.assert_called_once_with(_SCOPE)
    credential.close.assert_not_called()


def test_materialize_acquires_fresh_token_on_each_call(
    credentials: tuple[Mock, Mock, Mock],
) -> None:
    credential, default_builder, _client_builder = credentials
    credential.get_token.side_effect = [
        AccessToken("first-token", 9999999999),
        AccessToken("second-token", 9999999999),
    ]
    server = _server(scope=_SCOPE)

    assert mcp_auth.materialize_mcp_headers(server) == {"Authorization": "******"}
    assert mcp_auth.materialize_mcp_headers(server) == {"Authorization": "******"}
    assert credential.get_token.call_count == 2
    assert credential.close.call_count == 2
    assert default_builder.call_count == 2


def test_header_provider_reuses_token_until_refresh_boundary(
    credentials: tuple[Mock, Mock, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    credential, default_builder, _client_builder = credentials
    credential.get_token.side_effect = [
        AccessToken("first-token", 1000),
        AccessToken("second-token", 2000),
    ]
    monkeypatch.setattr(mcp_auth.time, "time", lambda: 100)
    provider = mcp_auth.build_mcp_header_provider(_server(scope=_SCOPE, headers={"X-Test": "yes"}))
    assert provider is not None

    assert provider({}) == {"Authorization": "******", "X-Test": "yes"}
    monkeypatch.setattr(mcp_auth.time, "time", lambda: 699)
    assert provider({}) == {"Authorization": "******", "X-Test": "yes"}
    assert credential.get_token.call_count == 1
    credential.close.assert_not_called()

    monkeypatch.setattr(mcp_auth.time, "time", lambda: 700)
    assert provider({}) == {"Authorization": "******", "X-Test": "yes"}
    assert credential.get_token.call_count == 2
    credential.close.assert_not_called()
    default_builder.assert_called_once_with()


def test_failed_token_refresh_never_returns_cached_or_static_authorization(
    credentials: tuple[Mock, Mock, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    credential, default_builder, _client_builder = credentials
    credential.get_token.side_effect = [
        AccessToken("first-token", 1000),
        RuntimeError("test refresh failure"),
        AccessToken("third-token", 2000),
    ]
    monkeypatch.setattr(mcp_auth.time, "time", lambda: 100)
    provider = mcp_auth.build_mcp_header_provider(
        _server(scope=_SCOPE, headers={"Authorization": "static-value"})
    )
    assert provider is not None
    assert provider({}) == {"Authorization": "******"}

    monkeypatch.setattr(mcp_auth.time, "time", lambda: 700)
    with pytest.raises(RuntimeError, match="test refresh failure"):
        provider({})
    assert credential.get_token.call_count == 2

    assert provider({}) == {"Authorization": "******"}
    assert credential.get_token.call_count == 3
    credential.close.assert_not_called()
    default_builder.assert_called_once_with()


def test_token_acquisition_failure_is_not_static_header_success(
    credentials: tuple[Mock, Mock, Mock], caplog: pytest.LogCaptureFixture
) -> None:
    credential, _default_builder, _client_builder = credentials
    credential.get_token.side_effect = RuntimeError("test acquisition failure")

    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError, match="test acquisition failure"):
        mcp_auth.materialize_mcp_headers(
            _server(scope=_SCOPE, headers={"Authorization": "static-value"})
        )

    credential.get_token.assert_called_once_with(_SCOPE)
    credential.close.assert_called_once_with()
    assert "static-value" not in caplog.text


@pytest.mark.parametrize("token", ["", " \t\n "])
def test_empty_acquired_token_is_an_explicit_error(
    token: str, credentials: tuple[Mock, Mock, Mock], caplog: pytest.LogCaptureFixture
) -> None:
    credential, _default_builder, _client_builder = credentials
    credential.get_token.return_value = AccessToken(token, 9999999999)

    with caplog.at_level(logging.DEBUG), pytest.raises(
        ValueError, match="MCP authentication returned an empty token"
    ):
        mcp_auth.materialize_mcp_headers(
            _server(scope=_SCOPE, headers={"Authorization": "static-value"})
        )

    credential.get_token.assert_called_once_with(_SCOPE)
    credential.close.assert_called_once_with()
    assert "static-value" not in caplog.text


def test_empty_token_refresh_never_uses_the_previous_token(
    credentials: tuple[Mock, Mock, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    credential, default_builder, _client_builder = credentials
    credential.get_token.side_effect = [
        AccessToken("first-token", 1000),
        AccessToken("", 2000),
        AccessToken("third-token", 2000),
    ]
    monkeypatch.setattr(mcp_auth.time, "time", lambda: 100)
    provider = mcp_auth.build_mcp_header_provider(_server(scope=_SCOPE))
    assert provider is not None
    assert provider({}) == {"Authorization": "******"}

    monkeypatch.setattr(mcp_auth.time, "time", lambda: 700)
    with pytest.raises(ValueError, match="MCP authentication returned an empty token"):
        provider({})
    assert credential.get_token.call_count == 2

    assert provider({}) == {"Authorization": "******"}
    assert credential.get_token.call_count == 3
    credential.close.assert_not_called()
    default_builder.assert_called_once_with()


def test_credential_construction_failure_propagates(
    credentials: tuple[Mock, Mock, Mock],
) -> None:
    credential, default_builder, _client_builder = credentials
    default_builder.side_effect = RuntimeError("test credential failure")

    with pytest.raises(RuntimeError, match="test credential failure"):
        mcp_auth.materialize_mcp_headers(_server(scope=_SCOPE))
    credential.get_token.assert_not_called()
    credential.close.assert_not_called()


def test_materialize_closes_owned_credential_on_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    success = _credential_with_close()
    failure = _credential_with_close()
    failure.get_token.side_effect = RuntimeError("test acquisition failure")
    default_builder = Mock(side_effect=[success, failure])
    monkeypatch.setattr(mcp_auth, "build_credential", default_builder)
    monkeypatch.setattr(mcp_auth, "build_credential_with_client_id", Mock())

    assert mcp_auth.materialize_mcp_headers(_server(scope=_SCOPE)) == {"Authorization": "******"}
    success.close.assert_called_once_with()

    with pytest.raises(RuntimeError, match="test acquisition failure"):
        mcp_auth.materialize_mcp_headers(_server(scope=_SCOPE))
    failure.close.assert_called_once_with()


def test_materialize_surfaces_credential_close_failure_without_primary_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    credential = _credential_with_close()
    credential.close.side_effect = RuntimeError("close failure")
    monkeypatch.setattr(mcp_auth, "build_credential", Mock(return_value=credential))
    monkeypatch.setattr(mcp_auth, "build_credential_with_client_id", Mock())

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="close failure"):
        mcp_auth.materialize_mcp_headers(_server(scope=_SCOPE))
    credential.close.assert_called_once_with()
    assert "MCP credential cleanup failed." in caplog.text


def test_materialize_preserves_primary_failure_when_credential_close_also_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    credential = _credential_with_close()
    credential.get_token.side_effect = RuntimeError("token failure")
    credential.close.side_effect = RuntimeError("close failure")
    monkeypatch.setattr(mcp_auth, "build_credential", Mock(return_value=credential))
    monkeypatch.setattr(mcp_auth, "build_credential_with_client_id", Mock())

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="token failure"):
        mcp_auth.materialize_mcp_headers(_server(scope=_SCOPE))

    credential.close.assert_called_once_with()
    assert "MCP credential cleanup failed." in caplog.text


def test_header_provider_is_none_without_auth_or_static_headers(
    credentials: tuple[Mock, Mock, Mock],
) -> None:
    credential, default_builder, client_builder = credentials

    assert mcp_auth.build_mcp_header_provider(_server()) is None
    assert mcp_auth.materialize_mcp_headers(_server()) == {}
    credential.get_token.assert_not_called()
    credential.close.assert_not_called()
    default_builder.assert_not_called()
    client_builder.assert_not_called()
