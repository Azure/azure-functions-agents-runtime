from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


@pytest.fixture(autouse=True)
def _explicitly_select_maf_for_unit_tests(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Keep MAF-specific tests on the explicit opt-out path."""
    if request.node.get_closest_marker("e2e") is None:
        monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT", "false")
