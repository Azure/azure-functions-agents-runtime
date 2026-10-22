"""HTTP recovery adapter contracts for Azure and optional Starlette requests."""

from __future__ import annotations

import asyncio
import builtins
import importlib.util
import json
from urllib.parse import parse_qsl, urlsplit

import pytest
from azure.functions import HttpRequest, HttpResponse
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from azure_functions_agents import _hosted_recovery_http
from azure_functions_agents._hosted_recovery_http import RecoveryRequest


def _azure(method="GET", *, body=b"", routes=None, params=None):
    return HttpRequest(
        method=method,
        url="https://example.test:8443/api/item/old?x=old&keep=yes#fragment",
        headers={"Authorization": "private", "X-Custom": "keep", "Content-Length": "0"},
        params=params if params is not None else {"x": "old", "keep": "yes"},
        route_params=routes if routes is not None else {"item": "old", "other": "keep"},
        body=body,
    )


async def _unread_receive():
    pytest.fail("Recovery must not consume the original ASGI receive channel")


def _starlette(method="GET", *, headers=None, routes=None, query=None):
    return Request(
        {
            "type": "http", "http_version": "1.1", "scheme": "https", "method": method,
            "path": "/api/item/old", "raw_path": b"/api/item/old", "root_path": "/mount",
            "query_string": query if query is not None else b"x=old&keep=a&keep=b&blank=",
            "headers": headers if headers is not None else [
                (b"host", b"example.test:8443"), (b"content-length", b"0"),
                (b"authorization", b"private"), (b"x-custom", b"keep"),
            ],
            "path_params": routes if routes is not None else {"item": "old", "other": "keep"},
            "client": ("127.0.0.1", 1234), "server": ("example.test", 8443),
            "state": {"trace": "abc"}, "session": {"name": "synthetic"},
            "extensions": {"http.response.debug": {}},
            "app": object(), "router": object(), "endpoint": object(),
            "user": object(), "auth": object(),
        },
        receive=_unread_receive,
    )


@pytest.fixture(params=[_azure, _starlette], ids=["azure", "starlette"])
def request_factory(request):
    return request.param


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("value", [7, 2.5, True, False, None, "new"])
def test_scalar_route_overlays_preserve_original_and_metadata(request_factory, method, value):
    original = request_factory(method)
    corrected = RecoveryRequest.create(original).corrected({"item": value}, [])
    assert corrected is not original
    assert corrected.method == original.method
    assert corrected.url == original.url
    assert corrected.headers == original.headers
    if isinstance(original, HttpRequest):
        assert isinstance(corrected, HttpRequest)
        routes = corrected.route_params
        assert original.route_params == {"item": "old", "other": "keep"}
        assert corrected.params == original.params
        assert corrected.get_body() == original.get_body() == b""
    else:
        assert isinstance(corrected, Request)
        routes = corrected.path_params
        assert original.path_params == {"item": "old", "other": "keep"}
        assert corrected.query_params == original.query_params
    assert routes == {"item": value, "other": "keep"}
    assert type(routes["item"]) is type(value)


def test_azure_query_mapping_and_url_agree():
    original = _azure()
    corrected = RecoveryRequest.create(original).corrected(
        {"item": 4}, [("x", "space + & ü"), ("added", "")]
    )
    assert corrected.params == {"x": "space + & ü", "keep": "yes", "added": ""}
    assert dict(parse_qsl(urlsplit(corrected.url).query, keep_blank_values=True)) == corrected.params
    assert urlsplit(corrected.url)._replace(query="") == urlsplit(original.url)._replace(query="")
    assert original.params == {"x": "old", "keep": "yes"}
    assert original.url.endswith("?x=old&keep=yes#fragment")


