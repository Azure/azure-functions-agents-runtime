from __future__ import annotations

import pytest

from azure_functions_agents import runner
from azure_functions_agents._agent_validation import validate_prompt, validate_session_id


@pytest.mark.parametrize("prompt", [None, "", "   "])
def test_validate_prompt_rejects_non_strings_and_blank_strings(prompt: object) -> None:
    with pytest.raises(ValueError, match="prompt must be a nonblank string"):
        validate_prompt(prompt)


def test_validate_prompt_preserves_valid_input() -> None:
    assert validate_prompt("  hello  ") == "  hello  "


def test_validate_session_id_rejects_trailing_newline() -> None:
    with pytest.raises(ValueError, match="Invalid session_id"):
        validate_session_id("session-one\n")


def test_runner_reuses_shared_session_id_validator() -> None:
    assert runner._validate_session_id is validate_session_id