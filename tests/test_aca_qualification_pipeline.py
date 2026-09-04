"""Tests for the deployed ACA qualification tooling.

``eng/`` is outside the CI lint and type-check paths (`ruff check src tests`,
`mypy src`), so these tests are the only automated coverage the qualification
tooling gets. They deliberately exercise the pure decision functions -- marker
construction, marker comparison, and package assembly -- rather than the network
commands, because those are where a wrong answer would silently attribute
qualification evidence to the wrong deployment.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import re
import subprocess
import tomllib
import zipfile
from argparse import Namespace
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from azure.identity import aio as azure_identity_aio
from eng.scripts import aca_deployed_qualification, aca_qualification_pipeline
from eng.scripts.aca_qualification_pipeline import (
    _DEDICATED_GROUP_SCOPE_ACKNOWLEDGMENT,
    _DEPLOY_CONFIGURATION_TIMEOUT_SECONDS,
    _DEPLOY_TIMEOUT_SECONDS,
    _NOT_READY_STATUSES,
    QualificationPipelineError,
    _parser,
    _redacted_reason,
    _run_az,
    _write_deployment_archive,
    assemble_upload_directory,
    build_marker,
    compare_marker,
    content_report,
    deploy_preflight_failure_message,
    fetch_build_info,
    parse_created_at,
    render_requirements,
    render_sweep_report,
    run_deploy,
    run_sweep,
    select_runtime_wheel,
    select_stale_sandboxes,
    stamp_marker,
    sweep_with_adapter,
)
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

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


@pytest.mark.parametrize("status", [500, 501, 505, 599])
def test_build_attestation_retries_every_server_error(status: int) -> None:
    assert status in _NOT_READY_STATUSES


@pytest.mark.parametrize("status", [400, 401, 403, 404, 600])
def test_build_attestation_does_not_retry_definitive_responses(status: int) -> None:
    assert status not in _NOT_READY_STATUSES


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


@pytest.mark.asyncio
async def test_build_info_retries_total_timeout_within_readiness_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _marker_payload()
    attempts = 0

    class FakeCredential:
        async def get_token(self, _: str) -> object:
            return type("Token", (), {"token": "credential-material"})()

        async def close(self) -> None:
            return None

    class FakeResponse:
        status = 200

        async def json(self, *, content_type: object) -> dict[str, object]:
            del content_type
            return payload

    class FakeRequest:
        async def __aenter__(self) -> FakeResponse:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TimeoutError
            return FakeResponse()

        async def __aexit__(self, *_: object) -> None:
            return None

    class FakeSession:
        def __init__(self, *, timeout: object) -> None:
            del timeout

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        def get(self, *_: object, **__: object) -> FakeRequest:
            return FakeRequest()

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(azure_identity_aio, "DefaultAzureCredential", FakeCredential)
    monkeypatch.setattr("aiohttp.ClientSession", FakeSession)
    monkeypatch.setattr(aca_qualification_pipeline.asyncio, "sleep", no_sleep)

    assert await fetch_build_info("https://example.test", "api://scope") == payload
    assert attempts == 2


@dataclass(frozen=True)
class _Summary:
    sandbox_id: str
    created_at: str | None


class TestStaleSelection:
    def _now(self) -> datetime:
        return datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

    def test_only_resources_strictly_older_than_six_hours_are_selected(self) -> None:
        old = (self._now() - timedelta(hours=6, seconds=1)).isoformat()
        boundary = (self._now() - timedelta(hours=6)).isoformat()
        recent = (self._now() - timedelta(minutes=20)).isoformat()

        selection = select_stale_sandboxes(
            [
                _Summary("old", old),
                _Summary("boundary", boundary),
                _Summary("recent", recent),
            ],
            now=self._now(),
        )

        assert selection.stale_ids == ("old",)
        assert selection.recent_count == 2

    @pytest.mark.parametrize("created_at", [None, "", "not-a-date", "2026-08-19"])
    def test_unknown_age_is_never_selected(self, created_at: str | None) -> None:
        selection = select_stale_sandboxes(
            [_Summary("unknown", created_at)],
            now=self._now(),
        )

        assert selection.stale_ids == ()
        assert selection.unknown_age_ids == ("unknown",)

    def test_naive_and_zulu_timestamps_are_treated_as_utc(self) -> None:
        assert parse_created_at("2026-08-19T06:00:00Z") == datetime(
            2026, 8, 19, 6, 0, tzinfo=UTC
        )
        naive = (self._now() - timedelta(hours=7)).replace(tzinfo=None).isoformat()
        assert select_stale_sandboxes(
            [_Summary("naive", naive)],
            now=self._now(),
        ).stale_ids == ("naive",)


class TestSweepExecution:
    def test_helper_rejects_unacknowledged_group_before_inspection(self) -> None:
        seen: list[str] = []

        class _Adapter:
            async def list_sandboxes(self, *, labels: dict[str, str]) -> tuple[object, ...]:
                del labels
                seen.append("list")
                return ()

            async def delete_sandbox(self, sandbox_id: str) -> None:
                del sandbox_id
                seen.append("delete")

            async def close(self) -> None:
                seen.append("close")

        with pytest.raises(
            QualificationPipelineError,
            match="sweep_dedicated_group_acknowledgment_required",
        ):
            asyncio.run(
                sweep_with_adapter(
                    _Adapter(),
                    now=datetime(2026, 8, 19, 12, 0, tzinfo=UTC),
                    dedicated_group_scope="shared-group",
                )
            )

        assert seen == ["close"]

    @pytest.mark.parametrize(
        "arguments",
        [
            ["sweep", "--region", "eastus"],
            [
                "sweep",
                "--region",
                "eastus",
                "--dedicated-group-scope",
                "shared-group",
            ],
        ],
    )
    def test_cli_requires_exact_dedicated_group_acknowledgment(
        self,
        arguments: list[str],
    ) -> None:
        with pytest.raises(SystemExit):
            _parser().parse_args(arguments)

    def test_list_failure_warns_and_cannot_render_as_clean(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        class _Adapter:
            async def list_sandboxes(self, *, labels: dict[str, str]) -> tuple[object, ...]:
                assert labels == {}
                raise RuntimeError("failed?sig=secret-value")

            async def close(self) -> None:
                return None

        outcome = asyncio.run(
            sweep_with_adapter(
                _Adapter(),
                now=datetime(2026, 8, 19, 12, 0, tzinfo=UTC),
                dedicated_group_scope=_DEDICATED_GROUP_SCOPE_ACKNOWLEDGMENT,
            )
        )
        report = render_sweep_report(outcome)
        output = capsys.readouterr().out

        assert "##vso[task.logissue type=warning]" in output
        assert "secret-value" not in output
        assert "stale=unavailable" in report
        assert "inspection_failures=1" in report
        assert "incomplete=1" in report

    def test_unknown_age_and_delete_failures_emit_durable_warnings(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        now = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
        old = (now - timedelta(hours=7)).isoformat()
        deleted: list[str] = []

        class _Adapter:
            async def list_sandboxes(
                self,
                *,
                labels: dict[str, str],
            ) -> tuple[_Summary, ...]:
                assert labels == {}
                return (
                    _Summary("delete-ok", old),
                    _Summary("delete-fails", old),
                    _Summary("unknown", None),
                    _Summary("recent", now.isoformat()),
                )

            async def delete_sandbox(self, sandbox_id: str) -> None:
                if sandbox_id == "delete-fails":
                    raise RuntimeError("failed?sig=secret-value")
                deleted.append(sandbox_id)

            async def close(self) -> None:
                return None

        outcome = asyncio.run(
            sweep_with_adapter(
                _Adapter(),
                now=now,
                dedicated_group_scope=_DEDICATED_GROUP_SCOPE_ACKNOWLEDGMENT,
            )
        )
        report = render_sweep_report(outcome)
        output = capsys.readouterr().out

        assert deleted == ["delete-ok"]
        assert output.count("##vso[task.logissue type=warning]") == 2
        assert "secret-value" not in output
        assert "resource_ref=sha256:" in output
        assert "stale=2" in report
        assert "deleted=1" in report
        assert "unknown_age=1" in report
        assert "recent=1" in report
        assert "delete_failures=1" in report
        assert "incomplete=2" in report

    def test_delete_not_found_counts_as_already_absent_without_warning(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from azure_functions_agents.transport.transport_models import (
            SandboxNotFoundError,
        )

        now = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

        class _Adapter:
            async def list_sandboxes(
                self,
                *,
                labels: dict[str, str],
            ) -> tuple[_Summary, ...]:
                assert labels == {}
                return (_Summary("already-gone", (now - timedelta(hours=7)).isoformat()),)

            async def delete_sandbox(self, sandbox_id: str) -> None:
                assert sandbox_id == "already-gone"
                raise SandboxNotFoundError("Session backing sandbox was not found.")

            async def close(self) -> None:
                return None

        outcome = asyncio.run(
            sweep_with_adapter(
                _Adapter(),
                now=now,
                dedicated_group_scope=_DEDICATED_GROUP_SCOPE_ACKNOWLEDGMENT,
            )
        )
        report = render_sweep_report(outcome)

        assert capsys.readouterr().out == ""
        assert "stale=1" in report
        assert "deleted=0" in report
        assert "already_absent=1" in report
        assert "delete_failures=0" in report
        assert "incomplete=0" in report

    def test_run_sweep_does_not_warn_when_all_stale_resources_are_already_absent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        async def already_absent(*args: object, **kwargs: object) -> object:
            del args, kwargs
            return aca_qualification_pipeline.SweepOutcome(
                selection=aca_qualification_pipeline.SweepSelection(
                    stale_ids=("already-gone",),
                    unknown_age_ids=(),
                    recent_count=0,
                ),
                deleted_count=0,
                already_absent_count=1,
                delete_failure_count=0,
                inspection_failure_count=0,
            )

        monkeypatch.setattr(aca_qualification_pipeline, "_sweep", already_absent)

        result = run_sweep(
            {
                "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID": (
                    "/subscriptions/example/resourceGroups/example/providers/"
                    "Microsoft.App/sessionPools/example"
                )
            },
            region="eastus",
            dedicated_group_scope=_DEDICATED_GROUP_SCOPE_ACKNOWLEDGMENT,
        )
        output = capsys.readouterr().out

        assert result == 0
        assert "##vso[task.logissue type=warning]" not in output
        assert "stale=1" in output
        assert "already_absent=1" in output
        assert "incomplete=0" in output

    def test_adapter_open_failure_is_warning_only_and_explicitly_incomplete(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        async def fail_open(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise RuntimeError("failed?sig=secret-value")

        monkeypatch.setattr(aca_qualification_pipeline, "_sweep", fail_open)
        result = run_sweep(
            {
                "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID": (
                    "/subscriptions/example/resourceGroups/example/providers/"
                    "Microsoft.App/sessionPools/example"
                )
            },
            region="eastus",
            dedicated_group_scope=_DEDICATED_GROUP_SCOPE_ACKNOWLEDGMENT,
        )
        output = capsys.readouterr().out

        assert result == 0
        assert "##vso[task.logissue type=warning]" in output
        assert "secret-value" not in output
        assert "stale=unavailable" in output
        assert "incomplete=1" in output


class TestContentReport:
    def test_reports_usage_against_caps(self) -> None:
        rendered = content_report(_marker_payload())
        assert "content_entries=5968" in rendered
        assert "75.9MiB" in rendered

    def test_missing_content_is_explicit(self) -> None:
        assert content_report({}) == "content=unavailable"


class TestQualificationEnvironmentContract:
    """Validate what the qualification tooling authors against the enforced bounds.

    The fixture and the deploy command must agree with the live-test support
    module, which validates its environment at fixture time. Nothing else
    compares the two, so a drift here would surface only as an
    ``AcaSmokeEnvironmentError`` during a paid deployed run.
    """

    def test_fixture_authors_the_required_region(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (
            root
            / "tests"
            / "live"
            / "apps"
            / "aca-qualification"
            / "agents.config.yaml"
        ).read_text(encoding="utf-8")
        assert "region: $AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_REGION" in source

    def test_deploy_configures_the_region_app_setting(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "eng" / "scripts" / "aca_qualification_pipeline.py").read_text(
            encoding="utf-8"
        )
        assert "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_REGION={region}" in script

    def test_the_deployed_timeout_bound_is_the_platform_ceiling(self) -> None:
        """Fail if the support module's bound moves away from the platform limit.

        230 is not arbitrary: it is the Azure Functions platform HTTP request
        ceiling, so any larger authored timeout is unreachable regardless of
        what the client asks for.
        """
        source = (
            Path(__file__).resolve().parents[1]
            / "tests"
            / "live"
            / "aca_deployed_agent_support.py"
        ).read_text(encoding="utf-8")
        assert "1 <= timeout <= 230" in source, (
            "The enforced timeout bound changed; re-check the deployed timeout "
            "any qualification run passes."
        )


class TestQualificationPipelineWiring:
    def _root(self) -> Path:
        return Path(__file__).resolve().parents[1]

    def _pipeline(self) -> str:
        return (self._root() / "eng" / "ci" / "e2e-tests.yml").read_text(encoding="utf-8")

    def _template(self) -> str:
        return (
            self._root() / "eng" / "templates" / "official" / "jobs" / "aca-qualify.yml"
        ).read_text(encoding="utf-8")

    def _sweep_template(self) -> str:
        return (
            self._root() / "eng" / "templates" / "official" / "jobs" / "aca-sweep.yml"
        ).read_text(encoding="utf-8")

    def _stage(self, name: str) -> str:
        pipeline = self._pipeline()
        marker = f"      - stage: {name}"
        assert pipeline.count(marker) == 1
        tail = pipeline.split(marker, 1)[1]
        return tail.split("\n      - stage:", 1)[0]

    def test_sweep_and_qualification_stages_are_present(self) -> None:
        pipeline = self._pipeline()
        assert pipeline.count("- stage: AcaQualification") == 1
        assert pipeline.count("- stage: AcaSweep") == 1
        assert "dependsOn: []" in self._stage("AcaSweep")

    def test_qualification_waits_for_build_and_nonblocking_sweep(self) -> None:
        stage = self._stage("AcaQualification")
        assert "dependsOn:\n          - Build\n          - AcaSweep" in stage
        assert "in(dependencies.Build.result, 'Succeeded', 'SucceededWithIssues')" in stage
        assert "succeeded()" not in stage

    @pytest.mark.parametrize("stage_name", ["AcaSweep", "AcaQualification"])
    def test_conditions_allow_manual_or_main_ci_only(self, stage_name: str) -> None:
        condition = next(
            line.strip()
            for line in self._stage(stage_name).splitlines()
            if line.strip().startswith("condition:")
        )
        assert "'Manual'" in condition
        assert "'IndividualCI', 'BatchedCI'" in condition
        assert "'refs/heads/main'" in condition
        assert "PullRequest" not in condition
        assert "Schedule" not in condition

    def test_matrix_runs_both_python_versions_in_parallel(self) -> None:
        template = self._template()
        assert "maxParallel: 2" in template
        assert template.count("runtimeTarget: 'python313'") == 1
        assert template.count("runtimeTarget: 'python314'") == 1
        assert template.count("pythonVersion: '3.13'") == 1
        assert template.count("pythonVersion: '3.14'") == 1
        assert "constraintsFile:" not in template
        assert "--constraints-file" not in template
        assert (
            template.count(
                "--requirements-export eng/constraints/aca-fixture-requirements.txt"
            )
            == 1
        )

    def test_each_leg_runs_the_combined_suite_with_provisioning_concurrency_one(
        self,
    ) -> None:
        template = self._template()
        assert template.count(" deployed-suite ") == 1
        assert template.count("--load-concurrency 5") == 1
        assert template.count("--provision-concurrency 1") == 1
        assert "continueOnError: true" in template

    def test_aca_settings_are_basic_variables_not_variable_groups(self) -> None:
        source = self._pipeline() + "\n" + self._template() + "\n" + self._sweep_template()
        assert not re.search(r"(?m)^\s*-\s*group\s*:", source)
        for name in (
            "ACA_DEPLOYED_APP_SUBSCRIPTION_ID",
            "ACA_DEPLOYED_RESOURCE_GROUP",
            "ACA_DEPLOYED_APP_SITE_NAME_PY313",
            "ACA_DEPLOYED_APP_SITE_NAME_PY314",
            "ACA_DEPLOYED_FUNCTION_BASE_URL_PY313",
            "ACA_DEPLOYED_FUNCTION_BASE_URL_PY314",
            "ACA_DEPLOYED_AGENT_SLUG",
            "ACA_DEPLOYED_EASY_AUTH_TOKEN_SCOPE",
            "ACA_DEPLOYED_EASY_AUTH_AUDIENCE",
            "ACA_DEPLOYED_TABLE_SERVICE_URI",
            "ACA_DEPLOYED_TABLE_NAME",
            "ACA_SANDBOX_GROUP_RESOURCE_ID",
            "ACA_SANDBOX_REGION",
        ):
            assert f"$({name})" in source

    def test_sweep_acknowledges_the_external_dedicated_group_invariant(self) -> None:
        template = self._sweep_template()
        assert (
            template.count(
                "--dedicated-group-scope exclusive-ci-qualification"
            )
            == 1
        )
        assert "continueOnError: true" in template
        assert '--region "$(ACA_SANDBOX_REGION)"' in template

    def test_sweep_is_pre_run_only(self) -> None:
        assert " aca_qualification_pipeline.py sweep " not in self._template()
        assert self._sweep_template().count(
            "aca_qualification_pipeline.py sweep"
        ) == 1
        assert "always()" not in self._sweep_template()

    def test_template_keeps_lightweight_marker_attestation(self) -> None:
        template = self._template()
        for name in (
            "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EXPECTED_BUILD_ID",
            "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EXPECTED_COMMIT_SHA",
            "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EXPECTED_PYTHON_VERSION",
        ):
            assert f"{name}:" in template
        assert "wheel digest" not in template.lower()
        assert "deployment storage" not in template.lower()


class TestCombinedDeployedSuite:
    def test_n100_is_rejected_before_environment_or_auth_preflight(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        auth_called = False

        def unexpected_auth(_: object) -> None:
            nonlocal auth_called
            auth_called = True

        monkeypatch.setattr(aca_deployed_qualification, "preflight_auth", unexpected_auth)

        with pytest.raises(
            aca_deployed_qualification.QualificationError,
            match=aca_deployed_qualification.FORMAL_N100_UNSUPPORTED_ERROR,
        ):
            aca_deployed_qualification.run_deployed_suite(
                {},
                runtime_target="python313",
                load_concurrency="100",
                provision_concurrency="4",
            )

        assert not auth_called

    def test_n5_remains_an_accepted_diagnostic(self) -> None:
        environment = {
            name: "configured"
            for name in aca_deployed_qualification._DEPLOYED_ENVIRONMENT
        }

        assert aca_deployed_qualification.validate_deployed_environment(
            environment,
            runtime_target="python313",
            load_concurrency="5",
            provision_concurrency="1",
        ) == (5, 1)

    def test_n100_command_failure_is_stable_and_redacted(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        result = aca_deployed_qualification.main(
            [
                "deployed-suite",
                "--runtime-target",
                "python313",
                "--load-concurrency",
                "100",
                "--provision-concurrency",
                "4",
            ],
            environment={},
        )

        assert result == 1
        assert capsys.readouterr().err.strip() == (
            "ACA qualification failed: "
            f"{aca_deployed_qualification.FORMAL_N100_UNSUPPORTED_ERROR}"
        )

    def test_cold_start_gates_the_remaining_modules(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[tuple[tuple[str, ...], bool]] = []
        monkeypatch.setattr(
            aca_deployed_qualification,
            "validate_deployed_environment",
            lambda *args, **kwargs: (5, 1),
        )
        monkeypatch.setattr(aca_deployed_qualification, "preflight_auth", lambda _: None)
        monkeypatch.setattr(
            aca_deployed_qualification,
            "_run_pytest",
            lambda paths, _, *, fail_fast=False: captured.append(
                (tuple(paths), fail_fast)
            )
            or 0,
        )

        result = aca_deployed_qualification.run_deployed_suite(
            {},
            runtime_target="python313",
            load_concurrency="5",
            provision_concurrency="1",
        )

        assert result == 0
        assert captured == [
            (
                ("tests/live/test_aca_deployed_cold_start.py",),
                True,
            ),
            (
                (
                    "tests/live/test_aca_deployed_agent_turn.py",
                    "tests/live/test_aca_deployed_lifecycle.py",
                    "tests/live/test_aca_deployed_loss.py",
                    "tests/live/test_aca_deployed_load.py",
                ),
                True,
            ),
        ]

    def test_run_deployed_suite_pytest_command_enables_fail_fast(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[list[str]] = []

        def capture_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            captured.append(command)
            return subprocess.CompletedProcess(command, 0)

        monkeypatch.setattr(aca_deployed_qualification.subprocess, "run", capture_run)

        assert (
            aca_deployed_qualification._run_pytest(
                ("cold.py", "later.py"),
                {},
                fail_fast=True,
            )
            == 0
        )
        assert captured[0].index("-x") < captured[0].index("cold.py")
        assert captured[0].index("cold.py") < captured[0].index("later.py")

    def test_cold_start_failure_suppresses_later_suites(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[tuple[tuple[str, ...], bool]] = []
        monkeypatch.setattr(
            aca_deployed_qualification,
            "validate_deployed_environment",
            lambda *args, **kwargs: (5, 1),
        )
        monkeypatch.setattr(aca_deployed_qualification, "preflight_auth", lambda _: None)

        def fail_cold_start(
            paths: tuple[str, ...],
            _: object,
            *,
            fail_fast: bool = False,
        ) -> int:
            captured.append((paths, fail_fast))
            return 1

        monkeypatch.setattr(aca_deployed_qualification, "_run_pytest", fail_cold_start)

        assert (
            aca_deployed_qualification.run_deployed_suite(
                {},
                runtime_target="python313",
                load_concurrency="5",
                provision_concurrency="1",
            )
            == 1
        )
        assert captured == [(("tests/live/test_aca_deployed_cold_start.py",), True)]

    def test_the_expected_identity_environment_is_required(self) -> None:
        """Every provenance input must be required before a deployed run starts."""
        for name in (
            "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EXPECTED_BUILD_ID",
            "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EXPECTED_COMMIT_SHA",
            "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EXPECTED_PYTHON_VERSION",
        ):
            assert name in aca_deployed_qualification._DEPLOYED_ENVIRONMENT


class TestDeploymentCommand:
    def test_azure_cli_failure_diagnostics_are_omitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def failed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            del args, kwargs
            return subprocess.CompletedProcess(
                args=["az"],
                returncode=1,
                stdout="",
                stderr="request failed with Bearer secret-token-value",
            )

        monkeypatch.setattr(subprocess, "run", failed)

        with pytest.raises(QualificationPipelineError) as caught:
            _run_az(["functionapp", "show"])

        assert "secret-token-value" not in str(caught.value)
        assert str(caught.value) == "az_failed:functionapp:show:exit_1"

    def test_azure_cli_timeout_is_a_typed_redacted_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def timed_out(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise subprocess.TimeoutExpired("az", 30)

        monkeypatch.setattr(subprocess, "run", timed_out)

        with pytest.raises(
            QualificationPipelineError,
            match=r"az_timeout:functionapp:show:30s",
        ):
            _run_az(["functionapp", "show"])

    def test_archive_contains_staged_files_relative_to_its_root(self, tmp_path: Path) -> None:
        staging = tmp_path / "staging"
        (staging / "nested").mkdir(parents=True)
        (staging / "host.json").write_text("{}", encoding="utf-8")
        (staging / "nested" / "file.txt").write_text("content", encoding="utf-8")
        archive = tmp_path / "fixture.zip"

        _write_deployment_archive(staging, archive)

        with zipfile.ZipFile(archive) as package:
            assert package.namelist() == ["host.json", "nested/file.txt"]

    def test_deploy_combines_preflight_region_upload_and_best_effort_tag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "host.json").write_text("{}", encoding="utf-8")
        archive = tmp_path / "fixture.zip"
        commands: list[tuple[list[str], float]] = []

        def fake_run_az(args: list[str], *, timeout_seconds: float = 30.0) -> None:
            commands.append((args, timeout_seconds))

        monkeypatch.setattr(
            "eng.scripts.aca_qualification_pipeline._run_az",
            fake_run_az,
        )

        result = run_deploy(
            Namespace(
                staging_root=str(staging),
                archive_path=str(archive),
                app_name="qualification-app",
                resource_group="qualification-rg",
                region="westus3",
                build_id="12345",
                commit_sha=_COMMIT,
            )
        )

        assert result == 0
        assert [command[:3] for command, _ in commands] == [
            ["functionapp", "show", "--name"],
            ["functionapp", "config", "appsettings"],
            ["functionapp", "deployment", "source"],
            ["tag", "update", "--operation"],
        ]
        assert [timeout for _, timeout in commands] == [
            30.0,
            _DEPLOY_CONFIGURATION_TIMEOUT_SECONDS,
            _DEPLOY_TIMEOUT_SECONDS,
            30.0,
        ]
        assert archive.is_file()

    def test_deploy_continues_when_redacted_metadata_tag_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "host.json").write_text("{}", encoding="utf-8")

        def fake_run_az(args: list[str], *, timeout_seconds: float = 30.0) -> None:
            del timeout_seconds
            if args[:2] == ["tag", "update"]:
                raise QualificationPipelineError(
                    "tag_failed: Bearer credential-material"
                )

        monkeypatch.setattr(
            "eng.scripts.aca_qualification_pipeline._run_az",
            fake_run_az,
        )

        result = run_deploy(
            Namespace(
                staging_root=str(staging),
                archive_path=str(tmp_path / "fixture.zip"),
                app_name="qualification-app",
                resource_group="qualification-rg",
                region="westus3",
                build_id="12345",
                commit_sha=_COMMIT,
            )
        )

        output = capsys.readouterr().out
        assert result == 0
        assert "warning: Function App metadata tag failed" in output
        assert "credential-material" not in output
        assert "<redacted>" in output


class TestFixtureRouteBinding:
    """Guard the fixture's HTTP types against the worker's binding validation.

    The route originally annotated ``azure.functions.HttpRequest``. The runtime
    registers every route with the FastAPI ``Request``/response types instead,
    and the worker rejects the mismatch at indexing time. Indexing is
    all-or-nothing, so that single bad function took down the whole deployed
    app -- every agent route included -- surfacing only as the generic
    "No job functions found".

    The fixture cannot be imported here (it requires a Linux host, and fails
    closed on Windows by design), so this reads the source instead.
    """

    def _build_info_signature(self) -> tuple[str, str]:
        source = (
            Path(__file__).resolve().parents[1]
            / "tests"
            / "live"
            / "apps"
            / "aca-qualification"
            / "function_app.py"
        )
        if not source.is_file():
            source = (
                Path(__file__).resolve().parent
                / "live"
                / "apps"
                / "aca-qualification"
                / "function_app.py"
            )
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "build_info":
                annotation = node.args.args[0].annotation
                returns = node.returns
                return (ast.unparse(annotation), ast.unparse(returns))
        raise AssertionError("build_info not found in the fixture app.")

    def test_uses_the_fastapi_request_type(self) -> None:
        parameter, _ = self._build_info_signature()
        assert parameter == "Request", (
            "The worker rejects azure.functions.HttpRequest here and refuses to "
            "index the entire app."
        )

    def test_returns_a_fastapi_response(self) -> None:
        _, returns = self._build_info_signature()
        assert "Response" in returns
        assert "HttpResponse" not in returns

    def test_registers_the_expected_binding_shape(self) -> None:
        """The corrected annotation must produce httpTrigger + http $return."""
        import azure.functions as func
        from azurefunctions.extensions.http.fastapi import JSONResponse, Request

        probe = func.FunctionApp()

        @probe.route(route="__buildinfo", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
        def build_info(req: Request) -> JSONResponse:  # pragma: no cover - shape probe
            return JSONResponse(content={}, status_code=200)

        bindings = {
            (binding.type, binding.name)
            for function in probe.get_functions()
            for binding in function.get_bindings()
        }
        assert ("httpTrigger", "req") in bindings
        assert ("http", "$return") in bindings


class TestDeployedAcaQualificationFixtureContract:
    """Keep the deployed fixture aligned with the live suites that invoke it."""

    _LIVE_SUITE_NAMES = (
        "test_aca_deployed_agent_turn.py",
        "test_aca_deployed_cold_start.py",
        "test_aca_deployed_lifecycle.py",
        "test_aca_deployed_load.py",
        "test_aca_deployed_loss.py",
    )
    _CALL_EXACTLY_ONCE = re.compile(r"\bCall\s+([a-z][a-z0-9_]*)\s+exactly once\b")

    def _repo_root(self) -> Path:
        return Path(__file__).resolve().parents[1]

    def _fixture_root(self) -> Path:
        return self._repo_root() / "tests" / "live" / "apps" / "aca-qualification"

    def _live_suite_paths(self) -> tuple[Path, ...]:
        live_root = self._repo_root() / "tests" / "live"
        return tuple(live_root / name for name in self._LIVE_SUITE_NAMES)

    def _suite_agent_slugs(self) -> dict[str, set[str]]:
        slugs: dict[str, set[str]] = {}
        for path in self._live_suite_paths():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
                    continue
                names = [target.id for target in node.targets if isinstance(target, ast.Name)]
                if any(name.endswith("_AGENT_SLUG") for name in names):
                    slugs.setdefault(path.name, set()).add(node.value.value)
        return slugs

    def _suite_tool_names(self) -> dict[str, set[str]]:
        tools: dict[str, set[str]] = {}
        for path in self._live_suite_paths():
            source = path.read_text(encoding="utf-8")
            names = set(self._CALL_EXACTLY_ONCE.findall(source))
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Compare):
                    continue
                compared = [node.left, *node.comparators]
                constants = {
                    item.value
                    for item in compared
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                }
                if not constants:
                    continue
                calls = [item for item in compared if isinstance(item, ast.Call)]
                if any(self._is_tool_name_get(call) for call in calls):
                    names.update(constants)
            if names:
                tools[path.name] = names
        return tools

    @staticmethod
    def _is_tool_name_get(call: ast.Call) -> bool:
        return (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "get"
            and bool(call.args)
            and isinstance(call.args[0], ast.Constant)
            and call.args[0].value == "tool_name"
        )

    def _fixture_agent_slugs(self) -> set[str]:
        return {
            path.name.removesuffix(".agent.md")
            for path in self._fixture_root().glob("*.agent.md")
            if path.is_file()
        }

    def _fixture_tool_names(self) -> set[str]:
        tool_names: set[str] = set()
        tools_root = self._fixture_root() / "tools"
        for path in tools_root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and not node.name.startswith(
                    "_"
                ):
                    tool_names.add(node.name)
        return tool_names

    def test_live_suite_agent_slugs_exist_in_the_deployed_fixture(self) -> None:
        required = {
            slug
            for slugs in self._suite_agent_slugs().values()
            for slug in slugs
            if slug.startswith("deployed_")
        }
        assert required, "No deployed agent slugs were derived from the live suites."

        missing = required - self._fixture_agent_slugs()
        assert not missing, f"Missing deployed fixture agent(s): {sorted(missing)}"

    def test_live_suite_tool_names_exist_in_the_deployed_fixture(self) -> None:
        required = {
            tool
            for tools in self._suite_tool_names().values()
            for tool in tools
            if tool.startswith("qualification_")
        }
        assert required, "No qualification tool names were derived from the live suites."

        missing = required - self._fixture_tool_names()
        assert not missing, f"Missing deployed fixture tool(s): {sorted(missing)}"


class TestRedactedReason:
    """A reason must be diagnosable without carrying credential material."""

    def test_keeps_the_operational_cause(self) -> None:
        reason = _redacted_reason(OSError("Cannot connect to host aca.example: [Errno -2]"))
        assert "Cannot connect to host" in reason

    def test_strips_bearer_tokens_and_sas_signatures(self) -> None:
        reason = _redacted_reason(
            RuntimeError("failed https://x/y?sv=2024-01-01&sig=AbCdEf123 Bearer eyJhbGciOiJIUzI1NiJ9abcdefghijk")
        )
        assert "AbCdEf123" not in reason
        assert "eyJhbGciOiJIUzI1NiJ9" not in reason
        assert "<redacted>" in reason

    def test_falls_back_to_type_when_empty(self) -> None:
        assert _redacted_reason(ValueError()) == "ValueError"

    def test_truncates_runaway_messages(self) -> None:
        assert len(_redacted_reason(RuntimeError("x" * 5000))) < 500


class TestAdapterRegionContract:
    """Bind the qualification assets to the real adapter signature.

    The deployed suites and the qualification fixture both depend on the
    authored Sandbox Group region being required, keyword-only, and free of ARM
    discovery. Copying the real signature here makes a future drift fail during
    the normal gate instead of in a paid deployed run.
    """

    def test_real_adapter_requires_an_authored_region(self) -> None:
        from azure_functions_agents.transport.aca_sdk import AcaSandboxAdapter

        parameters = inspect.signature(AcaSandboxAdapter.open).parameters
        region = parameters["region"]
        assert region.kind is inspect.Parameter.KEYWORD_ONLY
        assert region.default is inspect.Parameter.empty


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
        requirements_export = tmp_path / "aca-fixture-requirements.txt"
        requirements_export.write_text(
            "azure-functions==1.23.0\npydantic==2.11.0\n",
            encoding="utf-8",
        )
        rendered = render_requirements(
            wheel_filename="azure_functions_agents-1.2.3-py3-none-any.whl",
            requirements_export_path=requirements_export,
        )
        assert rendered.splitlines() == [
            "./azure_functions_agents-1.2.3-py3-none-any.whl",
            "",
            "azure-functions==1.23.0",
            "pydantic==2.11.0",
        ]

    def test_render_requirements_uses_template_placeholder_once(self, tmp_path: Path) -> None:
        requirements_export = tmp_path / "aca-fixture-requirements.txt"
        requirements_export.write_text("azure-functions==1.24.0\n", encoding="utf-8")
        template = tmp_path / "requirements.txt"
        template.write_text("{{RUNTIME_WHEEL}}\n", encoding="utf-8")
        rendered = render_requirements(
            wheel_filename="runtime-1.0.0-py3-none-any.whl",
            requirements_export_path=requirements_export,
            template_path=template,
        )
        assert rendered.splitlines().count("./runtime-1.0.0-py3-none-any.whl") == 1
        assert "azure-functions==1.24.0" in rendered

    def test_render_requirements_replaces_generated_template_placeholder(self, tmp_path: Path) -> None:
        requirements_export = tmp_path / "aca-fixture-requirements.txt"
        requirements_export.write_text("azure-functions==1.23.0\n", encoding="utf-8")
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
            requirements_export_path=requirements_export,
            template_path=template,
        )
        assert "BUILD_WHEEL_PLACEHOLDER" not in rendered
        assert "./azurefunctions_agents_runtime-1.0.0-py3-none-any.whl" in rendered
        assert "stale-pin==0.0.1" not in rendered
        assert "azure-functions==1.23.0" in rendered

    def test_render_requirements_rejects_missing_export(self, tmp_path: Path) -> None:
        with pytest.raises(QualificationPipelineError, match="requirements_export_missing"):
            render_requirements(
                wheel_filename="runtime-1.0.0-py3-none-any.whl",
                requirements_export_path=tmp_path / "missing.txt",
            )

    def test_assemble_upload_materializes_fixture_wheel_marker_and_requirements(
        self,
        tmp_path: Path,
    ) -> None:
        artifact_root = tmp_path / "artifact"
        dist_root = artifact_root / "dist"
        dist_root.mkdir(parents=True)
        wheel = dist_root / "azurefunctions_agents_runtime-1.0.0-py3-none-any.whl"
        wheel.write_bytes(b"wheel")
        fixture_root = tmp_path / "fixture"
        fixture_root.mkdir()
        (fixture_root / "host.json").write_text("{}", encoding="utf-8")
        (fixture_root / "requirements.txt").write_text(
            "{{RUNTIME_WHEEL}}\n",
            encoding="utf-8",
        )
        ignored = fixture_root / "__pycache__"
        ignored.mkdir()
        (ignored / "fixture.pyc").write_bytes(b"ignored")
        requirements_export = tmp_path / "aca-fixture-requirements.txt"
        requirements_export.write_text("azure-functions==1.24.0\n", encoding="utf-8")
        staging_root = tmp_path / "staging"

        result = assemble_upload_directory(
            Namespace(
                artifact_root=str(artifact_root),
                fixture_root=str(fixture_root),
                staging_root=str(staging_root),
                requirements_template=None,
                requirements_export=str(requirements_export),
                commit_sha=_COMMIT,
                build_id=_BUILD_ID,
                branch="refs/heads/main",
                runtime_version="1.0.0",
            )
        )

        assert result == staging_root
        assert (staging_root / wheel.name).read_bytes() == b"wheel"
        assert json.loads(
            (staging_root / "BUILD_INFO.json").read_text(encoding="utf-8")
        ) == build_marker(
            commit_sha=_COMMIT,
            build_id=_BUILD_ID,
            branch="refs/heads/main",
            runtime_version="1.0.0",
        )
        assert (staging_root / "requirements.txt").read_text(
            encoding="utf-8"
        ).splitlines() == [
            f"./{wheel.name}",
            "",
            "azure-functions==1.24.0",
        ]
        assert not (staging_root / "__pycache__").exists()


class TestDeployPreflightHelpers:
    def test_preflight_failure_names_actionable_role(self) -> None:
        rendered = deploy_preflight_failure_message(
            app_name="aca-app",
            resource_group="rg",
            check_name="publishing_config_read",
        )
        assert "functionapp_deploy_preflight_failed:publishing_config_read" in rendered
        assert "grant Website Contributor on aca-app" in rendered


class TestFixtureRequirementsExport:
    """Keep the Oryx export synchronized with the selected uv.lock closure."""

    def _repo_root(self) -> Path:
        return Path(__file__).resolve().parents[1]

    def _export_path(self) -> Path:
        return self._repo_root() / "eng" / "constraints" / "aca-fixture-requirements.txt"

    def _requirements(self) -> tuple[Requirement, ...]:
        lines = self._export_path().read_text(encoding="utf-8").splitlines()
        assert all(
            line and not line.startswith(("-", ".", "git+")) and " @ " not in line
            for line in lines
        )
        requirements = tuple(Requirement(line) for line in lines)
        assert all(requirement.url is None for requirement in requirements)
        return requirements

    def _locked_closure(self) -> dict[str, str]:
        lock = tomllib.loads((self._repo_root() / "uv.lock").read_text(encoding="utf-8"))
        packages = {
            canonicalize_name(package["name"]): package
            for package in lock["package"]
        }
        project_name = canonicalize_name("azurefunctions-agents-runtime")
        project = packages[project_name]
        pending = [
            *project["dependencies"],
            *project["optional-dependencies"]["aca-sandbox"],
            *project["optional-dependencies"]["monitor"],
        ]
        closure: dict[str, str] = {}
        processed_extras: dict[str, set[str]] = {}
        while pending:
            dependency = pending.pop()
            name = canonicalize_name(dependency["name"])
            package = packages[name]
            requested_extras = set(dependency.get("extra", ()))
            new_extras = requested_extras - processed_extras.get(name, set())
            if name not in closure:
                closure[name] = package["version"]
                pending.extend(package.get("dependencies", ()))
            for extra in new_extras:
                pending.extend(package.get("optional-dependencies", {}).get(extra, ()))
            processed_extras.setdefault(name, set()).update(requested_extras)
        return closure

    def test_export_exactly_matches_the_selected_uv_lock_closure(self) -> None:
        actual: dict[str, str] = {}
        for requirement in self._requirements():
            specifiers = list(requirement.specifier)
            assert len(specifiers) == 1
            specifier = specifiers[0]
            assert specifier.operator == "=="
            actual[canonicalize_name(requirement.name)] = specifier.version

        assert actual == self._locked_closure()
        assert "azurefunctions-agents-runtime" not in actual
        assert not {
            "azure-storage-queue",
            "jmespath",
            "mypy",
            "pytest",
            "pytest-asyncio",
            "pytest-cov",
            "ruff",
            "types-jsonschema",
        } & actual.keys()

    def test_export_has_the_same_active_linux_closure_for_both_python_minors(self) -> None:
        requirements = self._requirements()

        def active_names(python_version: str) -> set[str]:
            environment = default_environment()
            environment.update(
                python_version=python_version,
                python_full_version=f"{python_version}.0",
                sys_platform="linux",
                platform_system="Linux",
                implementation_name="cpython",
                platform_python_implementation="CPython",
            )
            return {
                canonicalize_name(requirement.name)
                for requirement in requirements
                if requirement.marker is None or requirement.marker.evaluate(environment)
            }

        python313 = active_names("3.13")
        python314 = active_names("3.14")
        assert python313 == python314
        assert {
            "azure-containerapps-sandbox",
            "azure-data-tables",
            "azure-monitor-opentelemetry",
        } <= python313
        assert {"colorama", "pywin32"}.isdisjoint(python313)

    def test_assembled_requirements_include_the_monitor_distribution(self) -> None:
        root = self._repo_root()
        rendered = render_requirements(
            wheel_filename="azurefunctions_agents_runtime-test.whl",
            requirements_export_path=(
                root / "eng" / "constraints" / "aca-fixture-requirements.txt"
            ),
            template_path=(
                root / "tests" / "live" / "apps" / "aca-qualification" / "requirements.txt"
            ),
        )
        assert "./azurefunctions_agents_runtime-test.whl" in rendered
        assert "azure-monitor-opentelemetry==1.8.8" in rendered
