"""End-to-end functional tests that invoke timer-triggered agents.

Timer triggers have no HTTP route, so they are fired locally through the
Functions admin API (``POST /admin/functions/{name}`` with ``{"input": ...}``),
which returns ``202 Accepted`` and runs the function in the background.

The deterministic, provider-independent signal that a timer worked is the host
log line ``Executed 'Functions.<name>'``: it appears whether the agent run
succeeds or fails, proving the admin invoke reached the registered timer handler
and the function executed. A happy-path assertion (successful agent run) needs a
live LLM and auto-skips unless a provider is configured.

Like the other E2E tests these require ``func`` + Azurite and are marked ``e2e``
(excluded from the default unit run; the E2E pipeline runs ``-m e2e``).
"""

from __future__ import annotations

import contextlib
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.endtoend._func_host import HostHandle, running_host
from tests.endtoend._http_probe import (
    HttpClient,
    discover_functions,
    expect_status,
    find_functions,
    invoke_admin_function,
)

APPS_DIR = Path(__file__).resolve().parent / "apps"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(shutil.which("func") is None, reason="Azure Functions Core Tools not found"),
]

# Served timer hosts are (handle, client): the handle exposes host output so we
# can assert the function actually executed after an admin invoke.
Served = tuple[HostHandle, HttpClient]


def _provider_configured() -> bool:
    """Whether an LLM provider appears configured in the environment."""
    return bool(
        os.environ.get("OPENAI_API_KEY")
        or os.environ.get("AZURE_OPENAI_ENDPOINT")
        or os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    )


requires_llm = pytest.mark.skipif(
    not _provider_configured(), reason="no LLM provider configured (set OPENAI_API_KEY etc.)"
)


@contextlib.contextmanager
def _serve(app_name: str) -> Iterator[Served]:
    """Start ``app_name`` under ``func start`` and yield its handle + a client."""
    with running_host(APPS_DIR / app_name) as handle:
        client = HttpClient(handle.base_url)
        try:
            client.wait_until_responsive()
            yield handle, client
        finally:
            client.close()


@pytest.fixture(scope="module")
def storage_triggers_host() -> Iterator[Served]:
    with _serve("storage-triggers") as served:
        yield served


@pytest.fixture(scope="module")
def builtin_endpoints_host() -> Iterator[Served]:
    with _serve("builtin-endpoints") as served:
        yield served


# --------------------------------------------------------------------------- #
# Timer discovery
# --------------------------------------------------------------------------- #


def test_admin_api_discovers_timer_function(storage_triggers_host: Served) -> None:
    """The storage-triggers app should register exactly one timer function."""
    _, client = storage_triggers_host

    timers = find_functions(discover_functions(client), trigger_type="timerTrigger")

    assert timers, "expected at least one timer-triggered function to be indexed"
    for timer in timers:
        assert timer.route is None, "timer triggers must not expose an HTTP route"
        assert timer.methods == (), "timer triggers must not list HTTP methods"


def test_builtin_endpoints_indexes_timer_alongside_http(builtin_endpoints_host: Served) -> None:
    """A timer agent and the builtin HTTP endpoints coexist in one app."""
    _, client = builtin_endpoints_host
    functions = discover_functions(client)

    timers = find_functions(functions, trigger_type="timerTrigger")
    https = find_functions(functions, trigger_type="httpTrigger")

    assert timers, "expected the api_only timer to be indexed"
    assert https, "expected the builtin chat/mcp HTTP endpoints to be indexed"


# --------------------------------------------------------------------------- #
# Timer invocation
# --------------------------------------------------------------------------- #


def test_timer_admin_invoke_accepted_and_executes(storage_triggers_host: Served) -> None:
    """Firing the timer via the admin API returns 202 and the function runs.

    The ``Executed 'Functions.<name>'`` marker is provider-independent: it is
    logged whether the agent run succeeds or fails, so this asserts the admin
    invoke reached the registered timer handler and the function completed.
    """
    handle, client = storage_triggers_host

    timers = find_functions(discover_functions(client), trigger_type="timerTrigger")
    assert timers, "expected a timer function to invoke"
    timer = timers[0]

    resp = invoke_admin_function(client, timer.name)
    expect_status(resp, 202)

    executed = handle.wait_for_log(f"Executed 'Functions.{timer.name}'", timeout=90.0)
    assert executed, (
        f"host never logged execution of timer '{timer.name}'. "
        f"Recent output:\n{handle.read_output()[-2000:]}"
    )


@requires_llm
def test_timer_run_succeeds_with_provider(storage_triggers_host: Served) -> None:
    """With an LLM provider configured, the timer agent run should succeed."""
    handle, client = storage_triggers_host

    timers = find_functions(discover_functions(client), trigger_type="timerTrigger")
    timer = timers[0]

    resp = invoke_admin_function(client, timer.name)
    expect_status(resp, 202)

    succeeded = handle.wait_for_log(
        f"Executed 'Functions.{timer.name}' (Succeeded", timeout=120.0
    )
    assert succeeded, (
        f"timer '{timer.name}' did not complete successfully. "
        f"Recent output:\n{handle.read_output()[-2000:]}"
    )
