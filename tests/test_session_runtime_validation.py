"""Startup-validation tests for FRD 0008's ``session_runtime`` config surface (P2).

Each test below drives ``create_function_app()`` against a static fixture
folder under ``fixtures/config_scenarios/`` (17-29), mirroring the existing
pipeline-level test style in ``test_app.py`` (fixture + ``pytest.raises``)
rather than the schema-level ``pytest.raises(ValidationError)`` style used
for pure Pydantic rejections (see ``test_config_schema.py`` for those,
including the four dropped-field rejection tests).

Fixture-to-row map (see ``docs/frds/0008-aca-sandbox-session-runtime.md``'s
"Matrix: aca_sandbox startup/configuration behavior" table for the full
row text):

* ``17_session_runtime_absent``                         -> no row fires; in_process default
* ``18_aca_sandbox_row1_bad_harness``                    -> Row 1  (harness != maf)
* ``19_aca_sandbox_row2_workflows_enabled``               -> Row 2  (workflows.enabled)
* ``20_aca_sandbox_row3_dynamic_sessions_conflict``        -> Row 3  (Dynamic Sessions code interpreter)
* ``21_aca_sandbox_row4_non_http_trigger``                 -> Row 4  (non-HTTP trigger)
* ``22_aca_sandbox_row5_missing_sandbox_group``            -> Row 5  (missing aca_sandbox block)
* ``23_aca_sandbox_row6_shared_key_state_storage``         -> Row 6  (Shared Key state storage)
* ``24_aca_sandbox_row7_missing_dedicated_storage``        -> Row 7  (no dedicated state storage)
* ``25_aca_sandbox_row8_anonymous_auth``                   -> Row 8  (anonymous HTTP access)
* ``26_aca_sandbox_row9_bad_auto_suspend_idle``            -> Row 9  (bad auto_suspend_idle)
* ``27_aca_sandbox_row10_bad_reclaim_idle``                -> Row 10 (bad reclaim_idle)
* ``28_aca_sandbox_row11_retention_without_aca_sandbox``   -> Row 11 (retention + non-aca_sandbox)
* ``29_aca_sandbox_valid_but_unavailable``                 -> Row 12 (platform) + Row 13
  (always-hard-fail backstop, vacuously satisfied) + "valid config parses but
  fails the capability gate". One physical fixture serves all three: rows 12
  and 13 have no independently representable *config-content* violation (13
  in particular is a live-SDK-value check deferred past this phase -- see
  ``auto_delete_backstop_violated``'s docstring in ``config/validation.py``),
  so the same otherwise-fully-valid fixture is exercised three times below
  with different ``platform``/``sys.version_info`` monkeypatching per test.

Only rows that run after ``_validate_state_storage_auth_mode`` /
``_validate_state_storage_dedicated_account`` in ``validate_session_runtime``'s
execution order (2, 3, 4, 8, 12, and 29's valid-but-gated case) need the
dedicated-state-storage env vars monkeypatched at all; rows 1, 5, 9, 10, and
11 fire earlier and need no environment setup.
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

import pytest

from azure_functions_agents.app import create_function_app

FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures" / "config_scenarios"

_STATE_STORAGE_SETTING_NAME = "AzureFunctionsAgentsStateStorage"


def _set_identity_based_state_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure a dedicated, identity-based (RBAC) state-storage connection.

    Satisfies Row 7 (a dedicated account exists) without tripping Row 6 (no
    Shared Key marker), using the ``<name>__blobServiceUri`` /
    ``<name>__tableServiceUri`` sibling-setting convention that
    ``config/env.py``'s ``runtime_env_value`` reads directly from
    ``os.environ``.
    """
    monkeypatch.delenv(_STATE_STORAGE_SETTING_NAME, raising=False)
    monkeypatch.setenv(
        f"{_STATE_STORAGE_SETTING_NAME}__blobServiceUri",
        "https://exampleacct.blob.core.windows.net",
    )
    monkeypatch.setenv(
        f"{_STATE_STORAGE_SETTING_NAME}__tableServiceUri",
        "https://exampleacct.table.core.windows.net",
    )


