"""Bodyless recovery overlays for explicitly opted-in synthetic HTTP inputs."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, cast
from urllib.parse import urlencode, urlsplit, urlunsplit

from azure.functions import HttpRequest, HttpResponse

from ._hosted_recovery_input import HttpRetryArguments

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

type _Scalar = str | int | float | bool | None

_BODY_TIMEOUT = 1.0
_SENSITIVE_KEYS = frozenset({"auth", "code", "key", "pwd", "sessionid", "sig"})
_SENSITIVE_PARTS = (
    "token", "password", "passwd", "secret", "authorization", "apikey",
    "accesskey", "privatekey", "credential", "cookie", "signature",
    "subscriptionkey", "functionkey", "functionskey",
)


def _sensitive(key: str) -> bool:
    normalized = "".join(character for character in key.casefold() if character.isalnum())
    return normalized in _SENSITIVE_KEYS or any(part in normalized for part in _SENSITIVE_PARTS)


def _validate_key(key: str) -> None:
    if not isinstance(key, str) or _sensitive(key):
        raise ValueError("Recovery cannot patch credential-like input keys")


def _scalar(value: object) -> bool:
    return value is None or type(value) in (str, int, bool) or (
        isinstance(value, float) and math.isfinite(value)
    )


def _payload(remediation: str) -> dict[str, object]:
    return {
        "error": "FUNCTION_RECOVERY_FAILED",
        "requires_user_action": True,
        "remediation": remediation,
    }


class _RequestAdapter(Protocol):
    @property
    def request(self) -> object: ...

    @property
    def method(self) -> str: ...

    @property
    def route_params(self) -> Mapping[str, object]: ...

    @property
    def query_params(self) -> list[tuple[str, str]]: ...

    def validate_body(self) -> None: ...

    def corrected(
        self, route_params: dict[str, object], query_params: list[tuple[str, str]]
    ) -> object: ...

    def failure(self, remediation: str) -> object: ...


@dataclass(frozen=True)
class _AzureRequest:
    request: HttpRequest = field(repr=False)

    @property
    def method(self) -> str:
        method: str = self.request.method
        return method

    @property
    def route_params(self) -> Mapping[str, object]:
        params: Mapping[str, object] = self.request.route_params
        return params

    @property
    def query_params(self) -> list[tuple[str, str]]:
        return list(self.request.params.items())

    def validate_body(self) -> None:
        if self.request.get_body():
            raise ValueError("Recovery does not support request bodies")

    def corrected(
        self, route_params: dict[str, object], query_params: list[tuple[str, str]]
    ) -> HttpRequest:
        if len({key for key, _ in query_params}) != len(query_params):
            raise ValueError("Azure query patches require unique keys")
        params = dict(self.request.params)
        params.update(query_params)
        url = self.request.url
        if query_params:
            url = urlunsplit(urlsplit(url)._replace(query=urlencode(params)))
        return HttpRequest(
            method=self.method,
            url=url,
            headers=dict(self.request.headers),
            params=params,
            # SDK storage preserves scalars despite its string-only route annotation.
            route_params=cast(Mapping[str, str], route_params),
            body=b"",
        )

    def failure(self, remediation: str) -> HttpResponse:
        return HttpResponse(
            body=b"" if self.method == "HEAD" else json.dumps(_payload(remediation)),
            status_code=500,
            mimetype="application/json",
        )


@dataclass(frozen=True)
class _StarletteRequest:
    request: Request = field(repr=False)

    @property
    def method(self) -> str:
        return self.request.method

    @property
    def route_params(self) -> Mapping[str, object]:
        params: Mapping[str, object] = self.request.scope.get("path_params", {})
        return params

    @property
    def query_params(self) -> list[tuple[str, str]]:
        # Reading the scope avoids populating caches on the original Request.
        from starlette.datastructures import QueryParams

        return QueryParams(self.request.scope.get("query_string", b"")).multi_items()

    def validate_body(self) -> None:
        from starlette.datastructures import Headers

        headers = Headers(raw=self.request.scope["headers"])
        # Trust explicit host framing; never consume or replay an ASGI body channel.
        if headers.getlist("content-length") != ["0"] or "transfer-encoding" in headers:
            raise ValueError("Recovery requires Content-Length: 0 and no Transfer-Encoding framing")

    async def prepare(self) -> _StarletteRequest:
        from starlette.datastructures import Headers
        from starlette.requests import Request
        from starlette.types import Message

        headers = Headers(raw=self.request.scope["headers"])
        if "content-length" in headers or "transfer-encoding" in headers:
            self.validate_body()
            return self
        try:
            async with asyncio.timeout(_BODY_TIMEOUT):
                message = await self.request.receive()
        except TimeoutError:
            raise ValueError("Recovery requires a verified bodyless request") from None
        if (
            message["type"] != "http.request"
            or message.get("body", b"") != b""
            or message.get("more_body", False)
        ):
            raise ValueError("Recovery requires a verified bodyless request")

        async def empty_receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        # Only an empty terminal event was consumed; never replay original body data.
        return _StarletteRequest(Request(dict(self.request.scope), receive=empty_receive))

    def corrected(
        self, route_params: dict[str, object], query_params: list[tuple[str, str]]
    ) -> Request:
        from starlette.requests import Request

        scope = dict(self.request.scope)
        scope["path_params"] = route_params
        scope["headers"] = list(scope["headers"])
        if query_params:
            patched = {key for key, _ in query_params}
            pairs = [(key, value) for key, value in self.query_params if key not in patched]
            scope["query_string"] = urlencode(pairs + query_params).encode("ascii")
        # Preserve metadata identities, but do not share or replay receive/send channels.
        return Request(scope)

    def failure(self, remediation: str) -> Response:
        from starlette.responses import JSONResponse, Response

        if self.method == "HEAD":
            return Response(status_code=500, media_type="application/json")
        return JSONResponse(_payload(remediation), status_code=500)


def _adapter(request: object) -> _RequestAdapter:
    if isinstance(request, HttpRequest):
        return _AzureRequest(request)
    try:
        from starlette.requests import Request
    except ImportError:
        raise TypeError("Unsupported recovery request type") from None
    if isinstance(request, Request):
        return _StarletteRequest(request)
    raise TypeError("Unsupported recovery request type")


@dataclass(frozen=True)
class RecoveryRequest:
    """Clone GET/HEAD inputs; credential-key filtering is not value redaction."""

    _adapter: _RequestAdapter = field(repr=False)

    @property
    def retry_arguments(self) -> type[HttpRetryArguments]:
        return HttpRetryArguments

    @property
    def retry_description(self) -> str:
        return "Apply route/query corrections to the original request; repeat all corrections still needed."

    def corrected_arguments(self, arguments: HttpRetryArguments) -> object:
        return self.corrected(arguments.route_params or {}, arguments.query_params or [])

    @classmethod
    def create(cls, request: object) -> RecoveryRequest:
        """Reject unsupported methods, buffered bodies, and ambiguous ASGI framing."""
        adapter = _adapter(request)
        if adapter.method not in {"GET", "HEAD"}:
            raise ValueError("Recovery supports only GET and HEAD requests")
        adapter.validate_body()
        return cls(adapter)

    @classmethod
    async def prepare(cls, request: object) -> RecoveryRequest:
        """Verify headerless ASGI inputs with one bounded receive, without buffering bodies."""
        adapter = _adapter(request)
        if adapter.method not in {"GET", "HEAD"}:
            raise ValueError("Recovery supports only GET and HEAD requests")
        if isinstance(adapter, _StarletteRequest):
            adapter = await adapter.prepare()
        else:
            adapter.validate_body()
        return cls(adapter)

    @property
    def request(self) -> object:
        """Return the input for the initial handler call, possibly an empty-body ASGI clone."""
        return self._adapter.request

    def diagnostic(self) -> dict[str, object]:
        """Return fresh input projections without URLs, headers, or bodies."""
        return {
            "method": self._adapter.method,
            "route_params": {
                key: value for key, value in self._adapter.route_params.items()
                if not _sensitive(key) and _scalar(value)
            },
            "query_params": [
                (key, value) for key, value in self._adapter.query_params if not _sensitive(key)
            ],
        }

    def corrected(
        self, route_params: dict[str, _Scalar], query_params: list[tuple[str, str]]
    ) -> object:
        """Overlay existing finite scalar routes and merge textual query pairs by key."""
        routes = dict(self._adapter.route_params)
        for key, value in route_params.items():
            _validate_key(key)
            if key not in routes:
                raise ValueError("Recovery route patches must name existing route keys")
            if not _scalar(value):
                raise ValueError("Recovery route values must be finite JSON scalars")
            routes[key] = value
        for key, value in query_params:
            _validate_key(key)
            if not isinstance(value, str):
                raise ValueError("Recovery query values must be strings")
        return self._adapter.corrected(routes, query_params)

    def failure(self, remediation: str) -> object:
        """Return a native HTTP 500 response, omitting the body for HEAD."""
        return self._adapter.failure(remediation)