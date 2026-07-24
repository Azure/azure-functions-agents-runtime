"""Shared deterministic data for the fake PR tools."""

from __future__ import annotations

import hashlib
from typing import Any

SCENARIOS: tuple[dict[str, Any], ...] = (
    {
        "title": "Add queue-triggered workflow support",
        "author": "octocat",
        "draft": False,
        "mergeable": True,
        "review_decision": "APPROVED",
        "checks": [
            {"name": "unit-tests", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ],
        "unresolved_threads": 0,
    },
    {
        "title": "Refactor workflow registry",
        "author": "hubot",
        "draft": False,
        "mergeable": True,
        "review_decision": "CHANGES_REQUESTED",
        "checks": [
            {"name": "unit-tests", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"},
        ],
        "unresolved_threads": 2,
    },
    {
        "title": "Document Sub Agent capabilities",
        "author": "monalisa",
        "draft": True,
        "mergeable": True,
        "review_decision": "REVIEW_REQUIRED",
        "checks": [
            {"name": "unit-tests", "status": "IN_PROGRESS", "conclusion": None},
            {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ],
        "unresolved_threads": 0,
    },
)

ACTIVITIES: tuple[list[dict[str, str]], ...] = (
    [
        {
            "type": "comment",
            "author": "reviewer-one",
            "created_at": "2026-07-23T18:15:00Z",
            "summary": "Confirmed the latest changes address the review feedback.",
        }
    ],
    [
        {
            "type": "review",
            "author": "reviewer-two",
            "created_at": "2026-07-23T19:30:00Z",
            "summary": "Requested changes to error handling and test coverage.",
        },
        {
            "type": "commit",
            "author": "hubot",
            "created_at": "2026-07-23T20:05:00Z",
            "summary": "Updated the registry implementation; lint remains failing.",
        },
    ],
    [],
)


def scenario_index(pull_request_url: str) -> int:
    if not pull_request_url.startswith("https://github.com/") or "/pull/" not in pull_request_url:
        raise ValueError("pull_request_url must be a GitHub pull-request URL")
    return hashlib.sha256(pull_request_url.encode("utf-8")).digest()[0] % len(SCENARIOS)
