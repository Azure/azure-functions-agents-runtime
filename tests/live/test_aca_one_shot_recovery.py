"""Opt-in one-shot recovery coverage against a deployed ACA-backed endpoint."""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

import pytest
import pytest_asyncio
from tests.aca_smoke_diagnostics import AcaSmokeEnvironmentError
from tests.live.aca_smoke_support import aca_smoke_run_id

_HTTP_TIMEOUT_SECONDS = 60.0
_RECOVERY_TIMEOUT_SECONDS = 180.0

if os.environ.get("AZURE_FUNCTIONS_AGENTS_RUN_ACA_SMOKE") != "1":
    pytest.skip(
        "Set AZURE_FUNCTIONS_AGENTS_RUN_ACA_SMOKE=1 after human authorization to run live ACA.",
        allow_module_level=True,
    )

if os.environ.get("AZURE_FUNCTIONS_AGENTS_RUN_ACA_ENDPOINT_SMOKE") != "1":
    pytest.skip(
        "Set AZURE_FUNCTIONS_AGENTS_RUN_ACA_ENDPOINT_SMOKE=1 for deployed endpoint coverage.",
        allow_module_level=True,
    )


@dataclass(frozen=True, slots=True)
class _EndpointConfig:
    chat_url: str
    function_key: str


@dataclass(frozen=True, slots=True)
class _HttpResponse:
    status_code: int
    body: object
    headers: Mapping[str, str]


def _required_environment_value(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise AcaSmokeEnvironmentError(f"{name} must be set to a non-blank value.")
    return value


def _request(
    config: _EndpointConfig,
    url: str,
    *,
    method: str,
    body: object | None = None,
    headers: Mapping[str, str] | None = None,
) -> _HttpResponse:
    request_headers = {
        "Accept": "application/json",
        "x-functions-key": config.function_key,
        **(headers or {}),
    }
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=payload,
        headers=request_headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            status_code = response.status
            response_headers = dict(response.headers.items())
            response_body = response.read()
    except urllib.error.HTTPError as error:
        status_code = error.code
        response_headers = dict(error.headers.items())
        response_body = error.read()
    except (OSError, urllib.error.URLError) as error:
        raise AcaSmokeEnvironmentError(
            f"deployed ACA endpoint request failed before receiving HTTP: {type(error).__name__}"
        ) from error
    try:
        parsed_body: object = json.loads(response_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed_body = response_body.decode("utf-8", errors="replace")
    return _HttpResponse(
        status_code=status_code,
        body=parsed_body,
        headers=response_headers,
    )


def _open_events(config: _EndpointConfig, url: str) -> int:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/event-stream",
            "x-functions-key": config.function_key,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            response.read(1)
            return response.status
    except urllib.error.HTTPError as error:
        error.read()
        return error.code
    except (OSError, urllib.error.URLError) as error:
        raise AcaSmokeEnvironmentError(
            f"deployed ACA event stream failed before receiving HTTP: {type(error).__name__}"
        ) from error


def _management_url(config: _EndpointConfig, value: object) -> str:
    assert isinstance(value, str) and value
    return urllib.parse.urljoin(config.chat_url, value)


async def _wait_for_status(
    config: _EndpointConfig,
    status_url: str,
    *,
    terminal: bool,
) -> _HttpResponse:
    deadline = time.monotonic() + _RECOVERY_TIMEOUT_SECONDS
    latest: _HttpResponse | None = None
    while time.monotonic() < deadline:
        latest = await asyncio.to_thread(
            _request,
            config,
            status_url,
            method="GET",
        )
        if (
            latest.status_code == 200
            and isinstance(latest.body, dict)
            and (not terminal or latest.body.get("phase") == "terminal")
        ):
            return latest
        await asyncio.sleep(2)
    raise AssertionError(f"run status did not settle before timeout: {latest!r}")


@pytest.fixture
def aca_endpoint_config() -> _EndpointConfig:
    chat_url = _required_environment_value(
        "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_CHAT_URL"
    )
    parsed = urllib.parse.urlparse(chat_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AcaSmokeEnvironmentError(
            "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_CHAT_URL must be an absolute HTTP(S) URL."
        )
    return _EndpointConfig(
        chat_url=chat_url,
        function_key=_required_environment_value(
            "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_FUNCTION_KEY"
        ),
    )


@pytest_asyncio.fixture
async def one_shot_admission(
    aca_endpoint_config: _EndpointConfig,
) -> _HttpResponse:
    response = await asyncio.to_thread(
        _request,
        aca_endpoint_config,
        aca_endpoint_config.chat_url,
        method="POST",
        body={
            "prompt": (
                "one-shot ACA setup recovery "
                f"{aca_smoke_run_id()}-{uuid.uuid4().hex}"
            )
        },
        headers={
            "Idempotency-Key": f"aca-one-shot-{uuid.uuid4().hex}",
            "Prefer": "respond-async",
        },
    )
    if response.status_code in {401, 403, 404, 429, 503}:
        raise AcaSmokeEnvironmentError(
            f"deployed ACA endpoint is not ready for the smoke: HTTP {response.status_code}"
        )
    return response


@pytest.mark.live_aca
@pytest.mark.asyncio
async def test_live_aca_first_response_is_a_complete_recovery_handle(
    aca_endpoint_config: _EndpointConfig,
    one_shot_admission: _HttpResponse,
) -> None:
    response = one_shot_admission
    assert response.status_code in {202, 504}
    assert isinstance(response.body, dict)
    body = response.body
    assert isinstance(body.get("session_id"), str)
    assert isinstance(body.get("run_id"), str)
    if response.status_code == 504:
        assert body.get("admission") in {"committed", "possibly_committed"}

    status_url = _management_url(aca_endpoint_config, body.get("status_url"))
    result_url = _management_url(aca_endpoint_config, body.get("result_url"))
    events_url = _management_url(aca_endpoint_config, body.get("events_url"))
    cancel_url = _management_url(aca_endpoint_config, body.get("cancel_url"))

    status = await _wait_for_status(
        aca_endpoint_config,
        status_url,
        terminal=False,
    )
    assert status.status_code == 200
    assert isinstance(status.body, dict)
    assert status.body.get("session_id") == body["session_id"]
    assert status.body.get("run_id") == body["run_id"]

    result = await asyncio.to_thread(
        _request,
        aca_endpoint_config,
        result_url,
        method="GET",
    )
    if status.body.get("phase") == "provisioning":
        assert result.status_code == 200
        assert isinstance(result.body, dict)
        assert result.body.get("status") == "accepted"
    else:
        assert result.status_code in {200, 410}

    assert await asyncio.to_thread(_open_events, aca_endpoint_config, events_url) == 200

    canceled = await asyncio.to_thread(
        _request,
        aca_endpoint_config,
        cancel_url,
        method="POST",
    )
    assert canceled.status_code == 200
    assert isinstance(canceled.body, dict)
    assert canceled.body.get("status") in {
        "canceled",
        "failed",
        "abandoned",
        "succeeded",
    }

    terminal = await _wait_for_status(
        aca_endpoint_config,
        status_url,
        terminal=True,
    )
    assert isinstance(terminal.body, dict)
    assert terminal.body.get("phase") == "terminal"
