from __future__ import annotations

from azurefunctions.extensions.http.fastapi import Request

from azure_functions_agents.experimental.durable_chat_origin import (
    durable_chat_cors_preflight,
    durable_chat_mutation_origin,
    durable_chat_preflight_headers,
    durable_chat_request_origin,
    parse_durable_chat_allowed_origins,
)


def _request(
    headers: dict[str, str],
    *,
    scheme: str = "https",
) -> Request:
    return Request(
        {
            "type": "http",
            "scheme": scheme,
            "path": "/",
            "headers": [
                (name.casefold().encode("ascii"), value.encode("ascii"))
                for name, value in headers.items()
            ],
        }
    )


def test_origin_policy_matches_only_canonical_exact_configured_origins() -> None:
    policy = parse_durable_chat_allowed_origins(
        '["https://frontend.example.test", "http://localhost:7091"]'
    )

    assert policy.allowed_origin("https://FRONTEND.example.test:443") == (
        "https://frontend.example.test"
    )
    assert policy.allowed_origin("http://localhost:7091") == "http://localhost:7091"
    assert policy.allowed_origin("https://frontend.example.test.evil.test") is None
    assert policy.allowed_origin("https://frontend.example.test/path") is None
    assert policy.allowed_origin("null") is None


def test_preflight_allows_only_route_methods_and_known_headers() -> None:
    policy = parse_durable_chat_allowed_origins('["https://frontend.example.test"]')
    allowed_methods = frozenset({"GET", "POST"})
    request = _request(
        {
            "Origin": "https://frontend.example.test",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": (
                "Content-Type, Idempotency-Key, Last-Event-ID, Accept"
            ),
        }
    )

    preflight = durable_chat_cors_preflight(request, policy, allowed_methods)

    assert preflight is not None
    assert durable_chat_preflight_headers(preflight, allowed_methods) == {
        "Access-Control-Allow-Origin": "https://frontend.example.test",
        "Access-Control-Allow-Methods": "GET, POST",
        "Access-Control-Allow-Headers": (
            "Content-Type, Idempotency-Key, Last-Event-ID, Accept"
        ),
        "Vary": "Origin, Access-Control-Request-Method, "
        "Access-Control-Request-Headers",
    }
    assert (
        durable_chat_cors_preflight(
            _request(
                {
                    "Origin": "https://frontend.example.test",
                    "Access-Control-Request-Method": "DELETE",
                }
            ),
            policy,
            allowed_methods,
        )
        is None
    )
    assert (
        durable_chat_cors_preflight(
            _request(
                {
                    "Origin": "https://frontend.example.test",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Authorization",
                }
            ),
            policy,
            allowed_methods,
        )
        is None
    )
    assert (
        durable_chat_cors_preflight(
            _request(
                {
                    "Origin": "https://attacker.example.test",
                    "Access-Control-Request-Method": "POST",
                }
            ),
            policy,
            allowed_methods,
        )
        is None
    )


def test_actual_mutation_guard_retains_same_origin_and_allows_only_policy_origins() -> None:
    policy = parse_durable_chat_allowed_origins('["https://frontend.example.test"]')

    configured = durable_chat_mutation_origin(
        _request({"Origin": "https://frontend.example.test"}),
        policy,
    )
    same_origin = durable_chat_mutation_origin(
        _request(
            {
                "Host": "backend.example.test",
                "Origin": "https://backend.example.test",
                "X-Forwarded-Proto": "https",
            }
        ),
        policy,
    )
    denied = durable_chat_mutation_origin(
        _request(
            {
                "Host": "backend.example.test",
                "Origin": "https://attacker.example.test",
                "X-Forwarded-Proto": "https",
            }
        ),
        policy,
    )
    nonbrowser = durable_chat_request_origin(_request({}), policy)

    assert configured.permitted
    assert configured.cors_origin == "https://frontend.example.test"
    assert same_origin.permitted
    assert same_origin.cors_origin is None
    assert not denied.permitted
    assert nonbrowser.permitted
