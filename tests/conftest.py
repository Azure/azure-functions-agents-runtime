from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Must precede the first-party imports below: without it, running pytest from a
# checkout that has not been installed cannot resolve azure_functions_agents.
SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from azure_functions_agents.controller.package import CapturedContentPackage  # noqa: E402
from tests.doubles.content_package import content_package  # noqa: E402


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--aca-cold-start-samples",
        action="store",
        default=None,
        metavar="N",
        help="Run the manual deployed ACA cold-start qualification with 1..5 sequential samples.",
    )
    parser.addoption(
        "--aca-load-concurrency",
        action="store",
        default=None,
        metavar="N",
        help="Run the manual deployed ACA load qualification with N concurrent sessions (1..100).",
    )


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
