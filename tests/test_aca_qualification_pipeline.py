"""Tests for the post-main ACA qualification pipeline helpers.

``eng/`` is outside the CI lint and type-check paths (`ruff check src tests`,
`mypy src`), so these tests are the only automated coverage the pipeline logic
gets. They deliberately exercise the pure decision functions -- marker
construction, marker comparison, and stale-sandbox selection -- rather than the
network commands, because those are where a wrong answer would silently pass a
qualification or delete a live sandbox.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from eng.scripts.aca_qualification_pipeline import (
    QualificationPipelineError,
    _redacted_reason,
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
    sweep_with_adapter,
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


class TestAcaStageGating:
    """Every ACA stage in the E2E pipeline must be gated off PR builds.

    The E2E pipeline carries PR triggers, so an ungated ACA stage would make
    every pull request pay for a full deploy-and-qualify cycle against real
    Azure. That failure is expensive and silent -- the stage simply runs -- so
    it is checked here rather than left to review.
    """

    def _e2e_pipeline(self) -> str:
        return (
            Path(__file__).resolve().parents[1] / "eng" / "ci" / "e2e-tests.yml"
        ).read_text(encoding="utf-8")

    def _aca_stage_blocks(self) -> dict[str, str]:
        text = self._e2e_pipeline()
        blocks: dict[str, str] = {}
        current: str | None = None
        collected: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("- stage:"):
                if current is not None:
                    blocks[current] = "\n".join(collected)
                name = stripped.split(":", 1)[1].strip()
                current = name if name.startswith("Aca") else None
                collected = []
                continue
            if current is not None:
                collected.append(line)
        if current is not None:
            blocks[current] = "\n".join(collected)
        return blocks

    def test_the_expected_aca_stages_are_present(self) -> None:
        assert set(self._aca_stage_blocks()) == {
            "AcaSweep",
            "AcaDeployColdPy313",
            "AcaDeployColdPy314",
            "AcaQualifyPy313",
            "AcaQualifyPy314",
        }

    def test_every_aca_stage_declares_a_condition(self) -> None:
        for name, block in self._aca_stage_blocks().items():
            assert "condition:" in block, f"{name} has no condition and would run on PRs."

    def test_every_condition_restricts_ci_to_main(self) -> None:
        for name, block in self._aca_stage_blocks().items():
            condition = next(
                line for line in block.splitlines() if line.strip().startswith("condition:")
            )
            assert "'refs/heads/main'" in condition, f"{name} does not pin CI runs to main."
            assert "'IndividualCI', 'BatchedCI'" in condition, (
                f"{name} does not restrict automatic runs to CI reasons; a PR or "
                "scheduled build could satisfy it."
            )

    def test_no_condition_admits_pr_or_scheduled_builds(self) -> None:
        """Assert the exclusions directly, not via the allow-list substring.

        Checking only that the CI reasons appear still passes when another
        reason is appended to the same list, so the excluded reasons are named
        here explicitly.
        """
        for name, block in self._aca_stage_blocks().items():
            condition = next(
                line for line in block.splitlines() if line.strip().startswith("condition:")
            )
            for reason in ("PullRequest", "Schedule"):
                assert reason not in condition, (
                    f"{name} names {reason} in its condition; these stages must not "
                    "run on PR or scheduled builds."
                )

    def test_manual_runs_stay_possible_on_any_branch(self) -> None:
        """Manual runs are the only way to exercise these stages pre-merge."""
        for name, block in self._aca_stage_blocks().items():
            condition = next(
                line for line in block.splitlines() if line.strip().startswith("condition:")
            )
            assert "'Manual'" in condition, (
                f"{name} would reject manual runs, removing the only way to test "
                "these stages from a topic branch."
            )

    def test_official_build_no_longer_defines_aca_stages(self) -> None:
        """The stages moved; leaving copies behind would double-deploy."""
        official = (
            Path(__file__).resolve().parents[1] / "eng" / "ci" / "official-build.yml"
        ).read_text(encoding="utf-8")
        for stage in ("AcaSweep", "AcaDeployCold", "AcaQualify"):
            assert stage not in official, (
                f"{stage} still present in official-build.yml; both pipelines would "
                "deploy to the same fixture apps."
            )

    def test_official_build_keeps_its_existing_contracts(self) -> None:
        official = (
            Path(__file__).resolve().parents[1] / "eng" / "ci" / "official-build.yml"
        ).read_text(encoding="utf-8")
        for fragment in (
            "pr: none",
            "- stage: Build",
            "- stage: RunTests",
            "- stage: RunE2ETests",
            "acaServiceConnection: 'larohra-sandboxgroup-test'",
        ):
            assert fragment in official, f"official-build.yml lost {fragment!r}."


class TestPipelineEnvironmentContract:
    """Validate values the pipeline passes against the bounds the code enforces.

    The templates set environment variables that the live-test support module
    validates at fixture time. Nothing previously compared the two, so the
    pipeline shipped a timeout of 300 seconds against a hard bound of 230 --
    which would have raised ``AcaSmokeEnvironmentError`` during fixture setup
    and errored every deployed suite before a single assertion ran. 230 is not
    arbitrary: it is the Azure Functions platform HTTP request ceiling, so any
    larger value is unreachable regardless of what the client asks for.
    """

    def _template_env(self, name: str) -> dict[str, str]:
        root = Path(__file__).resolve().parents[1]
        template = root / "eng" / "templates" / "official" / "jobs" / name
        found: dict[str, str] = {}
        for line in template.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if ":" not in stripped or stripped.startswith("#"):
                continue
            key, _, value = stripped.partition(":")
            if key.strip().startswith("AZURE_FUNCTIONS_AGENTS_"):
                found[key.strip()] = value.strip().strip("'\"")
        return found

    @pytest.mark.parametrize("template", ["aca-deploy-cold.yml", "aca-qualify.yml"])
    def test_timeout_is_within_the_enforced_bound(self, template: str) -> None:
        env = self._template_env(template)
        raw = env.get("AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TIMEOUT_SECONDS")
        assert raw is not None, f"{template} must set the deployed timeout."
        timeout = float(raw)
        assert 1 <= timeout <= 230, (
            f"{template} passes {timeout}s, outside the 1-230s bound enforced by "
            "tests/live/aca_deployed_agent_support.py; the suites would error "
            "during fixture setup."
        )

    @pytest.mark.parametrize("template", ["aca-deploy-cold.yml", "aca-qualify.yml"])
    def test_the_bound_still_matches_the_support_module(self, template: str) -> None:
        """Fail if the support module's bound moves away from the platform limit."""
        source = (
            Path(__file__).resolve().parents[1]
            / "tests"
            / "live"
            / "aca_deployed_agent_support.py"
        ).read_text(encoding="utf-8")
        assert "1 <= timeout <= 230" in source, (
            "The enforced timeout bound changed; re-check the value the pipeline "
            f"passes in {template}."
        )


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

    # The exact suite file set is intentional: support modules and unrelated live
    # fixtures contain constants for different deployed apps, so globbing all
    # live files would create false requirements for this fixture.
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


