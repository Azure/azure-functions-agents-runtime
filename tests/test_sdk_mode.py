"""Tests for SDK mode selection logic."""

from __future__ import annotations

import pytest

from azure_functions_agents.runner import get_sdk_mode, set_sdk_mode


def test_get_sdk_mode_default() -> None:
    """Default SDK mode should be 'maf'."""
    # Reset to default first
    set_sdk_mode("maf")
    assert get_sdk_mode() == "maf"


def test_set_sdk_mode_maf() -> None:
    """Setting SDK mode to 'maf' should work."""
    set_sdk_mode("maf")
    assert get_sdk_mode() == "maf"


def test_set_sdk_mode_copilot_sdk() -> None:
    """Setting SDK mode to 'copilot-sdk' should work."""
    set_sdk_mode("copilot-sdk")
    assert get_sdk_mode() == "copilot-sdk"
    # Reset to default
    set_sdk_mode("maf")


def test_set_sdk_mode_invalid() -> None:
    """Setting an invalid SDK mode should raise ValueError."""
    with pytest.raises(ValueError, match="Invalid SDK mode"):
        set_sdk_mode("invalid")  # type: ignore[arg-type]
