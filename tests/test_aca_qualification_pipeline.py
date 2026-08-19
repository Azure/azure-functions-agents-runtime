"""Tests for the post-main ACA qualification pipeline helpers.

``eng/`` is outside the CI lint and type-check paths (`ruff check src tests`,
`mypy src`), so these tests are the only automated coverage the pipeline logic
gets. They deliberately exercise the pure decision functions -- marker
construction, marker comparison, and stale-sandbox selection -- rather than the
network commands, because those are where a wrong answer would silently pass a
qualification or delete a live sandbox.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from eng.scripts.aca_qualification_pipeline import (
    QualificationPipelineError,
    build_marker,
    compare_marker,
    content_report,
    deploy_preflight_failure_message,
    parse_created_at,
    render_requirements,
    render_sweep_report,
    select_runtime_wheel,
    select_stale_sandboxes,
    stamp_marker,
)

_BUILD_ID = "12345"
_COMMIT = "2415708287d1ce719e8380208d0ba52a8df9c080"


def _marker_payload(**overrides: object) -> dict[str, object]:
    build: dict[str, object] = {
        "marker": "present",
        "schema": 1,
        "build_id": _BUILD_ID,
        "commit_sha": _COMMIT,
        "branch": "refs/heads/main",
        "runtime_version": "1.2.3",
    }
    build.update(overrides)
    return {
        "build": build,
        "runtime": {"python_version": "3.13", "python_micro": 2},
        "content": {"entry_count": 5968, "total_bytes": 79_600_000, "truncated": False},
    }


class TestBuildMarker:
    def test_marker_carries_build_identity(self) -> None:
        marker = build_marker(
            commit_sha=_COMMIT,
            build_id=_BUILD_ID,
            branch="refs/heads/main",
            runtime_version="1.2.3",
        )
        assert marker["schema"] == 1
        assert marker["build_id"] == _BUILD_ID
        assert marker["commit_sha"] == _COMMIT

    @pytest.mark.parametrize("field", ["commit_sha", "build_id", "branch", "runtime_version"])
    def test_empty_field_is_rejected(self, field: str) -> None:
        kwargs = {
            "commit_sha": _COMMIT,
            "build_id": _BUILD_ID,
            "branch": "refs/heads/main",
            "runtime_version": "1.2.3",
        }
        kwargs[field] = "   "
        with pytest.raises(QualificationPipelineError, match=f"marker_field_empty:{field}"):
            build_marker(**kwargs)  # type: ignore[arg-type]

    def test_stamp_writes_readable_json(self, tmp_path: Path) -> None:
        marker = build_marker(
            commit_sha=_COMMIT,
            build_id=_BUILD_ID,
            branch="refs/heads/main",
            runtime_version="1.2.3",
        )
        target = stamp_marker(tmp_path, marker)
        assert json.loads(target.read_text(encoding="utf-8")) == marker

    def test_stamp_rejects_missing_app_root(self, tmp_path: Path) -> None:
        with pytest.raises(QualificationPipelineError, match="app_root_missing"):
            stamp_marker(tmp_path / "absent", {"schema": 1})


class TestCompareMarker:
    def test_matching_build_passes(self) -> None:
        result = compare_marker(
            _marker_payload(),
            expected_build_id=_BUILD_ID,
            expected_commit_sha=_COMMIT,
            expected_python="3.13",
        )
        assert result.matches

    def test_stale_build_is_detected(self) -> None:
        result = compare_marker(
            _marker_payload(build_id="99999"),
            expected_build_id=_BUILD_ID,
            expected_commit_sha=_COMMIT,
            expected_python="3.13",
        )
        assert not result.matches
        assert "build_id" in result.mismatches

    def test_wrong_python_leg_is_detected(self) -> None:
        """The 3.14 package deployed onto the 3.13 app must not pass."""
        result = compare_marker(
            _marker_payload(),
            expected_build_id=_BUILD_ID,
            expected_commit_sha=_COMMIT,
            expected_python="3.14",
        )
        assert "python_version" in result.mismatches

    def test_absent_marker_never_passes(self) -> None:
        """An app with no marker must fail, not be treated as unverified-but-fine."""
        payload = _marker_payload()
        payload["build"] = {"marker": "absent"}
        result = compare_marker(
            payload,
            expected_build_id=_BUILD_ID,
            expected_commit_sha=_COMMIT,
            expected_python="3.13",
        )
        assert not result.matches
        assert result.mismatches == ("marker_absent",)

    def test_missing_build_section_never_passes(self) -> None:
        result = compare_marker(
            {"runtime": {"python_version": "3.13"}},
            expected_build_id=_BUILD_ID,
            expected_commit_sha=_COMMIT,
            expected_python="3.13",
        )
        assert result.mismatches == ("build_section_missing",)

    def test_mismatch_output_carries_no_values(self) -> None:
        """Field names only: this lands in a pipeline log."""
        result = compare_marker(
            _marker_payload(build_id="99999", commit_sha="deadbeef"),
            expected_build_id=_BUILD_ID,
            expected_commit_sha=_COMMIT,
            expected_python="3.13",
        )
        rendered = ",".join(result.mismatches)
        assert "99999" not in rendered
        assert "deadbeef" not in rendered


@dataclass(frozen=True)
class _Summary:
    sandbox_id: str
    created_at: str | None


class TestStaleSelection:
    def _now(self) -> datetime:
        return datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

    def test_old_sandboxes_are_selected(self) -> None:
        old = (self._now() - timedelta(hours=9)).isoformat()
        selection = select_stale_sandboxes([_Summary("old", old)], now=self._now())
        assert selection.stale_ids == ("old",)

    def test_recent_sandboxes_are_left_alone(self) -> None:
        """A concurrent run's sandbox must survive the sweep."""
        recent = (self._now() - timedelta(minutes=20)).isoformat()
        selection = select_stale_sandboxes([_Summary("live", recent)], now=self._now())
        assert selection.stale_ids == ()
        assert selection.live_count == 1

    def test_unknown_age_is_never_deleted(self) -> None:
        """Unprovable age must not authorize deletion, but must be reported."""
        selection = select_stale_sandboxes([_Summary("mystery", None)], now=self._now())
        assert selection.stale_ids == ()
        assert selection.unknown_age_ids == ("mystery",)

    def test_unparseable_timestamp_is_unknown_not_stale(self) -> None:
        selection = select_stale_sandboxes([_Summary("bad", "not-a-date")], now=self._now())
        assert selection.stale_ids == ()
        assert selection.unknown_age_ids == ("bad",)

    def test_boundary_is_not_stale(self) -> None:
        exactly = (self._now() - timedelta(hours=6)).isoformat()
        selection = select_stale_sandboxes([_Summary("edge", exactly)], now=self._now())
        assert selection.stale_ids == ()

    def test_naive_timestamp_is_treated_as_utc(self) -> None:
        naive = (self._now() - timedelta(hours=9)).replace(tzinfo=None).isoformat()
        selection = select_stale_sandboxes([_Summary("naive", naive)], now=self._now())
        assert selection.stale_ids == ("naive",)

    def test_zulu_suffix_parses(self) -> None:
        assert parse_created_at("2026-08-19T06:00:00Z") == datetime(2026, 8, 19, 6, 0, tzinfo=UTC)

    def test_report_shows_zero_when_clean(self) -> None:
        """A clean group must be distinguishable from a sweep that found nothing to look at."""
        selection = select_stale_sandboxes([], now=self._now())
        report = render_sweep_report(selection, deleted=0, max_age_hours=6)
        assert "stale=0" in report
        assert "deleted=0" in report


