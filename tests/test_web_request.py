"""Tests for the ``web_request`` built-in system tool.

Covers the pydantic param schema, response shaping, the full SSRF validator
(using an injectable resolver — never real DNS/network), header policy, and
telemetry. All HTTP I/O is stubbed via a fake ``aiohttp.ClientSession``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from pydantic import ValidationError

from azure_functions_agents.config.schema import WebRequestConfig
from azure_functions_agents.system_tools import web_request as wr

# ---------------------------------------------------------------------------
# Fake aiohttp harness — no real network access anywhere in this test module.
# ---------------------------------------------------------------------------


class _FakeContent:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def iter_chunked(self, chunk_size: int) -> AsyncIterator[bytes]:
        async def _gen() -> AsyncIterator[bytes]:
            for i in range(0, len(self._data), chunk_size):
                yield self._data[i : i + chunk_size]

        return _gen()


class _FakeResponse:
    def __init__(self, *, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.content = _FakeContent(body)

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeRequestCM:
    def __init__(self, response: _FakeResponse | Exception) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeSession:
    def __init__(self, response: _FakeResponse | Exception, captured: dict[str, Any]) -> None:
        self._response = response
        self._captured = captured

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeRequestCM:
        self._captured["method"] = method
        self._captured["url"] = url
        self._captured.update(kwargs)
        return _FakeRequestCM(self._response)


def _install_fake_http(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    raise_exc: Exception | None = None,
) -> dict[str, Any]:
    """Stub ``aiohttp.ClientSession`` so no real socket is ever touched.

    Returns a dict that will be populated with the captured request kwargs
    (method, url, headers, data, allow_redirects, timeout) and the
    connector passed to ``ClientSession``.
    """
    captured: dict[str, Any] = {}
    response: _FakeResponse | Exception = (
        raise_exc if raise_exc is not None else _FakeResponse(status=status, headers=headers or {}, body=body)
    )

    def _fake_client_session(*, connector: Any = None) -> _FakeSession:
        captured["connector"] = connector
        return _FakeSession(response, captured)

    monkeypatch.setattr(wr.aiohttp, "ClientSession", _fake_client_session)
    return captured


def _stub_resolver(monkeypatch: pytest.MonkeyPatch, ips: list[str]) -> None:
    async def _fake_resolve(host: str) -> list[str]:
        return ips

    monkeypatch.setattr(wr, "_resolve_host", _fake_resolve)


def _build_tool(config: WebRequestConfig | None = None) -> Any:
    tools = wr.create_web_request_tools(config or WebRequestConfig())
    assert len(tools) == 1
    return tools[0]


def _call(tool: Any, **kwargs: Any) -> dict[str, Any]:
    raw = asyncio.run(tool.func(**kwargs))
    result: dict[str, Any] = json.loads(raw)
    return result


# ---------------------------------------------------------------------------
# Telemetry capture — replaces start_span/record_web_request with fakes so
# tests can assert on span attributes/errors and counter increments without
# depending on a real OTel provider being configured.
# ---------------------------------------------------------------------------


class _CapturedSpan:
    def __init__(self, attributes: dict[str, Any]) -> None:
        self.attributes: dict[str, Any] = dict(attributes)
        self.errors: list[tuple[str, str]] = []
        self.exceptions: list[BaseException] = []

    def set_attribute(self, key: str, value: Any) -> None:
        if value is not None:
            self.attributes[key] = value

    def set_content(self, key: str, value: str) -> None:
        self.attributes[key] = value

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        pass

    def set_error(self, message: str, *, fault_domain: str) -> None:
        self.errors.append((message, fault_domain))

    def record_exception(self, exc: BaseException, *, fault_domain: str | None = None) -> None:
        self.exceptions.append(exc)


def _install_span_capture(monkeypatch: pytest.MonkeyPatch) -> list[_CapturedSpan]:
    spans: list[_CapturedSpan] = []

    @contextlib.contextmanager
    def _fake_start_span(
        name: str,
        *,
        fault_domain: str | None = None,
        lifecycle_stage: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[_CapturedSpan]:
        span = _CapturedSpan(attributes or {})
        spans.append(span)
        yield span

    monkeypatch.setattr(wr, "start_span", _fake_start_span)
    return spans


def _install_counter_capture(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    calls: list[bool] = []
    monkeypatch.setattr(wr, "record_web_request", lambda *, error: calls.append(error))
    return calls


# ---------------------------------------------------------------------------
# Param schema
# ---------------------------------------------------------------------------


def test_params_method_defaults_to_get() -> None:
    params = wr.WebRequestParams(url="https://example.com")
    assert params.method == "GET"


def test_params_method_rejects_unknown_verb() -> None:
    with pytest.raises(ValidationError):
        wr.WebRequestParams(url="https://example.com", method="TRACE")


def test_params_body_and_json_are_mutually_exclusive() -> None:
    with pytest.raises(ValidationError):
        wr.WebRequestParams(url="https://example.com", body="x", json={"a": 1})


def test_params_json_alias_accepted_and_exposed_in_schema() -> None:
    params = wr.WebRequestParams(url="https://example.com", json={"a": 1})
    assert params.json_body == {"a": 1}
    schema = wr.WebRequestParams.model_json_schema()
    assert "json" in schema["properties"]
    assert "json_body" not in schema["properties"]


# ---------------------------------------------------------------------------
# Response shaping
# ---------------------------------------------------------------------------


def test_response_json_content_type_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_resolver(monkeypatch, ["93.184.216.34"])
    _install_fake_http(
        monkeypatch,
        status=200,
        headers={"Content-Type": "application/json"},
        body=b'{"hello": "world"}',
    )
    tool = _build_tool()
    result = _call(tool, url="https://example.com/api")
    assert result["status"] == 200
    assert result["body"] == {"hello": "world"}
    assert result["body_truncated"] is False
    assert result["body_omitted_reason"] is None


def test_response_text_content_type_is_raw_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_resolver(monkeypatch, ["93.184.216.34"])
    _install_fake_http(
        monkeypatch, status=200, headers={"Content-Type": "text/plain"}, body=b"hello world"
    )
    tool = _build_tool()
    result = _call(tool, url="https://example.com/text")
    assert result["body"] == "hello world"
    assert result["body_omitted_reason"] is None


def test_response_binary_content_type_omits_body(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_resolver(monkeypatch, ["93.184.216.34"])
    _install_fake_http(
        monkeypatch,
        status=200,
        headers={"Content-Type": "image/png"},
        body=b"\x89PNG\r\n\x1a\n",
    )
    tool = _build_tool()
    result = _call(tool, url="https://example.com/image.png")
    assert result["body"] is None
    assert result["body_omitted_reason"] == "binary"
    assert result["content_type"] == "image/png"
    assert result["response_bytes"] == 8


def test_response_head_method_omits_body(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_resolver(monkeypatch, ["93.184.216.34"])
    _install_fake_http(
        monkeypatch, status=200, headers={"Content-Type": "application/json"}, body=b""
    )
    tool = _build_tool()
    result = _call(tool, method="HEAD", url="https://example.com/api")
    assert result["body"] is None
    assert result["body_omitted_reason"] == "head"


def test_response_truncation_never_parses_json_and_sets_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_resolver(monkeypatch, ["93.184.216.34"])
    big_body = json.dumps({"data": "x" * 100}).encode("utf-8")
    _install_fake_http(
        monkeypatch, status=200, headers={"Content-Type": "application/json"}, body=big_body
    )
    tool = _build_tool(WebRequestConfig(max_response_bytes=10))
    result = _call(tool, url="https://example.com/api")
    assert result["body_truncated"] is True
    assert isinstance(result["body"], str)
    assert len(result["body"]) == 10


def test_oversized_response_is_truncated_not_a_hard_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_resolver(monkeypatch, ["93.184.216.34"])
    _install_fake_http(
        monkeypatch, status=200, headers={"Content-Type": "text/plain"}, body=b"a" * 1000
    )
    tool = _build_tool(WebRequestConfig(max_response_bytes=100))
    result = _call(tool, url="https://example.com/big")
    assert result["status"] == 200
    assert result["body_truncated"] is True
    assert "error" not in result


def test_response_url_strips_query_and_userinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_resolver(monkeypatch, ["93.184.216.34"])
    _install_fake_http(monkeypatch, status=200, headers={}, body=b"")
    tool = _build_tool()
    result = _call(tool, url="https://example.com/path?secret=1&x=2")
    assert result["url"] == "https://example.com/path"
    assert "secret" not in result["url"]


# ---------------------------------------------------------------------------
# SSRF validator — the security-critical suite. Every test injects the
# resolver; none touch real DNS or network.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "169.254.169.254",  # IMDS
        "127.0.0.1",  # loopback
        "10.0.0.5",  # private (RFC1918)
        "169.254.1.1",  # link-local
        "100.64.0.1",  # CGNAT
        "0.0.0.0",  # unspecified
        "224.0.0.1",  # multicast
        "240.0.0.1",  # reserved
        "::1",  # loopback (v6)
        "fc00::1",  # ULA
        "fe80::1",  # link-local (v6)
    ],
)
def test_ssrf_blocks_non_global_unicast_ips(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    _stub_resolver(monkeypatch, [ip])
    captured = _install_fake_http(monkeypatch, status=200, headers={}, body=b"")
    tool = _build_tool()
    result = _call(tool, url="https://internal.example.com/")
    assert "error" in result
    assert "blocked" in result["error"]
    # The blocked request must never reach the HTTP layer.
    assert "connector" not in captured


def test_ssrf_blocks_ipv4_mapped_ipv6_imds(monkeypatch: pytest.MonkeyPatch) -> None:
    # ::ffff:169.254.169.254 is a literal IP in the URL itself (no resolver
    # involved) — the validator must unwrap the IPv4-mapped form before
    # classifying it.
    _install_fake_http(monkeypatch, status=200, headers={}, body=b"")
    tool = _build_tool()
    result = _call(tool, url="http://[::ffff:169.254.169.254]/", method="GET")
    assert "error" in result


@pytest.mark.parametrize(
    "host",
    [
        "2130706433",  # decimal for 127.0.0.1
        "0x7f000001",  # hex for 127.0.0.1
        "0177.0.0.1",  # octal-looking first octet
    ],
)
def test_ssrf_blocks_non_canonical_numeric_ip_literals(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    # These never reach the resolver — rejected purely by hostname shape.
    def _fail_resolve(_host: str) -> list[str]:
        raise AssertionError("resolver must not be called for disguised IP literals")

    monkeypatch.setattr(wr, "_resolve_host", _fail_resolve)
    tool = _build_tool()
    result = _call(tool, url=f"https://{host}/")
    assert "error" in result


def test_ssrf_rejects_embedded_userinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _build_tool()
    result = _call(tool, url="https://user:pass@example.com/")
    assert "error" in result


def test_ssrf_allowlist_allows_exact_match(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_resolver(monkeypatch, ["93.184.216.34"])
    _install_fake_http(monkeypatch, status=204, headers={}, body=b"")
    tool = _build_tool(WebRequestConfig(allowed_hosts=["api.example.com"]))
    result = _call(tool, url="https://api.example.com/ping")
    assert result["status"] == 204


def test_ssrf_allowlist_denies_non_matching_host(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _build_tool(WebRequestConfig(allowed_hosts=["api.example.com"]))
    result = _call(tool, url="https://other.example.com/ping")
    assert "error" in result


def test_ssrf_allowlist_checked_before_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail_resolve(_host: str) -> list[str]:
        raise AssertionError("resolver must not be called when allowlist rejects the host")

    monkeypatch.setattr(wr, "_resolve_host", _fail_resolve)
    tool = _build_tool(WebRequestConfig(allowed_hosts=["api.example.com"]))
    result = _call(tool, url="https://not-allowed.example.com/ping")
    assert "error" in result


def test_ssrf_allowlist_matches_host_with_single_trailing_dot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1 normalization strips exactly ONE trailing dot before exact-match."""
    _stub_resolver(monkeypatch, ["93.184.216.34"])
    _install_fake_http(monkeypatch, status=204, headers={}, body=b"")
    tool = _build_tool(WebRequestConfig(allowed_hosts=["api.example.com"]))
    result = _call(tool, url="https://api.example.com./ping")
    assert result["status"] == 204


