"""Unit coverage for ACA live-smoke diagnostic classification."""

from __future__ import annotations

import pytest
from azure.core.exceptions import HttpResponseError

from azure_functions_agents.transport.transport_models import SandboxGroupBindingError
from tests.aca_smoke_diagnostics import classify_aca_smoke_exception


def _http_response_error(status_code: int) -> HttpResponseError:
    error = HttpResponseError("sandbox transport request failed")
    error.status_code = status_code
    return error


@pytest.mark.parametrize(
    ("name", "error"),
    [
        ("authentication-401", _http_response_error(401)),
        ("authorization-403", _http_response_error(403)),
        ("group-not-found-404", _http_response_error(404)),
        ("quota-429", _http_response_error(429)),
        (
            "region-mismatch",
            SandboxGroupBindingError("Persisted Sandbox Group region does not match."),
        ),
        ("create-timeout", TimeoutError("sandbox creation timed out")),
    ],
)
def test_aca_smoke_setup_failures_are_operations_errors(name: str, error: BaseException) -> None:
    assert classify_aca_smoke_exception(error) == "environment", name
