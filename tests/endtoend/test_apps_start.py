"""End-to-end tests that boot each curated E2E Function App with ``func start``.

These apps live under ``tests/endtoend/apps`` and are purpose-built to exercise
the runtime's discovery/registration surface (triggers, builtin endpoints,
capability filtering, env substitution, tools/skills, web_request, workflows).

Like the sample-start tests, they require Azure Functions Core Tools (``func``)
and a running Azurite instance, so they are marked ``e2e`` and excluded from the
default unit-test run (see ``addopts`` in ``pyproject.toml``). The E2E pipeline
runs them explicitly with ``-m e2e``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.endtoend._func_host import start_and_verify

APPS_DIR = Path(__file__).resolve().parent / "apps"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(shutil.which("func") is None, reason="Azure Functions Core Tools not found"),
]


def _discover_e2e_apps() -> list[Path]:
    """Return every E2E Function App directory (those with a host.json)."""
    return sorted(host.parent for host in APPS_DIR.glob("*/host.json"))


E2E_APPS = _discover_e2e_apps()


@pytest.mark.parametrize(
    "app_dir",
    E2E_APPS,
    ids=[app.name for app in E2E_APPS],
)
def test_e2e_app_starts(app_dir: Path) -> None:
    """Each curated E2E app should start cleanly under ``func start``."""
    result = start_and_verify(app_dir)
    assert result.started, (
        f"`func start` did not start cleanly for '{app_dir.name}': "
        f"{result.reason}\n"
        f"--- func output ---\n{result.output}"
    )