def test_ssrf_allowlist_rejects_host_with_double_trailing_dot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only ONE trailing dot is stripped — a double trailing dot must not match."""

    def _fail_resolve(_host: str) -> list[str]:
        raise AssertionError("resolver must not be called when allowlist rejects the host")

    monkeypatch.setattr(wr, "_resolve_host", _fail_resolve)
    tool = _build_tool(WebRequestConfig(allowed_hosts=["api.example.com"]))
    result = _call(tool, url="https://api.example.com../ping")
    assert "error" in result


def test_ssrf_rejects_non_http_scheme_ftp(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail_resolve(_host: str) -> list[str]:
        raise AssertionError("resolver must not be called for a rejected scheme")

    monkeypatch.setattr(wr, "_resolve_host", _fail_resolve)
    tool = _build_tool()
    result = _call(tool, url="ftp://example.com/")
    assert "error" in result


def test_ssrf_rejects_non_http_scheme_file(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail_resolve(_host: str) -> list[str]:
        raise AssertionError("resolver must not be called for a rejected scheme")

    monkeypatch.setattr(wr, "_resolve_host", _fail_resolve)
    tool = _build_tool()
    result = _call(tool, url="file:///etc/passwd")
    assert "error" in result


def test_https_floor_rejects_http_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _build_tool()
    result = _call(tool, url="http://example.com/")
    assert "error" in result


def test_require_https_false_allows_http(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_resolver(monkeypatch, ["93.184.216.34"])
    _install_fake_http(monkeypatch, status=200, headers={}, body=b"ok")
    tool = _build_tool(WebRequestConfig(require_https=False))
    result = _call(tool, url="http://example.com/")
    assert result["status"] == 200


def test_ssrf_rejects_malformed_url_missing_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _build_tool()
    result = _call(tool, url="example.com/path")
    assert "error" in result


def test_ssrf_rejects_invalid_port(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _build_tool()
    result = _call(tool, url="https://example.com:99999/")
    assert "error" in result


def test_dns_rebind_all_ips_must_pass_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """If any resolved IP is blocked, the whole request is rejected (multi-answer DNS)."""
    _stub_resolver(monkeypatch, ["93.184.216.34", "127.0.0.1"])
    tool = _build_tool()
    result = _call(tool, url="https://multi.example.com/")
    assert "error" in result


# ---------------------------------------------------------------------------
# IP pinning — connection must be pinned to the validated IP(s); Host/SNI use
# the original hostname (we never rewrite the URL's host to a raw IP), and
# DNS caching is disabled on the connector.
# ---------------------------------------------------------------------------


def test_ip_pinning_uses_validated_ip_and_preserves_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_resolver(monkeypatch, ["93.184.216.34"])
    captured = _install_fake_http(monkeypatch, status=200, headers={}, body=b"")
    tool = _build_tool()
    _call(tool, url="https://example.com/path")

    connector = captured["connector"]
    assert isinstance(connector._resolver, wr._PinnedResolver)
    assert connector._resolver._ips == ("93.184.216.34",)
    assert connector.use_dns_cache is False
    # Host/SNI: the request URL keeps the original hostname (not the IP) —
    # aiohttp derives the Host header and TLS SNI from it automatically.
    assert "example.com" in captured["url"]
    assert "93.184.216.34" not in captured["url"]


def test_pinned_resolver_resolve_returns_pinned_ips() -> None:
    resolver = wr._PinnedResolver(("203.0.113.5",))
    results = asyncio.run(resolver.resolve("example.com", 443))
    assert len(results) == 1
    assert results[0]["host"] == "203.0.113.5"
    asyncio.run(resolver.close())


# ---------------------------------------------------------------------------
# Header policy
# ---------------------------------------------------------------------------


def _mixed_case(name: str) -> str:
    """Alternate-case a header name to prove case-insensitive matching."""
    return "".join(c.upper() if i % 2 == 0 else c.lower() for i, c in enumerate(name))


@pytest.mark.parametrize("denied_header", sorted(wr._REQUEST_HEADER_DENYLIST))
def test_request_header_denylist_strips_full_set(
    monkeypatch: pytest.MonkeyPatch, denied_header: str
) -> None:
    """Every header in the denylist is stripped, using its canonical casing."""
    _stub_resolver(monkeypatch, ["93.184.216.34"])
    captured = _install_fake_http(monkeypatch, status=200, headers={}, body=b"")
    tool = _build_tool()
    canonical = denied_header.title()
    _call(
        tool,
        url="https://example.com/",
        headers={canonical: "value", "X-Custom": "keep-me"},
    )
    sent_headers = captured["headers"]
    assert "X-Custom" in sent_headers
    assert canonical not in sent_headers
    assert not any(key.lower() == denied_header for key in sent_headers)


@pytest.mark.parametrize("denied_header", sorted(wr._REQUEST_HEADER_DENYLIST))
def test_request_header_denylist_strips_mixed_case(
    monkeypatch: pytest.MonkeyPatch, denied_header: str
) -> None:
    """The denylist match is case-insensitive — mixed-case header names are stripped too."""
    _stub_resolver(monkeypatch, ["93.184.216.34"])
    captured = _install_fake_http(monkeypatch, status=200, headers={}, body=b"")
    tool = _build_tool()
    mixed = _mixed_case(denied_header)
    _call(
        tool,
        url="https://example.com/",
        headers={mixed: "value", "X-Custom": "keep-me"},
    )
    sent_headers = captured["headers"]
    assert "X-Custom" in sent_headers
    assert not any(key.lower() == denied_header for key in sent_headers)


@pytest.mark.parametrize("redacted_header", sorted(wr._RESPONSE_HEADER_REDACT))
def test_response_header_redaction_strips_full_set(
    monkeypatch: pytest.MonkeyPatch, redacted_header: str
) -> None:
    """Every header in the redaction set is stripped, using its canonical casing."""
    _stub_resolver(monkeypatch, ["93.184.216.34"])
    canonical = redacted_header.title()
    _install_fake_http(
        monkeypatch,
        status=200,
        headers={canonical: "secret-value", "X-Custom": "keep-me"},
        body=b"",
    )
    tool = _build_tool()
    result = _call(tool, url="https://example.com/")
    assert result["response_headers"] == {"X-Custom": "keep-me"}


@pytest.mark.parametrize("redacted_header", sorted(wr._RESPONSE_HEADER_REDACT))
def test_response_header_redaction_strips_mixed_case(
    monkeypatch: pytest.MonkeyPatch, redacted_header: str
) -> None:
    """The redaction match is case-insensitive -- mixed-case header names are stripped too."""
    _stub_resolver(monkeypatch, ["93.184.216.34"])
    mixed = _mixed_case(redacted_header)
    _install_fake_http(
        monkeypatch,
        status=200,
        headers={mixed: "secret-value", "X-Custom": "keep-me"},
        body=b"",
    )
    tool = _build_tool()
    result = _call(tool, url="https://example.com/")
    assert result["response_headers"] == {"X-Custom": "keep-me"}


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def test_telemetry_success_emits_span_and_counter_without_leaking_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_resolver(monkeypatch, ["93.184.216.34"])
    _install_fake_http(monkeypatch, status=200, headers={}, body=b"ok")
    spans = _install_span_capture(monkeypatch)
    counters = _install_counter_capture(monkeypatch)

    tool = _build_tool()
    _call(tool, url="https://example.com/path?apikey=super-secret")

    assert counters == [False]
    assert len(spans) == 1
    span = spans[0]
    assert span.errors == []
    # No attribute value may contain the query string or its secret value.
    for value in span.attributes.values():
        assert "super-secret" not in str(value)
        assert "?" not in str(value)


def test_telemetry_ssrf_block_records_error_and_span_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spans = _install_span_capture(monkeypatch)
    counters = _install_counter_capture(monkeypatch)

    tool = _build_tool()
    _call(tool, url="https://user:pass@example.com/")

    assert counters == [True]
    assert len(spans) == 1
    assert spans[0].errors  # at least one set_error call


def test_telemetry_timeout_records_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_resolver(monkeypatch, ["93.184.216.34"])
    _install_fake_http(monkeypatch, raise_exc=TimeoutError("timed out"))
    counters = _install_counter_capture(monkeypatch)

    tool = _build_tool()
    result = _call(tool, url="https://example.com/")

    assert "error" in result
    assert counters == [True]


# ---------------------------------------------------------------------------
# Config clamping
# ---------------------------------------------------------------------------


def test_clamp_config_uses_defaults_when_unset() -> None:
    timeout, max_response, max_request = wr._clamp_config(WebRequestConfig())
    assert timeout == wr._DEFAULT_TIMEOUT_SECONDS
    assert max_response == wr._DEFAULT_MAX_RESPONSE_BYTES
    assert max_request == wr._DEFAULT_MAX_REQUEST_BYTES


def test_clamp_config_clamps_to_absolute_ceilings() -> None:
    timeout, max_response, max_request = wr._clamp_config(
        WebRequestConfig(
            timeout_seconds=99999,
            max_response_bytes=999_999_999,
            max_request_bytes=999_999_999,
        )
    )
    assert timeout == wr._MAX_TIMEOUT_SECONDS
    assert max_response == wr._MAX_RESPONSE_BYTES
    assert max_request == wr._MAX_REQUEST_BYTES


def test_request_body_too_large_is_rejected_without_network_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_resolver(monkeypatch, ["93.184.216.34"])
    captured = _install_fake_http(monkeypatch, status=200, headers={}, body=b"")
    tool = _build_tool(WebRequestConfig(max_request_bytes=5))
    result = _call(tool, url="https://example.com/", method="POST", body="this is too long")
    assert "error" in result
    assert "connector" not in captured


# ---------------------------------------------------------------------------
# Default-on registration regression
# ---------------------------------------------------------------------------


def test_create_web_request_tools_returns_one_tool_named_web_request() -> None:
    tools = wr.create_web_request_tools(WebRequestConfig())
    assert len(tools) == 1
    assert tools[0].name == "web_request"


def test_redirect_is_returned_as_is_with_redirect_count_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_resolver(monkeypatch, ["93.184.216.34"])
    _install_fake_http(
        monkeypatch,
        status=302,
        headers={"Location": "https://example.com/new"},
        body=b"",
    )
    tool = _build_tool()
    result = _call(tool, url="https://example.com/old")
    assert result["status"] == 302
    assert result["redirect_count"] == 0