def test_starlette_query_duplicates_metadata_and_original_caches():
    original = _starlette()
    before = dict(original.scope)
    attributes = dict(vars(original))
    recovery = RecoveryRequest.create(original)
    recovery.diagnostic()
    corrected = recovery.corrected({"item": 4}, [("x", "one + ü"), ("x", "two&"), ("new", "")])
    assert original.scope == before
    assert vars(original) == attributes
    assert corrected.query_params.multi_items() == [
        ("keep", "a"), ("keep", "b"), ("blank", ""),
        ("x", "one + ü"), ("x", "two&"), ("new", ""),
    ]
    assert parse_qsl(corrected.url.query, keep_blank_values=True) == corrected.query_params.multi_items()
    for key, value in before.items():
        if key not in {"path_params", "query_string", "headers"}:
            assert corrected.scope[key] is value
    assert corrected.scope is not original.scope
    assert corrected.scope["headers"] is not original.scope["headers"]
    assert corrected.path_params is not original.path_params
    corrected.path_params["item"] = "mutated"
    corrected.scope["headers"].clear()
    assert original.path_params["item"] == "old"
    assert original.scope["headers"]


def test_starlette_empty_patch_preserves_exact_query_encoding():
    original = _starlette(query=b"x=a%20b&keep=a+%2b&keep=b&blank=")
    corrected = RecoveryRequest.create(original).corrected({}, [])
    assert corrected.scope["query_string"] == original.scope["query_string"]


def test_starlette_missing_routes_do_not_mutate_original_scope():
    original = _starlette()
    del original.scope["path_params"]
    recovery = RecoveryRequest.create(original)
    assert recovery.diagnostic()["route_params"] == {}
    assert recovery.corrected({}, []).path_params == {}
    assert "path_params" not in original.scope


def test_repeated_clones_and_diagnostics_are_independent(request_factory):
    recovery = RecoveryRequest.create(request_factory())
    first = recovery.corrected({"item": 1}, [])
    second = recovery.corrected({}, [])
    assert first is not second
    assert RecoveryRequest.create(first).diagnostic()["route_params"]["item"] == 1
    diagnostic = recovery.diagnostic()
    diagnostic["route_params"]["item"] = "mutated"
    diagnostic["query_params"].clear()
    assert recovery.diagnostic()["route_params"]["item"] == "old"
    assert RecoveryRequest.create(second).diagnostic() == recovery.diagnostic()
    assert set(diagnostic) == {"method", "route_params", "query_params"}
    assert "private" not in json.dumps(diagnostic)


@pytest.mark.parametrize("key", [
    "auth", "code", "TOKEN", "access_token", "Password", "passwd", "client-secret",
    "Authorization", "api_key", "Api-Key", "x-functions-key", "credential", "cookie",
    "signature", "pwd", "key", "sig", "session_id", "subscription-key", "private_key",
])
def test_sensitive_keys_are_filtered_unpatchable_and_preserved(request_factory, key):
    routes = {"item": "old", key: "synthetic-route"}
    if request_factory is _azure:
        original = request_factory(routes=routes, params={"x": "safe", key: "synthetic-query"})
    else:
        original = request_factory(routes=routes, query=f"x=safe&{key}=synthetic-query".encode())
    recovery = RecoveryRequest.create(original)
    assert recovery.diagnostic() == {
        "method": "GET", "route_params": {"item": "old"}, "query_params": [("x", "safe")],
    }
    with pytest.raises(ValueError, match="credential-like"):
        recovery.corrected({key: "replacement"}, [])
    with pytest.raises(ValueError, match="credential-like"):
        recovery.corrected({}, [(key, "replacement")])
    corrected = recovery.corrected({"item": 1}, [])
    if isinstance(original, HttpRequest):
        assert corrected.route_params[key] == "synthetic-route"
        assert corrected.params[key] == "synthetic-query"
    else:
        assert corrected.path_params[key] == "synthetic-route"
        assert corrected.query_params[key] == "synthetic-query"


