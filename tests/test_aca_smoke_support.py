from __future__ import annotations

import asyncio
import hashlib
import io
import zipfile
from types import SimpleNamespace

import pytest
from azure.core.exceptions import HttpResponseError

from azure_functions_agents.controller.package import (
    FUNCS_ZIP_DIGEST_KIND,
    CapturedContentPackage,
)
from azure_functions_agents.harness.delegation import rebuild_agent_catalog
from tests.live import aca_smoke_support


class _EmptySandboxAdapter:
    def __init__(self) -> None:
        self.closed = False
        self.label_queries: list[dict[str, str]] = []

    async def list_sandboxes(self, *, labels: dict[str, str]) -> tuple[object, ...]:
        self.label_queries.append(labels)
        return ()

    async def list_snapshots(self) -> tuple[object, ...]:
        return ()

    async def delete_snapshot(self, _: str) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class _ForbiddenSandboxAdapter:
    def __init__(self) -> None:
        self.calls = 0

    async def list_sandboxes(self, *, labels: dict[str, str]) -> tuple[object, ...]:
        self.calls += 1
        error = HttpResponseError("Operation returned an invalid status 'Forbidden'")
        error.status_code = 403
        raise error


class _FamilySandboxAdapter:
    def __init__(
        self,
        *,
        sandbox_lists: list[tuple[SimpleNamespace, ...]],
        snapshot_lists: list[tuple[SimpleNamespace, ...]],
        snapshot_failures: set[str] = frozenset(),
    ) -> None:
        self._sandbox_lists = sandbox_lists
        self._snapshot_lists = snapshot_lists
        self._snapshot_failures = snapshot_failures
        self.deleted_snapshots: list[str] = []
        self.sandbox_list_calls = 0
        self.snapshot_list_calls = 0

    async def list_sandboxes(
        self, *, labels: dict[str, str]
    ) -> tuple[SimpleNamespace, ...]:
        del labels
        self.sandbox_list_calls += 1
        return self._sandbox_lists.pop(0)

    async def list_snapshots(self) -> tuple[SimpleNamespace, ...]:
        self.snapshot_list_calls += 1
        return self._snapshot_lists.pop(0)

    async def delete_snapshot(self, snapshot_id: str) -> None:
        self.deleted_snapshots.append(snapshot_id)
        if snapshot_id in self._snapshot_failures:
            raise RuntimeError(f"provider body snapshot={snapshot_id}; token=do-not-disclose")


def _set_model_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_AZURE_OPENAI_ENDPOINT",
        "https://smoke-model.openai.azure.com",
    )
    monkeypatch.setenv(
        "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_AZURE_OPENAI_DEPLOYMENT",
        "u3-gpt-5-6-luna-20260709",
    )


