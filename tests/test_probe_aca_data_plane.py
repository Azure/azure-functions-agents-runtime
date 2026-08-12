"""Default-gate coverage for the ACA data-plane preflight probe."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from azure.core.exceptions import HttpResponseError

_PROBE_PATH = Path(__file__).resolve().parents[1] / "eng" / "scripts" / "probe_aca_data_plane.py"
_GROUP_ID = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.App/sandboxGroups/smoke"


def _load_probe() -> object:
    spec = importlib.util.spec_from_file_location("probe_aca_data_plane", _PROBE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


def _forbidden() -> HttpResponseError:
    error = HttpResponseError("Operation returned an invalid status 'Forbidden'")
    error.status_code = 403
    return error


def test_probe_deadline_reports_authorization_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def _timeout_probe(_group: str) -> None:
        raise TimeoutError

    monkeypatch.setenv(probe._GROUP_RESOURCE_ID_ENV_VAR, _GROUP_ID)
    monkeypatch.setattr(probe, "_probe", _timeout_probe)

    assert probe.main() == 1
    stderr = capsys.readouterr().err
    assert probe._DATA_OWNER_ROLE_ID in stderr
    assert "deadline" in stderr


def test_probe_403_reports_authorization_failure_without_slow_note(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def _forbidden_probe(_group: str) -> None:
        raise _forbidden()

    monkeypatch.setenv(probe._GROUP_RESOURCE_ID_ENV_VAR, _GROUP_ID)
    monkeypatch.setattr(probe, "_probe", _forbidden_probe)

    assert probe.main() == 1
    stderr = capsys.readouterr().err
    assert probe._DATA_OWNER_ROLE in stderr
    assert "deadline" not in stderr


def test_probe_success_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok_probe(_group: str) -> None:
        return None

    monkeypatch.setenv(probe._GROUP_RESOURCE_ID_ENV_VAR, _GROUP_ID)
    monkeypatch.setattr(probe, "_probe", _ok_probe)

    assert probe.main() == 0
