from __future__ import annotations

import pytest

from azure_functions_agents.harness.bootstrap_report import (
    BootstrapErrorReport,
    BootstrapReportError,
    parse_bootstrap_error_report,
)


def test_bootstrap_report_round_trips_with_a_bounded_payload() -> None:
    report = BootstrapErrorReport.create(
        code="content_digest_mismatch",
        message="Sandbox content verification failed.",
        permanent=True,
    )

    assert parse_bootstrap_error_report(report.to_bytes()) == report


def test_bootstrap_report_rejects_duplicate_keys() -> None:
    with pytest.raises(BootstrapReportError):
        parse_bootstrap_error_report(
            b'{"code":"first","code":"second","message":"failure","permanent":true}'
        )
