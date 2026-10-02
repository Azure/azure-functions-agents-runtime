"""Offline Blob service errors raised through the pinned SDK's real response pipeline."""

from __future__ import annotations

import pytest
from azure.core.exceptions import HttpResponseError
from azure.core.pipeline.transport import AsyncHttpTransport
from azure.core.rest._http_response_impl_async import AsyncHttpResponseImpl
from azure.storage.blob.aio import BlobClient


class ScriptedBlobTransport(AsyncHttpTransport):
    """Answer requests in order with scripted Blob service responses, without network access."""

    def __init__(self, *responses: tuple[int, str | None]):
        self.responses, self.requests = list(responses), []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def open(self):
        pass

    async def close(self):
        pass

    async def send(self, request, **_kwargs):
        self.requests.append(request)
        status, code = self.responses.pop(0)
        headers = {"x-ms-error-code": code, "Content-Type": "application/xml"} if code else {}
        response = AsyncHttpResponseImpl(
            request=request, internal_response=None, status_code=status, headers=headers,
            reason="scripted", content_type=headers.get("Content-Type"),
            stream_download_generator=None,
        )
        response._content = (
            f"<?xml version='1.0' encoding='utf-8'?><Error><Code>{code}</Code>"
            "<Message>service error</Message></Error>"
        ).encode() if code else b""
        response._is_closed = response._is_stream_consumed = True
        return response


async def sdk_error(status, code, operation):
    """Return the exception the pinned SDK raises when the service answers ``operation``."""
    transport = ScriptedBlobTransport((status, code))
    async with BlobClient(
        "https://acct.blob.core.windows.net", "c", "b", transport=transport, retry_total=0
    ) as client:
        with pytest.raises(HttpResponseError) as caught:
            await operation(client)
    return caught.value, transport.requests[0]
