"""Native Functions, Blob journal, and browser tests with a controlled MAF transport."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import BlobServiceClient

from azure_functions_agents.experimental.durable_chat_protocol import durable_chat_run_correlation
from azure_functions_agents.experimental.hybrid_config import HYBRID_SANDBOX_GROUP_ENV
from tests.doubles.durable_chat_cdp import chromium_page
from tests.doubles.durable_chat_test_sandbox import TEST_SANDBOX_GROUP
from tests.endtoend._func_host import HostHandle, running_host
from tests.endtoend._http_probe import HttpClient, discover_http_endpoints
from tests.endtoend._storage_probe import DEV_CONNECTION_STRING

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(shutil.which("func") is None, reason="Azure Functions Core Tools not found"),
]

_WORKSPACE = Path(__file__).resolve().parents[2]
_FIXTURE = _WORKSPACE / "tests" / "fixtures" / "durable_chat_e2e"
_PREFIX = "/chat-e2e"
_SELECTED_ID = "document.querySelector('#durable-chat-session-id').textContent.trim()"
_REQUEST_TEXT = "document.querySelector('#durable-chat-request-list').textContent"
_DETAILS_VISIBLE = "document.querySelector('#durable-chat-shell').dataset.detailsVisible"


def _local_storage_connection() -> str:
    connection = os.environ.get("DURABLE_CHAT_E2E_STORAGE_CONNECTION", DEV_CONNECTION_STRING)
    fields = dict(field.split("=", 1) for field in connection.split(";") if field)
    for key in ("BlobEndpoint", "QueueEndpoint", "TableEndpoint"):
        endpoint = urlsplit(fields.get(key, ""))
        if endpoint.scheme != "http" or endpoint.hostname not in {"127.0.0.1", "localhost"}:
            pytest.fail("Durable chat E2E only accepts explicit loopback Azurite endpoints")
        try:
            with socket.create_connection((endpoint.hostname, endpoint.port), timeout=1):
                pass
        except OSError:
            pytest.skip("Azurite is not running for native durable chat E2E")
    return connection


@pytest.fixture(scope="module")
def durable_chat_host(tmp_path_factory: pytest.TempPathFactory) -> Iterator[HostHandle]:
    connection = _local_storage_connection()
    app_dir = tmp_path_factory.mktemp("durable-chat-app")
    for source in _FIXTURE.iterdir():
        if source.is_file():
            shutil.copyfile(source, app_dir / source.name)
    suffix = uuid4().hex
    container = f"durable-chat-e2e-{suffix}"
    settings = {
        "FUNCTIONS_WORKER_RUNTIME": "python",
        "PYTHON_ENABLE_INIT_INDEXING": "1",
        "AzureWebJobsStorage": connection,
        "DURABLE_CHAT_E2E_STORAGE_CONNECTION": connection,
        "DURABLE_CHAT_E2E_CONTENT_CONTAINER": container,
        "DURABLE_CHAT_E2E_TASK_HUB": f"DurableChat{suffix}",
        "WEBSITE_OWNER_NAME": "00000000-0000-0000-0000-000000000001+durable-chat-e2e",
        "WEBSITE_SITE_NAME": f"durable-chat-e2e-{suffix}",
        "WEBSITE_SLOT_NAME": "Production",
        "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_ENABLED": "true",
        "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_RETAINED_SANDBOX_ENABLED": "true",
        # The injected transport owns its group; never enable a live reaper from ambient settings.
        HYBRID_SANDBOX_GROUP_ENV: "",
        "AZURE_FUNCTIONS_AGENTS_PROVIDER": "openai",
        "AZURE_FUNCTIONS_AGENTS_MODEL": "durable-chat-e2e",
        "APPLICATIONINSIGHTS_CONNECTION_STRING": "",
        "APPINSIGHTS_INSTRUMENTATIONKEY": "",
        "PYTHON_APPLICATIONINSIGHTS_ENABLE_TELEMETRY": "0",
        "PYTHON_ENABLE_OPENTELEMETRY": "0",
        "ENABLE_SENSITIVE_DATA": "false",
    }
    (app_dir / "local.settings.json").write_text(
        json.dumps({"IsEncrypted": False, "Values": settings}),
        encoding="utf-8",
    )
    environment = {
        **settings,
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
        "VIRTUAL_ENV": sys.prefix,
        "PYTHONPATH": os.pathsep.join((str(_WORKSPACE), str(_WORKSPACE / "src"))),
        "FUNCTIONS_CORE_TOOLS_TELEMETRY_OPTOUT": "1",
        "AzureFunctionsWebHost__hostid": f"durablechat{suffix[:20]}",
        "NO_PROXY": "localhost,127.0.0.1,::1",
    }
    try:
        with running_host(app_dir, timeout=150, env=environment) as host:
            yield host
    finally:
        with (
            BlobServiceClient.from_connection_string(connection) as service,
            contextlib.suppress(ResourceNotFoundError),
        ):
            service.delete_container(container)


async def _new_browser_session(page) -> str:
    previous = await page.evaluate(_SELECTED_ID)
    await page.evaluate("document.querySelector('#durable-chat-new-session').click()")
    return await page.wait_for(
        f"({_SELECTED_ID} !== '\u2014' && {_SELECTED_ID} !== {json.dumps(previous)}"
        f" && {_SELECTED_ID})"
    )


async def _wait_for_run_status(host: HostHandle, run_id: str, expected: str) -> None:
    with HttpClient(host.base_url, timeout=10) as client:
        async with asyncio.timeout(45):
            while True:
                response = await asyncio.to_thread(
                    client.get,
                    f"{_PREFIX}/experimental/durable-agent-runs/{run_id}",
                )
                response.raise_for_status()
                if response.json()["status"] == expected:
                    return
                await asyncio.sleep(0.2)


async def _submit_browser_request(page, *, nonce: str, marker: str = "") -> None:
    await page.wait_for(
        "!document.querySelector('#durable-chat-prompt').disabled",
        timeout=30,
    )
    await page.evaluate(
        "document.querySelector('#durable-chat-prompt').value="
        f"{json.dumps(f'E2E:{nonce} {marker} Stream a local answer.')};"
        "document.querySelector('#durable-chat-composer').requestSubmit()"
    )


async def _start_browser_request(page, *, nonce: str, marker: str = "") -> str:
    await _submit_browser_request(page, nonce=nonce, marker=marker)
    await page.wait_for(
        "Array.from(document.querySelectorAll('[data-request-card]')).some(card => "
        f"card.textContent.includes({json.dumps(f'E2E:{nonce}')})"
        " && card.textContent.includes('Durable answer \u03bb'))",
        timeout=45,
    )
    run_id = await page.evaluate("document.querySelector('#durable-chat-run-id').textContent.trim()")
    assert run_id and run_id != "\u2014"
    return run_id


async def _verify_mobile_and_capture(page) -> None:
    await page.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": False},
    )
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    screenshot_dir = os.environ.get("DURABLE_CHAT_E2E_SCREENSHOTS")
    if screenshot_dir:
        path = Path(screenshot_dir)
        path.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path / "durable-chat-mobile.png")
        await page.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1440, "height": 1000, "deviceScaleFactor": 1, "mobile": False},
        )
        await page.screenshot(path / "durable-chat-desktop.png")


def _assert_recorded_run_spans(client: HttpClient, run_id: str) -> None:
    response = client.get(f"{_PREFIX}/_test/spans/{run_id}")
    response.raise_for_status()
    spans = response.json()["spans"]
    assert spans
    assert all(
        span["run_correlation"] == durable_chat_run_correlation(run_id) for span in spans
    )
    assert all(len(span["trace_id"]) == 32 for span in spans)


def _read_pre_final_draft(response) -> int:
    text = ""
    cursor = 0
    deadline = time.monotonic() + 45
    for line in response.iter_lines(chunk_size=1, decode_unicode=True):
        if time.monotonic() >= deadline:
            pytest.fail("The native SSE route did not publish the held model draft")
        if not line.startswith("data: "):
            continue
        frame = json.loads(line[6:])
        if frame.get("event_type") == "snapshot":
            projection = frame["projection"]
            cursor = projection["through_sequence"]
            text = (projection.get("draft") or {}).get("text", "")
        else:
            assert frame["sequence"] > cursor
            cursor = frame["sequence"]
            assert frame["published_revision"] >= 1
            event = frame["event"]
            if event["event_type"] == "assistant_text":
                text += event["delta"]
        if text == "Durable answer \u03bb":
            return cursor
    pytest.fail("Native SSE ended before exposing genuine pre-final model text")


@pytest.mark.parametrize("marker", ["", "E2E-TOOLS"])
def test_native_api_streams_before_final_and_replays(
    durable_chat_host: HostHandle, marker: str
) -> None:
    host = durable_chat_host
    nonce, session_id, request_id = uuid4().hex, uuid4().hex, uuid4().hex
    body = {
        "prompt": f"E2E:{nonce} {marker} Stream a local answer.",
        "session_id": session_id,
        "request_id": request_id,
        "ui": {"schema_version": "1", "stream_response": True},
    }
    with HttpClient(host.base_url, timeout=45) as client:
        bootstrap = client.get(f"{_PREFIX}/experimental/durable-chat/config")
        if bootstrap.status_code != 200:
            endpoints = discover_http_endpoints(client)
            pytest.fail(
                f"Bootstrap returned {bootstrap.status_code}: {bootstrap.text}; "
                f"registered HTTP routes: {[(item.function_name, item.route) for item in endpoints]}"
            )
        assert bootstrap.headers["Cache-Control"] == "no-store"
        configuration = bootstrap.json()
        assert configuration["foreground_streaming_available"] is True
        routes = {route["name"]: route["path_template"] for route in configuration["routes"]}
        rejected = client.post(
            routes["start_run"],
            json=body,
            headers={
                "Origin": "https://forged.example",
                "X-Forwarded-Host": "forged.example",
                "X-Forwarded-Proto": "https",
            },
        )
        assert rejected.status_code == 403
        assert rejected.json() == {"error": "cross_origin_request_rejected"}
        started = client.post(
            routes["start_run"],
            json=body,
            headers={"Idempotency-Key": request_id, "Origin": host.base_url},
        )
        assert started.status_code == 202, f"{started.text}\n{host.read_output()[-18000:]}"
        run_id = started.json()["run_id"]
        events_path = routes["events"].format(run_id=run_id)
        with client.get(events_path, stream=True) as stream:
            assert stream.status_code == 200, stream.text
            assert "text/event-stream" in stream.headers["Content-Type"]
            cursor = _read_pre_final_draft(stream)
        before_final = client.get(routes["status"].format(run_id=run_id)).json()
        assert before_final["session_id"] == session_id
        assert before_final["status"] not in {"Completed", "Failed", "Cancelled"}
        assert client.post(f"{_PREFIX}/_test/release/{nonce}").status_code == 200
        asyncio.run(_wait_for_run_status(host, run_id, "Completed"))
        result = client.get(routes["result"].format(run_id=run_id))
        assert result.json() == {
            "response": f"Durable answer \u03bb for {nonce}.",
            "run_id": run_id,
            "session_id": session_id,
            "status": "Completed",
        }
        replay = client.get(events_path, headers={"Last-Event-ID": str(cursor)})
        assert replay.status_code == 200
        frames = [
            json.loads(line[6:])
            for line in replay.text.splitlines()
            if line.startswith("data: ")
        ]
        assert any(
            frame.get("event", {}).get("event_type") == "terminal" for frame in frames
        )
        assert all(frame["sequence"] > cursor for frame in frames if "sequence" in frame)
        repeated = client.post(
            routes["start_run"],
            json=body,
            headers={"Idempotency-Key": request_id},
        )
        assert repeated.status_code in {200, 202}
        assert repeated.json()["run_id"] == run_id
        _assert_recorded_run_spans(client, run_id)
        if marker == "E2E-TOOLS":
            _assert_native_tool_observations(client, host, nonce, run_id)


@pytest.mark.asyncio
async def test_native_chat_stream_history_reconnect_and_details(
    durable_chat_host: HostHandle,
    tmp_path: Path,
) -> None:
    host = durable_chat_host
    page_url = f"{host.base_url}{_PREFIX}/experimental/durable-chat/"
    nonce = uuid4().hex
    async with chromium_page(tmp_path / "browser") as page:
        await page.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1440, "height": 1000, "deviceScaleFactor": 1, "mobile": False},
        )
        await page.navigate(page_url)
        await page.wait_for(
            "document.querySelector('#durable-chat-connection-summary').textContent"
            ".toLowerCase().includes('ready')",
            timeout=30,
        )
        session_id = await _new_browser_session(page)
        run_id = await _start_browser_request(page, nonce=nonce)
        with HttpClient(host.base_url) as client:
            status = client.get(
                f"{_PREFIX}/experimental/durable-agent-runs/{run_id}"
            ).json()
            assert status["session_id"] == session_id
            assert status["status"] not in {"Completed", "Failed", "Cancelled"}

        width_before = await page.evaluate(
            "document.querySelector('.durable-chat-conversation').getBoundingClientRect().width"
        )
        await page.evaluate("document.querySelector('#durable-chat-details-toggle').click()")
        assert await page.evaluate(_DETAILS_VISIBLE) == "false"
        assert await page.evaluate(
            "document.querySelector('.durable-chat-conversation').getBoundingClientRect().width"
        ) > width_before

        other_session_id = await _new_browser_session(page)
        assert other_session_id != session_id

        await page.call("Page.reload", {"ignoreCache": True})
        await page.wait_for(
            "document.querySelector('#durable-chat-connection-summary').textContent"
            ".toLowerCase().includes('ready')",
            timeout=30,
        )
        assert await page.evaluate(_DETAILS_VISIBLE) == "false"
        await page.wait_for(
            "Array.from(document.querySelectorAll('[data-action=\"select-session\"]'))"
            f".some(button => button.dataset.sessionId === {json.dumps(session_id)})"
        )
        await page.evaluate(
            "Array.from(document.querySelectorAll('[data-action=\"select-session\"]'))"
            f".find(button => button.dataset.sessionId === {json.dumps(session_id)}).click()"
        )
        await page.wait_for(f"{_SELECTED_ID} === {json.dumps(session_id)}")
        await page.wait_for(f"{_REQUEST_TEXT}.includes('Durable answer \u03bb')")

        with HttpClient(host.base_url) as client:
            released = client.post(f"{_PREFIX}/_test/release/{nonce}")
            assert released.status_code == 200
        await page.wait_for(f"{_REQUEST_TEXT}.includes({json.dumps(f'for {nonce}.')})", timeout=45)
        await page.wait_for(
            f"{_REQUEST_TEXT}.toLowerCase().includes('completed')", timeout=45
        )

        await page.call("Page.reload", {"ignoreCache": True})
        await page.wait_for(f"{_REQUEST_TEXT}.includes({json.dumps(f'for {nonce}.')})", timeout=30)
        assert await page.evaluate(_DETAILS_VISIBLE) == "false"
        await page.evaluate(
            "Array.from(document.querySelectorAll('#durable-chat-request-list button'))"
            ".find(button => /details/i.test(button.textContent)).click()"
        )
        assert await page.evaluate(_DETAILS_VISIBLE) == "true"
        assert await page.evaluate(
            "document.querySelector('#durable-chat-run-id').textContent.trim()"
        ) == run_id
        with HttpClient(host.base_url) as client:
            _assert_recorded_run_spans(client, run_id)

        await _verify_mobile_and_capture(page)


@pytest.mark.asyncio
async def test_native_chat_concurrent_sessions_and_cancellation(
    durable_chat_host: HostHandle,
    tmp_path: Path,
) -> None:
    host = durable_chat_host
    first_nonce, second_nonce = uuid4().hex, uuid4().hex
    async with chromium_page(tmp_path / "browser") as page:
        await page.navigate(f"{host.base_url}{_PREFIX}/experimental/durable-chat/")
        await page.wait_for(
            "document.querySelector('#durable-chat-connection-summary').textContent"
            ".toLowerCase().includes('ready')",
            timeout=30,
        )
        first_session = await _new_browser_session(page)
        first_run = await _start_browser_request(page, nonce=first_nonce)
        second_session = await _new_browser_session(page)
        second_run = await _start_browser_request(page, nonce=second_nonce)
        assert first_session != second_session
        assert first_run != second_run
        with HttpClient(host.base_url) as client:
            assert client.post(f"{_PREFIX}/_test/release/{first_nonce}").status_code == 200
        await _wait_for_run_status(host, first_run, "Completed")
        assert await page.evaluate(_SELECTED_ID) == second_session
        assert first_nonce not in await page.evaluate(_REQUEST_TEXT)

        await page.evaluate("document.querySelector('#durable-chat-cancel-request').click()")
        with HttpClient(host.base_url) as client:
            assert client.post(f"{_PREFIX}/_test/release/{second_nonce}").status_code in {
                200,
                404,
            }
        await _wait_for_run_status(host, second_run, "Cancelled")
        await page.wait_for(f"{_REQUEST_TEXT}.toLowerCase().includes('cancelled')")
        assert await page.evaluate(_SELECTED_ID) == second_session
        await page.evaluate(
            "Array.from(document.querySelectorAll('[data-action=\"select-session\"]'))"
            f".find(button => button.dataset.sessionId === {json.dumps(first_session)}).click()"
        )
        await page.wait_for(
            f"{_REQUEST_TEXT}.includes({json.dumps(f'for {first_nonce}.')})", timeout=30
        )
        assert await page.evaluate(_SELECTED_ID) == first_session


def _executed_sandboxes(host: HostHandle, nonce: str) -> list[dict[str, object]]:
    with HttpClient(host.base_url) as client:
        response = client.get(f"{_PREFIX}/_test/sandboxes")
        response.raise_for_status()
        return [
            sandbox
            for sandbox in response.json()["sandboxes"]
            if any(invocation["nonce"] == nonce for invocation in sandbox["invocations"])
        ]


def _assert_native_tool_observations(
    client: HttpClient, host: HostHandle, nonce: str, run_id: str
) -> None:
    sandboxes = _executed_sandboxes(host, nonce)
    assert len(sandboxes) == 2
    response = client.get(
        f"{_PREFIX}/experimental/durable-agent-runs/{run_id}/diagnostics"
    )
    response.raise_for_status()
    observations = response.json()["sandbox_observations"]
    assert {observation["sandbox_id"] for observation in observations} >= {
        sandbox["sandbox_id"] for sandbox in sandboxes
    }
    assert all(sandbox["delete_calls"] == 1 for sandbox in sandboxes)
    for sandbox in sandboxes:
        assert any(
            observation["sandbox_id"] == sandbox["sandbox_id"]
            and observation["sandbox_group_resource_id"] == TEST_SANDBOX_GROUP
            and observation["state"] == "confirmed_deleted"
            and observation["producer"]["call_key"] == sandbox["invocations"][0]["call_key"]
            for observation in observations
        )


async def _assert_sandbox_history(page, sandboxes: list[dict[str, object]]) -> None:
    assert len(sandboxes) == 2
    assert len({sandbox["sandbox_id"] for sandbox in sandboxes}) == 2
    assert all(sandbox["delete_calls"] == 1 for sandbox in sandboxes)
    assert all(sandbox["sandbox_group_resource_id"] == TEST_SANDBOX_GROUP for sandbox in sandboxes)
    for sandbox in sandboxes:
        await page.wait_for(
            "document.querySelector('#durable-chat-sandbox-history').textContent"
            f".includes({json.dumps(sandbox['sandbox_id'])})",
            timeout=30,
        )
    assert await page.evaluate(
        "document.querySelector('#durable-chat-sandbox-group').textContent.trim()"
    ) == TEST_SANDBOX_GROUP


@pytest.mark.asyncio
async def test_native_sandbox_identity_survives_deletion_and_later_turns(
    durable_chat_host: HostHandle,
    tmp_path: Path,
) -> None:
    host = durable_chat_host
    first_nonce, second_nonce = uuid4().hex, uuid4().hex
    async with chromium_page(tmp_path / "browser") as page:
        await page.navigate(f"{host.base_url}{_PREFIX}/experimental/durable-chat/")
        await page.wait_for(
            "document.querySelector('#durable-chat-connection-summary').textContent"
            ".toLowerCase().includes('ready')"
        )
        session_id = await _new_browser_session(page)
        first_run = await _start_browser_request(page, nonce=first_nonce, marker="E2E-TOOLS")
        first_sandboxes = _executed_sandboxes(host, first_nonce)
        await _assert_sandbox_history(page, first_sandboxes)
        with HttpClient(host.base_url) as client:
            assert client.post(f"{_PREFIX}/_test/release/{first_nonce}").status_code == 200
        await _wait_for_run_status(host, first_run, "Completed")

        second_run = await _start_browser_request(page, nonce=second_nonce, marker="E2E-TOOLS")
        assert second_run != first_run
        assert await page.evaluate(_SELECTED_ID) == session_id
        second_sandboxes = _executed_sandboxes(host, second_nonce)
        await _assert_sandbox_history(page, second_sandboxes)
        assert {item["sandbox_id"] for item in first_sandboxes}.isdisjoint(
            item["sandbox_id"] for item in second_sandboxes
        )

        await page.evaluate(
            "Array.from(document.querySelectorAll('#durable-chat-request-list button'))"
            ".find(button => /details/i.test(button.textContent)).click()"
        )
        await page.wait_for(
            "document.querySelector('#durable-chat-run-id').textContent.trim()"
            f" === {json.dumps(first_run)}"
        )
        await _assert_sandbox_history(page, first_sandboxes)
        with HttpClient(host.base_url) as client:
            assert client.post(f"{_PREFIX}/_test/release/{second_nonce}").status_code == 200
        await _wait_for_run_status(host, second_run, "Completed")
        await page.call("Page.reload", {"ignoreCache": True})
        await page.wait_for(
            "document.querySelector('#durable-chat-run-id').textContent.trim()"
            f" === {json.dumps(first_run)}",
            timeout=30,
        )
        await _assert_sandbox_history(page, first_sandboxes)


@pytest.mark.asyncio
async def test_native_human_input_is_actionable_with_details_hidden(
    durable_chat_host: HostHandle,
    tmp_path: Path,
) -> None:
    host = durable_chat_host
    nonce = uuid4().hex
    async with chromium_page(tmp_path / "browser") as page:
        await page.navigate(f"{host.base_url}{_PREFIX}/experimental/durable-chat/")
        await page.wait_for(
            "document.querySelector('#durable-chat-connection-summary').textContent"
            ".toLowerCase().includes('ready')"
        )
        await _new_browser_session(page)
        await page.evaluate("document.querySelector('#durable-chat-details-toggle').click()")
        assert await page.evaluate(_DETAILS_VISIBLE) == "false"
        await _submit_browser_request(page, nonce=nonce, marker="E2E-HUMAN")
        await page.wait_for(
            "Boolean(document.querySelector('[data-action=\"human-choice\"][data-answer=\"Proceed\"]'))",
            timeout=45,
        )
        assert await page.evaluate(_DETAILS_VISIBLE) == "false"
        await page.call("Page.reload", {"ignoreCache": True})
        await page.wait_for(
            "Boolean(document.querySelector('[data-action=\"human-choice\"][data-answer=\"Proceed\"]'))",
            timeout=30,
        )
        await page.evaluate(
            "document.querySelector('[data-action=\"human-choice\"][data-answer=\"Proceed\"]').click()"
        )
        await page.wait_for(f"{_REQUEST_TEXT}.includes('Durable answer \u03bb')", timeout=45)
        run_id = await page.evaluate("document.querySelector('#durable-chat-run-id').textContent.trim()")
        with HttpClient(host.base_url) as client:
            assert client.post(f"{_PREFIX}/_test/release/{nonce}").status_code == 200
        await _wait_for_run_status(host, run_id, "Completed")
        await page.wait_for(f"{_REQUEST_TEXT}.includes({json.dumps(f'for {nonce}.')})")
        assert await page.evaluate(_DETAILS_VISIBLE) == "false"
        assert not await page.evaluate("Boolean(document.querySelector('[data-action=\"human-choice\"]'))")


@pytest.mark.asyncio
async def test_native_lost_start_response_reuses_the_exact_submission(
    durable_chat_host: HostHandle,
    tmp_path: Path,
) -> None:
    host = durable_chat_host
    nonce = uuid4().hex
    async with chromium_page(tmp_path / "browser") as page:
        await page.call(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """(() => {
                  const original = window.fetch.bind(window);
                  window.__startAttempts = [];
                  window.fetch = async (input, init) => {
                    const url = new URL(input, location.href);
                    const isStart = init?.method === "POST"
                      && url.pathname.endsWith("/experimental/durable-agent-runs");
                    if (isStart) {
                      window.__startAttempts.push({
                        body: init.body,
                        key: new Headers(init.headers).get("Idempotency-Key"),
                      });
                    }
                    const response = await original(input, init);
                    if (isStart && response.ok && window.__startAttempts.length === 1) {
                      window.__admittedRunId = (await response.clone().json()).run_id;
                      throw new TypeError("Simulated lost acknowledgement after admission");
                    }
                    return response;
                  };
                })();""",
            },
        )
        await page.navigate(f"{host.base_url}{_PREFIX}/experimental/durable-chat/")
        await page.wait_for(
            "document.querySelector('#durable-chat-connection-summary').textContent"
            ".toLowerCase().includes('ready')"
        )
        await _new_browser_session(page)
        run_id = await _start_browser_request(page, nonce=nonce)
        attempts = await page.evaluate("window.__startAttempts")
        assert len(attempts) >= 2
        assert all(attempt == attempts[0] for attempt in attempts)
        assert attempts[0]["key"]
        assert run_id == await page.evaluate("window.__admittedRunId")
        with HttpClient(host.base_url) as client:
            assert client.post(f"{_PREFIX}/_test/release/{nonce}").status_code == 200
        await _wait_for_run_status(host, run_id, "Completed")
        await page.wait_for(f"{_REQUEST_TEXT}.includes({json.dumps(f'for {nonce}.')})")


@pytest.mark.asyncio
async def test_native_retained_sandbox_reuse_and_cancel_cleanup_are_observed(
    durable_chat_host: HostHandle,
    tmp_path: Path,
) -> None:
    host = durable_chat_host
    nonce = uuid4().hex
    async with chromium_page(tmp_path / "browser") as page:
        await page.navigate(f"{host.base_url}{_PREFIX}/experimental/durable-chat/")
        await page.wait_for(
            "document.querySelector('#durable-chat-connection-summary').textContent"
            ".toLowerCase().includes('ready')"
        )
        await _new_browser_session(page)
        await page.evaluate(
            "document.querySelector('#durable-chat-sandbox-profile').value = 'retained_session';"
            "document.querySelector('#durable-chat-sandbox-profile').dispatchEvent("
            "new Event('change', { bubbles: true }))"
        )
        run_id = await _start_browser_request(page, nonce=nonce, marker="E2E-TOOLS")
        [sandbox] = _executed_sandboxes(host, nonce)
        assert len(sandbox["invocations"]) == 2
        assert sandbox["delete_calls"] == 0
        await page.evaluate("document.querySelector('#durable-chat-cancel-request').click()")
        with HttpClient(host.base_url) as client:
            assert client.post(f"{_PREFIX}/_test/release/{nonce}").status_code in {200, 404}
        await _wait_for_run_status(host, run_id, "Cancelled")
        [deleted] = _executed_sandboxes(host, nonce)
        assert deleted["sandbox_id"] == sandbox["sandbox_id"]
        assert deleted["delete_calls"] == 1
        with HttpClient(host.base_url) as client:
            diagnostics = client.get(
                f"{_PREFIX}/experimental/durable-agent-runs/{run_id}/diagnostics"
            ).json()
        assert any(
            observation["sandbox_id"] == sandbox["sandbox_id"]
            and observation["state"] == "confirmed_deleted"
            for observation in diagnostics["sandbox_observations"]
        )
        await page.wait_for(
            "document.querySelector('#durable-chat-sandbox-history').textContent"
            f".includes({json.dumps(sandbox['sandbox_id'])})",
            timeout=30,
        )
        await page.wait_for(
            "document.querySelector('#durable-chat-sandbox-history').textContent"
            ".toLowerCase().includes('deleted')",
            timeout=30,
        )
        assert await page.evaluate(
            "document.querySelector('#durable-chat-sandbox-id').textContent.trim()"
        ) == sandbox["sandbox_id"]
