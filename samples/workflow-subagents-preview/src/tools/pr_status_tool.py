"""Synthetic pull-request status tool."""

from __future__ import annotations

from typing import Any

from _fake_pr_data import SCENARIOS, scenario_index as _scenario_index


def get_pull_request_status(pull_request_url: str) -> dict[str, Any]:
    """Return synthetic checks, reviews, and merge status for one pull request."""
    return {"url": pull_request_url, **SCENARIOS[_scenario_index(pull_request_url)]}