def _archive(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _captured_content_package(entries: dict[str, bytes]) -> CapturedContentPackage:
    archive = _archive(entries)
    return CapturedContentPackage.create(
        archive_bytes=archive,
        digest_kind=FUNCS_ZIP_DIGEST_KIND,
        digest=f"sha256:{hashlib.sha256(archive).hexdigest()}",
    )


def test_composite_real_agent_package_contains_catalog_and_delivered_dependencies() -> None:
    agent_project = _captured_content_package(
        {
            "agents.config.yaml": b"default_agent: model_turn\n",
            "model_turn.agent.md": b"---\nname: model_turn\n---\nReply briefly.\n",
        }
    )
    closure = aca_smoke_support.DependencyClosureArchive(
        payload=_archive(
            {
                "azure_functions_agents/__init__.py": b"VERSION = 'test'\n",
                "agent_framework/__init__.py": b"",
            }
        ),
        entry_count=2,
    )

    first = aca_smoke_support.compose_real_agent_project_package(agent_project, closure)
    second = aca_smoke_support.compose_real_agent_project_package(agent_project, closure)

    assert first.archive_bytes == second.archive_bytes
    assert first.size <= aca_smoke_support._CLOSURE_ARCHIVE_MAX_BYTES
    assert first.digest == f"sha256:{hashlib.sha256(first.archive_bytes).hexdigest()}"
    with zipfile.ZipFile(io.BytesIO(first.archive_bytes)) as archive:
        assert archive.namelist() == [
            ".python_packages/lib/site-packages/agent_framework/__init__.py",
            ".python_packages/lib/site-packages/azure_functions_agents/__init__.py",
            "agents.config.yaml",
            "model_turn.agent.md",
        ]
        assert archive.read("agents.config.yaml") == b"default_agent: model_turn\n"
        assert (
            archive.read(
                ".python_packages/lib/site-packages/azure_functions_agents/__init__.py"
            )
            == b"VERSION = 'test'\n"
        )
        assert all(member.extract_version <= 20 for member in archive.infolist())


def test_model_config_forwards_only_guest_safe_model_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_model_environment(monkeypatch)
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_REASONING_EFFORT", "medium")

    config = aca_smoke_support.aca_smoke_model_config_from_environment()

    assert config.sandbox_environment() == {
        "AZURE_FUNCTIONS_AGENTS_PROVIDER": "azure_openai",
        "AZURE_OPENAI_ENDPOINT": "https://smoke-model.openai.azure.com",
        "AZURE_OPENAI_DEPLOYMENT": "u3-gpt-5-6-luna-20260709",
        "AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT": "medium",
    }
    assert "AZURE_CLIENT_ID" not in config.sandbox_environment()

    policy = config.sandbox_egress_policy()
    assert policy.default_action == "Deny"
    assert policy.traffic_inspection == "Full"
    assert [(rule.host, rule.action) for rule in policy.host_rules] == [
        ("management.azure.com", "Deny"),
        ("management.azuredevcompute.io", "Deny"),
        ("smoke-model.openai.azure.com", "Allow"),
    ]


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        (
            "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_AZURE_OPENAI_ENDPOINT",
            "http://smoke-model.openai.azure.com",
            "must be an HTTPS endpoint",
        ),
        (
            "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_REASONING_EFFORT",
            "maximum",
            "must be one of",
        ),
    ],
)
def test_model_config_rejects_invalid_environment_inputs(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    message: str,
) -> None:
    _set_model_environment(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(aca_smoke_support.AcaSmokeEnvironmentError, match=message):
        aca_smoke_support.aca_smoke_model_config_from_environment()


def test_redact_aca_smoke_evidence_removes_secret_shaped_values() -> None:
    evidence = aca_smoke_support.redact_aca_smoke_evidence(
        "Authorization: Bearer top-secret; api_key=another-secret token: third-secret"
    )

    assert "top-secret" not in evidence
    assert "another-secret" not in evidence
    assert "third-secret" not in evidence
    assert "[redacted]" in evidence


def test_model_preflight_failure_never_surfaces_guest_output() -> None:
    with pytest.raises(
        aca_smoke_support.AcaSmokeEnvironmentError,
        match="guest diagnostics were redacted",
    ) as error:
        aca_smoke_support._require_successful_model_preflight(
            description="model preflight failed",
            exit_code=1,
        )

    assert "prompt" not in str(error.value).casefold()


def test_real_turn_fixture_rebuilds_a_no_tools_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SANDBOX", "1")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "u3-gpt-5-6-luna-20260709")

    catalog = rebuild_agent_catalog(aca_smoke_support._LIVE_MODEL_AGENT_PROJECT_ROOT)
    entry = catalog["model_turn"]

    assert entry.resolved.model == "u3-gpt-5-6-luna-20260709"
    assert entry.capabilities.filtered_user_tools == []
    assert entry.capabilities.filtered_mcp_tools == []
    assert entry.capabilities.web_request_tools == []


@pytest.mark.asyncio
async def test_cleanup_requires_confirmation_when_a_create_may_have_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _EmptySandboxAdapter()
    monkeypatch.setattr(aca_smoke_support, "_LABEL_RECONCILIATION_DELAY_SECONDS", 0.0)

    with pytest.raises(RuntimeError, match="label cleanup did not find"):
        await aca_smoke_support.cleanup_sandbox(
            adapter=adapter,
            handle=None,
            sandbox_id=None,
            labels={
                "owner_kind": "aca_smoke_ci",
                "owner_hash": "owner",
                "app_hash": "app",
                "session_id": "session",
            },
            creation_attempted=True,
        )

    assert adapter.closed is True
    assert len(adapter.label_queries) == 4


@pytest.mark.asyncio
async def test_shielded_cleanup_finishes_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_started = asyncio.Event()
    permit_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def delayed_cleanup(**_kwargs: object) -> None:
        cleanup_started.set()
        await permit_cleanup.wait()
        cleanup_finished.set()

    monkeypatch.setattr(aca_smoke_support, "cleanup_sandbox", delayed_cleanup)
    cleanup_task = asyncio.create_task(
        aca_smoke_support._cleanup_sandbox_shielded(
            adapter=None,
            handle=None,
            sandbox_id=None,
            labels={},
            creation_attempted=False,
        )
    )
    await cleanup_started.wait()
    cleanup_task.cancel()
    permit_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await cleanup_task

    assert cleanup_finished.is_set()


@pytest.mark.asyncio
async def test_label_cleanup_aborts_immediately_on_authorization_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _ForbiddenSandboxAdapter()
    monkeypatch.setattr(aca_smoke_support, "_LABEL_RECONCILIATION_DELAY_SECONDS", 0.0)

    with pytest.raises(
        aca_smoke_support.AcaSmokeEnvironmentError, match="data-plane authorization"
    ):
        await aca_smoke_support._delete_labelled_sandboxes(
            adapter,  # type: ignore[arg-type]
            {"owner_kind": "aca_smoke_ci"},
        )

    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_reaper_deletes_every_snapshot_and_sandbox_after_a_snapshot_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_sandbox = SimpleNamespace(sandbox_id="sandbox-first", labels={"run": "current"})
    second_sandbox = SimpleNamespace(sandbox_id="sandbox-second", labels={"run": "current"})
    first_snapshot = SimpleNamespace(
        snapshot_id="snapshot-first", sandbox_id="sandbox-first"
    )
    second_snapshot = SimpleNamespace(
        snapshot_id="snapshot-second", sandbox_id="sandbox-second"
    )
    adapter = _FamilySandboxAdapter(
        sandbox_lists=[(first_sandbox, second_sandbox), ()],
        snapshot_lists=[(first_snapshot, second_snapshot), (), ()],
        snapshot_failures={"snapshot-first"},
    )
    deleted_sandboxes: list[str] = []

    async def force_delete(_adapter: object, sandbox_id: str) -> None:
        deleted_sandboxes.append(sandbox_id)

    monkeypatch.setattr(aca_smoke_support, "_force_delete_by_id", force_delete)

    with pytest.raises(aca_smoke_support.AcaSmokeEnvironmentError):
        await aca_smoke_support.reap_labelled_sandbox_family(adapter, {"run": "current"})  # type: ignore[arg-type]

    assert adapter.deleted_snapshots[:2] == ["snapshot-first", "snapshot-second"]
    assert deleted_sandboxes == ["sandbox-first", "sandbox-second"]
    assert adapter.sandbox_list_calls == 2
    assert adapter.snapshot_list_calls == 3


@pytest.mark.asyncio
async def test_reaper_continues_after_sandbox_failure_and_relists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_sandbox = SimpleNamespace(sandbox_id="sandbox-first", labels={"run": "current"})
    second_sandbox = SimpleNamespace(sandbox_id="sandbox-second", labels={"run": "current"})
    adapter = _FamilySandboxAdapter(
        sandbox_lists=[(first_sandbox, second_sandbox), ()],
        snapshot_lists=[(), (), ()],
    )
    deleted_sandboxes: list[str] = []

    async def force_delete(_adapter: object, sandbox_id: str) -> None:
        deleted_sandboxes.append(sandbox_id)
        if sandbox_id == "sandbox-first":
            raise RuntimeError("provider body sandbox=sandbox-first; token=do-not-disclose")

    monkeypatch.setattr(aca_smoke_support, "_force_delete_by_id", force_delete)

    with pytest.raises(aca_smoke_support.AcaSmokeEnvironmentError) as error:
        await aca_smoke_support.reap_labelled_sandbox_family(adapter, {"run": "current"})  # type: ignore[arg-type]

    assert deleted_sandboxes == ["sandbox-first", "sandbox-second"]
    assert adapter.sandbox_list_calls == 2
    assert adapter.snapshot_list_calls == 3
    assert "sandbox-delete:unexpected:RuntimeError" in str(error.value)


@pytest.mark.asyncio
async def test_reaper_aggregate_error_redacts_provider_data_and_reports_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = SimpleNamespace(sandbox_id="sandbox-resource-id", labels={"run": "current"})
    snapshot = SimpleNamespace(
        snapshot_id="snapshot-resource-id", sandbox_id="sandbox-resource-id"
    )
    adapter = _FamilySandboxAdapter(
        sandbox_lists=[(sandbox,), (sandbox,)],
        snapshot_lists=[(snapshot,), (), (snapshot,)],
        snapshot_failures={"snapshot-resource-id"},
    )

    async def force_delete(_adapter: object, _sandbox_id: str) -> None:
        return None

    monkeypatch.setattr(aca_smoke_support, "_force_delete_by_id", force_delete)

    with pytest.raises(aca_smoke_support.AcaSmokeEnvironmentError) as error:
        await aca_smoke_support.reap_labelled_sandbox_family(adapter, {"run": "current"})  # type: ignore[arg-type]

    message = str(error.value)
    assert "snapshot-delete:unexpected:RuntimeError" in message
    assert "leaked-sandboxes=1" in message
    assert "leaked-snapshots=1" in message
    assert "sandbox-resource-id" not in message
    assert "snapshot-resource-id" not in message
    assert "provider body" not in message
    assert "do-not-disclose" not in message


@pytest.mark.asyncio
async def test_reaper_returns_selected_count_after_successful_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_sandbox = SimpleNamespace(sandbox_id="sandbox-first", labels={"run": "current"})
    second_sandbox = SimpleNamespace(sandbox_id="sandbox-second", labels={"run": "current"})
    first_snapshot = SimpleNamespace(
        snapshot_id="snapshot-first", sandbox_id="sandbox-first"
    )
    second_snapshot = SimpleNamespace(
        snapshot_id="snapshot-second", sandbox_id="sandbox-second"
    )
    adapter = _FamilySandboxAdapter(
        sandbox_lists=[(first_sandbox, second_sandbox), ()],
        snapshot_lists=[(first_snapshot, second_snapshot), (), ()],
    )
    deleted_sandboxes: list[str] = []

    async def force_delete(_adapter: object, sandbox_id: str) -> None:
        deleted_sandboxes.append(sandbox_id)

    monkeypatch.setattr(aca_smoke_support, "_force_delete_by_id", force_delete)

    reaped = await aca_smoke_support.reap_labelled_sandbox_family(adapter, {"run": "current"})  # type: ignore[arg-type]

    assert reaped == 2
    assert adapter.deleted_snapshots == ["snapshot-first", "snapshot-second"]
    assert deleted_sandboxes == ["sandbox-first", "sandbox-second"]
    assert adapter.sandbox_list_calls == 2
    assert adapter.snapshot_list_calls == 3


def test_setup_error_renders_empty_cause_as_type_name() -> None:
    result = aca_smoke_support._setup_error("ACA smoke setup failed", TimeoutError())

    message = str(result)
    assert "TimeoutError" in message
    assert not message.endswith(": ")


@pytest.mark.parametrize("status_code", [403, None])
def test_setup_error_does_not_double_prefix_a_wrapped_message(status_code: int | None) -> None:
    inner = HttpResponseError(
        "ACA smoke cleanup failed: Operation returned an invalid status 'Forbidden'."
    )
    inner.status_code = status_code

    result = aca_smoke_support._setup_error("ACA smoke cleanup failed", inner)

    message = str(result)
    assert "ACA smoke cleanup failed: ACA smoke cleanup failed:" not in message
    assert message.count("ACA smoke cleanup failed:") == 1


def test_aca_smoke_run_id_uses_the_seeded_ci_identifier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(aca_smoke_support.ACA_SMOKE_RUN_ID_ENV_VAR, "123456")

    assert aca_smoke_support.aca_smoke_run_id() == "123456"


def test_aca_smoke_run_id_sanitizes_and_bounds_the_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(aca_smoke_support.ACA_SMOKE_RUN_ID_ENV_VAR, "-Build/ID_" + "9" * 40)

    run_id = aca_smoke_support.aca_smoke_run_id()

    assert run_id == "buildid9999999999"[: aca_smoke_support._MAX_RUN_ID_LENGTH]
    assert set(run_id) <= set("abcdefghijklmnopqrstuvwxyz0123456789-")
    assert not run_id.startswith("-")


def test_aca_smoke_run_id_falls_back_to_a_unique_local_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(aca_smoke_support.ACA_SMOKE_RUN_ID_ENV_VAR, raising=False)

    first = aca_smoke_support.aca_smoke_run_id()
    second = aca_smoke_support.aca_smoke_run_id()

    assert first and second
    assert first != second


def test_session_belongs_to_run_matches_only_its_own_run() -> None:
    assert aca_smoke_support.session_belongs_to_run(
        {"session_id": "123456-aca-harness-smoke-0011223344556677"}, "123456"
    )
    # A different run's sandbox must never be selected for deletion.
    assert not aca_smoke_support.session_belongs_to_run(
        {"session_id": "999999-aca-harness-smoke-0011223344556677"}, "123456"
    )
    # A run id that is a numeric prefix of another must not match across the delimiter.
    assert not aca_smoke_support.session_belongs_to_run(
        {"session_id": "1234567-aca-harness-smoke-0011223344556677"}, "123456"
    )
    assert not aca_smoke_support.session_belongs_to_run({}, "123456")