class TestSweepAdapterContract:
    """Bind the sweep's adapter call to the real SDK signature.

    The first live run failed with a bare ``TypeError`` because the sweep called
    ``list_sandboxes()`` while the adapter declares ``list_sandboxes(*, labels)``
    -- keyword-only and required. Every unit test still passed, because the
    selection logic is pure and was exercised without an adapter at all, so the
    seam that actually broke was never touched.

    These tests copy the real signature rather than accepting any call, so a
    future drift fails here instead of in a paid pipeline run.
    """

    def test_real_adapter_requires_keyword_labels(self) -> None:
        from azure_functions_agents.transport.aca_sdk import AcaSandboxAdapter

        parameters = inspect.signature(AcaSandboxAdapter.list_sandboxes).parameters
        labels = parameters["labels"]
        assert labels.kind is inspect.Parameter.KEYWORD_ONLY
        assert labels.default is inspect.Parameter.empty, (
            "labels is required; the sweep must pass it explicitly."
        )

    def test_sweep_passes_an_empty_selector(self) -> None:
        """An empty selector is 'no filter', which is what age-scoping needs."""
        seen: dict[str, object] = {}

        class _StubAdapter:
            async def list_sandboxes(self, *, labels: dict[str, str]) -> tuple[object, ...]:
                seen["labels"] = labels
                return ()

            async def delete_sandbox(self, sandbox_id: str) -> None:  # pragma: no cover
                raise AssertionError("nothing stale should be deleted")

            async def close(self) -> None:
                seen["closed"] = True

        async def _run() -> str:
            return await sweep_with_adapter(
                _StubAdapter(), now=datetime(2026, 8, 19, 12, 0, tzinfo=UTC), max_age_hours=6
            )

        report = asyncio.run(_run())
        assert seen["labels"] == {}
        assert seen["closed"] is True
        assert "stale=0" in report


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
