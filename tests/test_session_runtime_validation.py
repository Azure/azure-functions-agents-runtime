"""Startup-validation tests for FRD 0008's ``session_runtime`` config surface (P2).

Each test below drives ``create_function_app()`` against a static fixture
folder under ``fixtures/config_scenarios/`` (17-30, with gaps at 22 and 28 --
see below), mirroring the existing pipeline-level test style in
``test_app.py`` (fixture + ``pytest.raises``) rather than the schema-level
``pytest.raises(ValidationError)`` style used for pure Pydantic rejections
(see ``test_config_schema.py`` for those, including the four dropped-field
rejection tests).

Fixture-to-row map (see ``docs/frds/0008-aca-sandbox-session-runtime.md``'s
"Matrix: aca_sandbox startup/configuration behavior" table for the full
row text):

* ``17_session_runtime_absent``                         -> no row fires; default (in-process) backend
* ``18_aca_sandbox_row1_bad_harness``                    -> Row 1  (harness != maf)
* ``19_aca_sandbox_row2_workflows_enabled``               -> Row 2  (workflows.enabled)
* ``20_aca_sandbox_row3_dynamic_sessions_conflict``        -> Row 3  (Dynamic Sessions code interpreter)
* ``21_aca_sandbox_row4_non_http_trigger``                 -> Row 4  (non-HTTP trigger)
* ``23_aca_sandbox_row6_shared_key_state_storage``         -> Row 6  (Shared Key state storage)
* ``24_aca_sandbox_row7_missing_dedicated_storage``        -> Row 7  (no dedicated state storage)
* ``25_aca_sandbox_row8_anonymous_auth``                   -> Row 8  (anonymous HTTP access)
* ``26_aca_sandbox_row9_bad_auto_suspend_idle``            -> Row 9  (bad auto_suspend_idle)
* ``27_aca_sandbox_row10_bad_reclaim_idle``                -> Row 10 (bad reclaim_idle)
* ``29_aca_sandbox_valid_but_unavailable``                 -> Row 12 (platform) + Row 13
  (always-hard-fail backstop, vacuously satisfied) + "valid config parses but
  fails the capability gate". One physical fixture serves all three: rows 12
  and 13 have no independently representable *config-content* violation (13
  in particular is a live-SDK-value check deferred past this phase -- see
  ``auto_delete_backstop_violated``'s docstring in ``config/validation.py``),
  so the same otherwise-fully-valid fixture is exercised three times below
  with different ``platform``/``sys.version_info`` monkeypatching per test.
* ``30_aca_sandbox_explicit_null``                        -> Row 5 edge case:
  a bare ``aca_sandbox:`` key (explicit ``null``, present in the mapping --
  distinct from the key being *omitted*, which is fixture 17's case). See the
  Row 5 discussion below for why this is schema-level, not
  ``validate_session_runtime``-level.

**Gaps at 22 and 28 (former Row 5 / Row 11 fixtures):** the ``provider``
field's removal (Decision #84) eliminated the fixture *scenario* each of
these exercised, so their fixtures and dedicated tests were deleted outright
instead of repurposed -- but for different underlying reasons. Only Row 11's
*requirement* is itself gone (structurally unrepresentable); Row 5's
requirement remains fully active and enforced, just no longer through a
dedicated ``validate_session_runtime`` fixture (see
``docs/architecture.md``'s "12 of the FRD's original 13 rows still active"
framing):

* Row 5 ("``aca_sandbox`` selected but the block is absent") required a
  ``provider`` flag to select ``aca_sandbox`` independently of the block's
  presence. With ``provider`` gone, the block's presence *is* the selection,
  so "selected but absent" no longer parses as a concept -- the fixture's
  ``session_runtime: {provider: aca_sandbox}`` (no ``aca_sandbox:`` block)
  now just fails Pydantic's ``extra="forbid"`` on the unknown ``provider``
  key (see ``test_config_schema.py::test_session_runtime_config_rejects_provider_field``).
  Two Row-5-shaped cases remain, both enforced at the schema layer (never
  something ``validate_session_runtime`` itself raises):

  * an ``aca_sandbox: {}`` block present but missing its required
    ``sandbox_group_resource_id`` -- a plain Pydantic required-field error;
    ``AcaSandboxConfig`` construction runs (the key's value is a dict, not
    ``None``) and fails on the missing field. No dedicated fixture; see
    ``test_config_schema.py::test_aca_sandbox_config_rejects_missing_sandbox_group_resource_id``.
  * a bare ``aca_sandbox:`` key -- explicit ``None``, present in the mapping
    but distinct from the key being *omitted*. Pydantic's union matching for
    ``AcaSandboxConfig | None`` matches an explicit ``None`` directly against
    the ``None`` arm *without ever attempting to construct*
    ``AcaSandboxConfig``, so its required-field check never runs -- left
    unguarded, this silently selects the in-process default instead of
    failing startup (fail-open, not fail-closed). A dedicated
    ``model_validator(mode="before")`` on ``SessionRuntimeConfig``
    (``_check_explicit_null_aca_sandbox``, alongside the existing
    ``_check_dropped_fields``) rejects this explicitly. See fixture
    ``30_aca_sandbox_explicit_null`` and
    ``test_row5_explicit_null_aca_sandbox_fails_startup`` below, plus the
    schema-level tests in ``test_config_schema.py``
    (``test_session_runtime_config_rejects_explicit_null_aca_sandbox`` and
    ``test_global_config_session_runtime_rejects_explicit_null_aca_sandbox``).
* Row 11 ("``retention`` set but provider != aca_sandbox") required
  authoring ``retention`` as a sibling of ``aca_sandbox`` while some other
  provider was active. ``retention`` moved onto ``AcaSandboxConfig`` (Decision
  #84), so it can no longer be authored anywhere except nested inside
  ``aca_sandbox`` -- the fixture's ``session_runtime: {retention: {...}}``
  (no ``aca_sandbox:`` block) now just fails Pydantic's ``extra="forbid"``
  on the unknown ``retention`` key (see
  ``test_config_schema.py::test_session_runtime_config_rejects_retention_as_sibling_of_aca_sandbox``).

Only rows that run after ``_validate_state_storage_auth_mode`` /
``_validate_state_storage_dedicated_account`` in ``validate_session_runtime``'s
execution order (2, 3, 4, 8, 12, and 29's valid-but-gated case) need the
dedicated-state-storage env vars monkeypatched at all; rows 1, 9, and 10 fire
earlier and need no environment setup.
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


def test_session_runtime_absent_defaults_to_in_lang_worker_no_row_fires() -> None:
    """Absence of ``session_runtime`` selects in_lang_worker; no matrix row fires.

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


def test_row5_explicit_null_aca_sandbox_fails_startup() -> None:
    """Row 5 edge case: a bare ``aca_sandbox:`` key (explicit ``null``) must
    fail startup, not silently select the in-process default.

    This fires at the Pydantic schema layer (``GlobalConfig.model_validate``
    inside ``load_global_config``), before ``validate_session_runtime`` ever
    runs -- like rows 1, 9, and 10, no environment/monkeypatching is needed.
    """
    with pytest.raises(ValueError, match=r"aca_sandbox.*must not be explicitly `null`") as exc_info:
        create_function_app(FIXTURES_ROOT / "30_aca_sandbox_explicit_null")
    message = str(exc_info.value)
    assert "session_runtime.aca_sandbox" in message


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
    3.13), the fixture is otherwise fully valid (all other matrix rows
    pass) -- proving it *parses* successfully -- yet still fails at the final,
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
