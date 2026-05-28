"""E2E drift-guard: per-sample expected functions must match runtime registration.

Runs in normal pytest CI. No external resources required.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from scripts.e2e import expectations
from scripts.e2e.expectations import SampleExpectations

from azure_functions_agents import create_function_app

REPO_ROOT = Path(__file__).resolve().parents[2]
_SAMPLES = expectations.list_samples()

_STUB_ENV: dict[str, str] = {
    "ACA_SESSION_POOL_ENDPOINT": "https://stub-sessions.example.com",
    "AZURE_CLIENT_ID": "00000000-0000-0000-0000-000000000000",
    "AZURE_CLIENT_SECRET": "stub-client-secret",
    "AZURE_TENANT_ID": "00000000-0000-0000-0000-000000000000",
    "FOUNDRY_MODEL": "gpt-5-stub",
    "FOUNDRY_PROJECT_ENDPOINT": "https://stub.example.com",
    "MAF_REASONING_EFFORT": "low",
    "MAF_REASONING_SUMMARY": "concise",
    "O365_MCP_CLIENT_ID": "00000000-0000-0000-0000-000000000000",
    "O365_MCP_SERVER_URL": "https://stub-mcp.example.com",
    "SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000000",
    "TO_EMAIL": "stub@example.com",
    "WATCHED_SENDER_EMAIL": "stub@example.com",
}


def _function_names(functions: list[Any]) -> frozenset[str]:
    return frozenset(function.get_function_name() for function in functions)


@pytest.fixture
def stubbed_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Seed stub env vars so create_function_app() can substitute placeholders."""
    for key, value in _STUB_ENV.items():
        monkeypatch.setenv(key, value)
    yield


@pytest.mark.parametrize("sample", _SAMPLES, ids=[sample.name for sample in _SAMPLES])
def test_expected_functions_match_runtime(
    sample: SampleExpectations,
    stubbed_env: None,
) -> None:
    """Every sample's expected function set must match runtime registration."""
    sample_path = REPO_ROOT / sample.sample_path
    assert sample_path.exists(), f"Sample path missing: {sample_path}"

    app = create_function_app(sample_path)
    registered = _function_names(app.get_functions())

    missing_in_runtime = sample.expected_function_names - registered
    extra_in_runtime = registered - sample.expected_function_names

    assert not missing_in_runtime and not extra_in_runtime, (
        f"Drift in sample '{sample.name}': "
        f"expected={sorted(sample.expected_function_names)} "
        f"registered={sorted(registered)} "
        f"missing={sorted(missing_in_runtime)} "
        f"extra={sorted(extra_in_runtime)}"
    )


@pytest.mark.parametrize("sample", _SAMPLES, ids=[sample.name for sample in _SAMPLES])
def test_invocations_reference_expected_functions(sample: SampleExpectations) -> None:
    """Every invocation must target a function listed in expected_function_names."""
    for invocation in sample.invocations:
        assert invocation.function_name in sample.expected_function_names, (
            f"Sample '{sample.name}' has invocation for function "
            f"'{invocation.function_name}' not listed in expected_function_names"
        )


@pytest.mark.parametrize("sample", _SAMPLES, ids=[sample.name for sample in _SAMPLES])
def test_skip_set_subset_of_expected(sample: SampleExpectations) -> None:
    """skip_invocation_function_names must be a subset of expected_function_names."""
    extra = sample.skip_invocation_function_names - sample.expected_function_names
    assert not extra, (
        f"Sample '{sample.name}' has skip_invocation_function_names not in expected: "
        f"{sorted(extra)}"
    )