class TestContentReport:
    def test_reports_usage_against_caps(self) -> None:
        rendered = content_report(_marker_payload())
        assert "content_entries=5968" in rendered
        assert "75.9MiB" in rendered

    def test_missing_content_is_explicit(self) -> None:
        assert content_report({}) == "content=unavailable"


class TestPackageAssemblyHelpers:
    def test_select_runtime_wheel_returns_the_only_wheel_name(self) -> None:
        selected = select_runtime_wheel(["azure_functions_agents-1.0.0-py3-none-any.whl"])
        assert selected == "azure_functions_agents-1.0.0-py3-none-any.whl"

    def test_select_runtime_wheel_rejects_no_candidates(self) -> None:
        with pytest.raises(QualificationPipelineError, match="runtime_wheel_missing"):
            select_runtime_wheel([])

    def test_select_runtime_wheel_rejects_ambiguous_candidates(self) -> None:
        with pytest.raises(QualificationPipelineError, match="runtime_wheel_ambiguous"):
            select_runtime_wheel(["a.whl", "b.whl"])

    def test_render_requirements_installs_actual_wheel_and_pinned_deps(self, tmp_path: Path) -> None:
        constraints = tmp_path / "aca-fixture-py313.txt"
        constraints.write_text("azure-functions==1.23.0\npydantic==2.11.0\n", encoding="utf-8")
        rendered = render_requirements(
            wheel_filename="azure_functions_agents-1.2.3-py3-none-any.whl",
            constraints_path=constraints,
        )
        assert rendered.splitlines() == [
            "./azure_functions_agents-1.2.3-py3-none-any.whl",
            "",
            "azure-functions==1.23.0",
            "pydantic==2.11.0",
        ]

    def test_render_requirements_uses_template_placeholder_once(self, tmp_path: Path) -> None:
        constraints = tmp_path / "aca-fixture-py314.txt"
        constraints.write_text("azure-functions==1.24.0\n", encoding="utf-8")
        template = tmp_path / "requirements.txt"
        template.write_text("{{RUNTIME_WHEEL}}\n", encoding="utf-8")
        rendered = render_requirements(
            wheel_filename="runtime-1.0.0-py3-none-any.whl",
            constraints_path=constraints,
            template_path=template,
        )
        assert rendered.splitlines().count("./runtime-1.0.0-py3-none-any.whl") == 1
        assert "azure-functions==1.24.0" in rendered

    def test_render_requirements_replaces_generated_template_placeholder(self, tmp_path: Path) -> None:
        constraints = tmp_path / "aca-fixture-py313.txt"
        constraints.write_text("azure-functions==1.23.0\n", encoding="utf-8")
        template = tmp_path / "requirements.txt"
        template.write_text(
            "# generated template\n"
            "./azurefunctions_agents_runtime-BUILD_WHEEL_PLACEHOLDER-py3-none-any.whl\n"
            "# pins follow\n"
            "stale-pin==0.0.1\n",
            encoding="utf-8",
        )
        rendered = render_requirements(
            wheel_filename="azurefunctions_agents_runtime-1.0.0-py3-none-any.whl",
            constraints_path=constraints,
            template_path=template,
        )
        assert "BUILD_WHEEL_PLACEHOLDER" not in rendered
        assert "./azurefunctions_agents_runtime-1.0.0-py3-none-any.whl" in rendered
        assert "stale-pin==0.0.1" not in rendered
        assert "azure-functions==1.23.0" in rendered

    def test_render_requirements_rejects_missing_constraints_file(self, tmp_path: Path) -> None:
        with pytest.raises(QualificationPipelineError, match="constraints_file_missing"):
            render_requirements(
                wheel_filename="runtime-1.0.0-py3-none-any.whl",
                constraints_path=tmp_path / "missing.txt",
            )


class TestDeployPreflightHelpers:
    def test_preflight_failure_names_actionable_role(self) -> None:
        rendered = deploy_preflight_failure_message(
            app_name="aca-app",
            resource_group="rg",
            check_name="publishing_config_read",
        )
        assert "functionapp_deploy_preflight_failed:publishing_config_read" in rendered
        assert "grant Website Contributor on aca-app" in rendered