def _clear_state_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no dedicated state-storage setting exists at all (for Row 7)."""
    monkeypatch.delenv(_STATE_STORAGE_SETTING_NAME, raising=False)
    monkeypatch.delenv(f"{_STATE_STORAGE_SETTING_NAME}__blobServiceUri", raising=False)
    monkeypatch.delenv(f"{_STATE_STORAGE_SETTING_NAME}__tableServiceUri", raising=False)


def test_session_runtime_absent_defaults_to_in_process_no_row_fires() -> None:
    """Absence of ``session_runtime`` selects in_process; no matrix row fires.

    No environment/monkeypatching at all is required for this fixture to
    build successfully -- the strongest available proof that the default
    path is untouched by FRD 0008.
    """
    app = create_function_app(FIXTURES_ROOT / "17_session_runtime_absent")
    assert app.get_functions()


def test_row1_bad_harness_fails_startup() -> None:
    with pytest.raises(ValueError, match=r"harness") as exc_info:
        create_function_app(FIXTURES_ROOT / "18_aca_sandbox_row1_bad_harness")
    message = str(exc_info.value)
    assert "maf" in message
    assert "custom-harness" in message


def test_row2_workflows_enabled_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_identity_based_state_storage(monkeypatch)
    with pytest.raises(ValueError, match=r"[Ww]orkflows") as exc_info:
        create_function_app(FIXTURES_ROOT / "19_aca_sandbox_row2_workflows_enabled")
    message = str(exc_info.value)
    assert "aca_sandbox" in message
    assert "main.agent.md" in message


def test_row3_dynamic_sessions_conflict_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_identity_based_state_storage(monkeypatch)
    with pytest.raises(ValueError, match=r"[Dd]ynamic [Ss]essions") as exc_info:
        create_function_app(FIXTURES_ROOT / "20_aca_sandbox_row3_dynamic_sessions_conflict")
    message = str(exc_info.value)
    assert "aca_sandbox" in message
    assert "system_tools.dynamic_sessions_code_interpreter" in message


def test_row4_non_http_trigger_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_identity_based_state_storage(monkeypatch)
    with pytest.raises(ValueError, match=r"http_trigger") as exc_info:
        create_function_app(FIXTURES_ROOT / "21_aca_sandbox_row4_non_http_trigger")
    message = str(exc_info.value)
    assert "timer_trigger" in message
    assert "main.agent.md" in message


def test_row5_missing_sandbox_group_fails_startup() -> None:
    with pytest.raises(ValueError, match=r"sandbox_group_resource_id") as exc_info:
        create_function_app(FIXTURES_ROOT / "22_aca_sandbox_row5_missing_sandbox_group")
    message = str(exc_info.value)
    assert "Required" in message
    assert "aca_sandbox" in message


def test_row6_shared_key_state_storage_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(f"{_STATE_STORAGE_SETTING_NAME}__blobServiceUri", raising=False)
    monkeypatch.delenv(f"{_STATE_STORAGE_SETTING_NAME}__tableServiceUri", raising=False)
    monkeypatch.setenv(
        _STATE_STORAGE_SETTING_NAME,
        "DefaultEndpointsProtocol=https;AccountName=fakeacct;"
        "AccountKey=ZmFrZWtleQ==;EndpointSuffix=core.windows.net",
    )
    with pytest.raises(ValueError, match=r"Shared Key") as exc_info:
        create_function_app(FIXTURES_ROOT / "23_aca_sandbox_row6_shared_key_state_storage")
    message = str(exc_info.value)
    assert _STATE_STORAGE_SETTING_NAME in message
    assert "managed-identity" in message


def test_row7_missing_dedicated_storage_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_state_storage(monkeypatch)
    with pytest.raises(ValueError, match=r"dedicated") as exc_info:
        create_function_app(FIXTURES_ROOT / "24_aca_sandbox_row7_missing_dedicated_storage")
    message = str(exc_info.value)
    assert "AzureWebJobsStorage" in message
    assert _STATE_STORAGE_SETTING_NAME in message


def test_row8_anonymous_auth_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_identity_based_state_storage(monkeypatch)
    with pytest.raises(ValueError, match=r"Anonymous") as exc_info:
        create_function_app(FIXTURES_ROOT / "25_aca_sandbox_row8_anonymous_auth")
    message = str(exc_info.value)
    assert "aca_sandbox" in message
    assert "main.agent.md" in message


def test_row9_bad_auto_suspend_idle_fails_startup() -> None:
    with pytest.raises(ValueError, match=r"auto_suspend_idle") as exc_info:
        create_function_app(FIXTURES_ROOT / "26_aca_sandbox_row9_bad_auto_suspend_idle")
    message = str(exc_info.value)
    assert "45" in message
    assert "60, 120, 300, 600, 1800, 3600" in message


def test_row10_bad_reclaim_idle_fails_startup() -> None:
    with pytest.raises(ValueError, match=r"reclaim_idle") as exc_info:
        create_function_app(FIXTURES_ROOT / "27_aca_sandbox_row10_bad_reclaim_idle")
    message = str(exc_info.value)
    assert "100" in message
    assert "300" in message


def test_row11_retention_without_aca_sandbox_fails_startup() -> None:
    with pytest.raises(ValueError, match=r"retention") as exc_info:
        create_function_app(
            FIXTURES_ROOT / "28_aca_sandbox_row11_retention_without_aca_sandbox"
        )
    message = str(exc_info.value)
    assert "aca_sandbox" in message


def test_row12_unsupported_platform_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 12: an unsupported Function App host ABI fails startup.

    Deterministically forces the "wrong platform" branch via monkeypatching
    (the fixture is otherwise fully valid) so this test's outcome does not
    depend on which OS actually runs the test suite.
    """
    _set_identity_based_state_storage(monkeypatch)
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    with pytest.raises(ValueError, match=r"Linux") as exc_info:
        create_function_app(FIXTURES_ROOT / "29_aca_sandbox_valid_but_unavailable")
    message = str(exc_info.value)
    assert "Windows" in message
    assert "aca_sandbox" in message


def test_row13_backstop_and_valid_config_fail_capability_gate_not_row12(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Row 13 (always-hard-fail) + "valid config parses but fails the gate".

    With the host mocked to a *supported* platform (Linux/x86_64/Python
    3.13), the fixture is otherwise fully valid (rows 1-11 all pass) --
    proving it *parses* successfully -- yet still fails at the final,
    unconditional capability-gate raise rather than reaching Row 12's
    platform-specific error. That same unconditional raise is exactly what
    vacuously satisfies Row 13's "always a hard fail" requirement (see
    ``auto_delete_backstop_violated``'s docstring in ``config/validation.py``):
    no ``aca_sandbox`` session can ever run in this build regardless of
    retention/backstop math.
    """
    _set_identity_based_state_storage(monkeypatch)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(sys, "version_info", (3, 13, 0, "final", 0))
    with pytest.raises(ValueError, match=r"not available in this build") as exc_info:
        create_function_app(FIXTURES_ROOT / "29_aca_sandbox_valid_but_unavailable")
    message = str(exc_info.value)
    assert "aca_sandbox" in message
