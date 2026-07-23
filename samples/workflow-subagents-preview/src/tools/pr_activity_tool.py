"""Synthetic pull-request activity tool."""

from __future__ import annotations

from typing import Any

from _fake_pr_data import ACTIVITIES, scenario_index as _scenario_index


def get_pull_request_activity(
    pull_request_url: str,
    last_checked_at: str | None = None,
) -> dict[str, Any]:
    """Return synthetic comments and commits added since the previous check."""
    activity = ACTIVITIES[_scenario_index(pull_request_url)]
    if last_checked_at is not None:
        activity = [item for item in activity if item["created_at"] > last_checked_at]
    return {
        "url": pull_request_url,
        "since": last_checked_at,
        "activity": activity,
    }