@pytest.mark.parametrize("key", ["missing", "path", "url", "method", "headers", "identity"])
def test_new_route_keys_are_rejected(request_factory, key):
    with pytest.raises(ValueError, match="existing route keys"):
        RecoveryRequest.create(request_factory()).corrected({key: "/elsewhere"}, [])


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), [], {}, b"1"])
def test_invalid_route_values_are_rejected(request_factory, value):
    with pytest.raises(ValueError, match="finite JSON scalars"):
        RecoveryRequest.create(request_factory()).corrected({"item": value}, [])


@pytest.mark.parametrize("value", [1, True, None, {}, []])
def test_query_values_must_be_strings(request_factory, value):
    with pytest.raises(ValueError, match="strings"):
        RecoveryRequest.create(request_factory()).corrected({}, [("x", value)])


@pytest.mark.parametrize("key", [1, None])
def test_patch_keys_must_be_strings(request_factory, key):
    recovery = RecoveryRequest.create(request_factory())
    with pytest.raises(ValueError, match="credential-like"):
        recovery.corrected({key: "new"}, [])
    with pytest.raises(ValueError, match="credential-like"):
        recovery.corrected({}, [(key, "new")])


def test_diagnostics_omit_non_scalar_route_values(request_factory):
    request = request_factory(routes={"item": "old", "nested": {}, "invalid": float("nan")})
    assert RecoveryRequest.create(request).diagnostic()["route_params"] == {"item": "old"}


def test_azure_duplicate_query_patch_is_rejected_without_mutation():
    original = _azure()
    with pytest.raises(ValueError, match="unique keys"):
        RecoveryRequest.create(original).corrected({}, [("x", "one"), ("x", "two")])
    assert original.params["x"] == "old"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"])
def test_unsupported_methods_are_rejected(request_factory, method):
    with pytest.raises(ValueError, match="GET and HEAD"):
        RecoveryRequest.create(request_factory(method))


def test_azure_buffered_body_is_rejected_despite_zero_length_header():
    with pytest.raises(ValueError, match="bodies"):
        RecoveryRequest.create(_azure(body=b"not empty"))


@pytest.mark.parametrize("headers", [
    [], [(b"content-length", b"1")], [(b"content-length", b"-1")],
    [(b"content-length", b"invalid")], [(b"transfer-encoding", b"chunked")],
    [(b"content-length", b"0"), (b"transfer-encoding", b"chunked")],
    [(b"content-length", b"0"), (b"transfer-encoding", b"")],
    [(b"content-length", b"0"), (b"content-length", b"0")],
])
def test_starlette_unsafe_framing_is_rejected_without_consuming_stream(headers):
    original = _starlette(headers=headers)
    attributes = dict(vars(original))
    with pytest.raises(ValueError, match="Content-Length: 0"):
        RecoveryRequest.create(original)
    assert vars(original) == attributes


@pytest.mark.asyncio
async def test_starlette_clone_does_not_replay_original_channel():
    recovery = RecoveryRequest.create(_starlette())
    recovery.diagnostic()
    corrected = recovery.corrected({}, [])
    with pytest.raises(RuntimeError, match="Receive channel"):
        await corrected.body()


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_native_failure_response(request_factory, method):
    response = RecoveryRequest.create(request_factory(method)).failure("Review synthetic input: ü")
    assert response.status_code == 500
    if request_factory is _azure:
        assert isinstance(response, HttpResponse)
        body = response.get_body()
        assert response.mimetype == "application/json"
    else:
        assert isinstance(response, Response)
        assert isinstance(response, JSONResponse) is (method == "GET")
        body = response.body
        assert response.media_type == "application/json"
    if method == "HEAD":
        assert body == b""
    else:
        assert json.loads(body) == {
            "error": "FUNCTION_RECOVERY_FAILED", "requires_user_action": True,
            "remediation": "Review synthetic input: ü",
        }


@pytest.mark.parametrize("candidate", [object(), {}, None, "request"])
def test_unsupported_request_types(candidate):
    with pytest.raises(TypeError, match="Unsupported recovery request"):
        RecoveryRequest.create(candidate)


