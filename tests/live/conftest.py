"""Shared fixtures and quiet logging for the ACA live smoke."""

import logging
from pathlib import Path

import pytest
from tests.live.aca_smoke_support import materialize_current_checkout_app

logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)


@pytest.fixture(scope="session")
def aca_materialized_app_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the delivered Linux Python package once for the complete smoke session."""

    return materialize_current_checkout_app(tmp_path_factory.mktemp("aca-current-checkout"))
