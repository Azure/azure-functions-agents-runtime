from __future__ import annotations

import pytest

from azure_functions_agents import runner
from azure_functions_agents._session_id import validate_session_id


def test_validate_session_id_rejects_trailing_newline() -> None:
    with pytest.raises(ValueError, match="Invalid session_id"):
        validate_session_id("session-one\n")


def test_runner_reuses_shared_session_id_validator() -> None:
    assert runner._validate_session_id is validate_session_id