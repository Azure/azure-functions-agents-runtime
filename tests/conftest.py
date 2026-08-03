from __future__ import annotations

import sys
from pathlib import Path

import pytest

from azure_functions_agents.controller.package import CapturedContentPackage
from tests.doubles.content_package import content_package

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


@pytest.fixture
def deterministic_content_package(
    monkeypatch: pytest.MonkeyPatch,
) -> CapturedContentPackage:
    async def get_content_package(_: Path) -> CapturedContentPackage:
        return content_package()

    monkeypatch.setattr(
        "azure_functions_agents.controller.readiness.get_content_package",
        get_content_package,
    )
    return content_package()
