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

from azure_functions_agents._slug import _function_name_from_source
from azure_functions_agents.config.loader import load_agent_specs
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


def _startup_env(app_dir: Path) -> dict[str, str]:
    """Disable timers while testing host startup so scheduled work cannot race CI."""
    return {
        f"AzureWebJobs.{_function_name_from_source(spec.source_file, spec.name)}.Disabled": "true"
        for spec in load_agent_specs(app_dir)
        if spec.trigger is not None and spec.trigger.type == "timer_trigger"
    }


def test_startup_env_disables_timer_functions_only() -> None:
    assert _startup_env(SAMPLES_DIR / "daily-tech-news-email" / "src") == {
        "AzureWebJobs.daily_tech_news.Disabled": "true"
    }
    assert _startup_env(SAMPLES_DIR / "workflow-queue-p0-report" / "src") == {}


@pytest.mark.parametrize(
    "app_dir",
    SAMPLE_APPS,
    ids=[app.parent.name for app in SAMPLE_APPS],
)
def test_sample_app_starts(app_dir: Path) -> None:
    """Each sample app should start cleanly under ``func start``."""
    result = start_and_verify(app_dir, env=_startup_env(app_dir))
    assert result.started, (
        f"`func start` did not start cleanly for '{app_dir.parent.name}': "
        f"{result.reason}\n"
        f"--- func output ---\n{result.output}"
    )