def test_module_and_azure_adapter_work_without_starlette(monkeypatch):
    real_import = builtins.__import__

    def without_starlette(name, *args, **kwargs):
        if name.startswith("starlette"):
            raise ImportError("optional dependency unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_starlette)
    spec = importlib.util.spec_from_file_location(
        _hosted_recovery_http.__name__, _hosted_recovery_http.__file__
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    recovery = module.RecoveryRequest.create(_azure())
    recovery.diagnostic()
    assert recovery.corrected({"item": 3}, []).route_params["item"] == 3
    assert recovery.failure("Guidance").status_code == 500
    with pytest.raises(TypeError, match="Unsupported recovery request"):
        module.RecoveryRequest.create(object())


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_prepare_headerless_starlette_verifies_empty_body_without_mutation(method):
    scope = dict(_starlette(method, headers=[]).scope)
    calls = []

    async def receive():
        calls.append(True)
        assert len(calls) == 1
        return {"type": "http.request", "body": b"", "more_body": False}

    original = Request(scope, receive=receive)
    attributes, before = dict(vars(original)), dict(scope)
    recovery = await RecoveryRequest.prepare(original)
    prepared = recovery.request
    assert calls == [True]
    assert prepared is not original
    assert vars(original) == attributes
    assert original.scope == before
    assert prepared.scope is not original.scope
    for key, value in before.items():
        assert prepared.scope[key] is value
    assert await prepared.body() == b""
    assert await prepared.body() == b""
    assert await prepared.is_disconnected() is False
    assert calls == [True]
    assert recovery.corrected({"item": 3}, []).path_params["item"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    {"type": "http.request", "body": b"unexpected"},
    {"type": "http.request", "body": b"x" * 100_000, "more_body": True},
    {"type": "http.request", "body": b"", "more_body": True},
    {"type": "http.disconnect"},
])
async def test_prepare_rejects_nonempty_or_unfinished_stream_after_one_event(message):
    calls = []

    async def receive():
        calls.append(True)
        assert len(calls) == 1
        return message

    original = Request(dict(_starlette(headers=[]).scope), receive=receive)
    attributes = dict(vars(original))
    with pytest.raises(ValueError, match="bodyless"):
        await RecoveryRequest.prepare(original)
    assert calls == [True]
    assert vars(original) == attributes


@pytest.mark.asyncio
async def test_prepare_bounds_headerless_receive_wait(monkeypatch):
    monkeypatch.setattr(_hosted_recovery_http, "_BODY_TIMEOUT", 0.01)
    finished = asyncio.Event()

    async def receive():
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    original = Request(dict(_starlette(headers=[]).scope), receive=receive)
    with pytest.raises(ValueError, match="bodyless"):
        await RecoveryRequest.prepare(original)
    assert finished.is_set()


@pytest.mark.asyncio
async def test_prepare_propagates_external_cancellation():
    entered, finished = asyncio.Event(), asyncio.Event()

    async def receive():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    original = Request(dict(_starlette(headers=[]).scope), receive=receive)
    task = asyncio.create_task(RecoveryRequest.prepare(original))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [
    [(b"content-length", b"1")], [(b"content-length", b"invalid")],
    [(b"content-length", b"0"), (b"content-length", b"0")],
    [(b"transfer-encoding", b"chunked")],
    [(b"content-length", b"0"), (b"transfer-encoding", b"")],
])
async def test_prepare_rejects_unsafe_framing_without_receiving(headers):
    original = _starlette(headers=headers)
    with pytest.raises(ValueError, match="Content-Length: 0"):
        await RecoveryRequest.prepare(original)


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", [_azure, _starlette])
async def test_prepare_explicitly_bodyless_request_keeps_identity(factory):
    original = factory()
    assert (await RecoveryRequest.prepare(original)).request is original


@pytest.mark.asyncio
async def test_prepare_checks_method_before_consuming_headerless_stream():
    with pytest.raises(ValueError, match="GET and HEAD"):
        await RecoveryRequest.prepare(_starlette("POST", headers=[]))