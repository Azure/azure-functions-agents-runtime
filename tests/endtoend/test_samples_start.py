"""End-to-end tests that boot each sample Function App with ``func start``.

These tests require Azure Functions Core Tools (``func``) and a running Azurite
instance, so they are marked ``e2e`` and excluded from the default unit-test run
(see ``addopts`` in ``pyproject.toml``). The E2E pipeline runs them explicitly
with ``-m e2e``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.endtoend._func_host import start_and_verify

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLES_DIR = REPO_ROOT / "samples"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(shutil.which("func") is None, reason="Azure Functions Core Tools not found"),
]


def _discover_sample_apps() -> list[Path]:
    """Return every sample Function App directory (those with a host.json)."""
    return sorted(host.parent for host in SAMPLES_DIR.glob("*/src/host.json"))


SAMPLE_APPS = _discover_sample_apps()


@pytest.mark.parametrize(
    "app_dir",
    SAMPLE_APPS,
    ids=[app.parent.name for app in SAMPLE_APPS],
)
def test_sample_app_starts(app_dir: Path) -> None:
    """Each sample app should start cleanly under ``func start``."""
    result = start_and_verify(app_dir)
    assert result.started, (
        f"`func start` did not start cleanly for '{app_dir.parent.name}': "
        f"{result.reason}\n"
        f"--- func output ---\n{result.output}"
    )
