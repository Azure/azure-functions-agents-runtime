import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_framework import FunctionInvocationContext, FunctionTool

import azure_functions_agents.runner as runner
from azure_functions_agents.controller.package import CapturedContentPackage
from azure_functions_agents.experimental.hybrid_config import (
    HYBRID_SANDBOX_GROUP_ENV,
    HYBRID_SANDBOX_REGION_ENV,
    HybridSandboxSettings,
)
from azure_functions_agents.experimental.hybrid_observability import (
    HybridMetric,
    HybridProgressPhase,
    HybridProgressStatus,
)
from azure_functions_agents.experimental.hybrid_protocol import (
    HybridInvocationStatus,
    HybridToolDescriptor,
    HybridToolInternalTimings,
    HybridToolInvocationResult,
    HybridToolManifest,
    HybridToolProvenance,
    canonical_hybrid_json_bytes,
)
from azure_functions_agents.experimental.hybrid_reaper import hybrid_app_hash
from azure_functions_agents.experimental.hybrid_tools import (
    _APP_ZIP_PATH,
    _EXECUTOR_PATH,
    _MIN_DELETE_ATTEMPT_SECONDS,
    _POST_RUN_DELETE_TIMEOUT_SECONDS,
    _ROLLBACK_DELETE_ATTEMPTS,
    _ROLLBACK_DELETE_TIMEOUT_SECONDS,
    _TRANSPORT_POLL_INTERVAL_SECONDS,
    HybridPreparedInvocation,
    HybridToolMiddleware,
    InvocationSandboxLease,
    _best_effort_delete,
    _delete_attempt_seconds,
    _deliver_executor,
    _hybrid_package_root,
    _poll_startup_file,
    _post_run_delete_seconds,
    _provisioning_labels,
    open_hybrid_invocation,
)
from azure_functions_agents.session_state import AppIdentityResolutionError
from azure_functions_agents.transport.transport_models import (
    SandboxCreateRequest,
    SandboxFileNotFoundError,
    SandboxLifecyclePolicy,
    SandboxNotFoundError,
)

_GROUP_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/"
    "resourceGroups/rg/providers/Microsoft.App/sandboxGroups/group"
)


class _Backend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def invoke(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: dict[str, object],
        deadline: float,
    ) -> HybridToolInvocationResult:
        assert deadline > asyncio.get_running_loop().time()
        self.calls.append((call_id, tool_name, arguments))
        return HybridToolInvocationResult(
            protocol_version="1",
            call_id=call_id,
            tool_name=tool_name,
            status=HybridInvocationStatus.SUCCESS,
            value={"ok": True},
            stdout="out",
            stderr="",
            exit_code=0,
            error=None,
            timings=HybridToolInternalTimings(
                queue_wait_ms=1,
                execution_ms=2,
                serialization_ms=3,
            ),
        )


def _tool(name: str) -> FunctionTool:
    async def handler(value: str) -> str:
        return value

    return FunctionTool(
        name=name,
        description=name,
        func=handler,
    )


@pytest.mark.asyncio
async def test_hybrid_middleware_routes_only_exact_local_stub() -> None:
    backend = _Backend()
    local = _tool("local")
    remote = _tool("remote")
    middleware = HybridToolMiddleware(
        backend,
        [local],
        deadline=asyncio.get_running_loop().time() + 30,
    )
    local_context = FunctionInvocationContext(
        local,
        {"value": "x"},
        metadata={"call_id": "maf-call-1"},
    )
    remote_context = FunctionInvocationContext(remote, {"value": "y"})
    next_calls = 0

    async def call_next() -> None:
        nonlocal next_calls
        next_calls += 1
        remote_context.result = "remote"

    await middleware.process(local_context, call_next)
    await middleware.process(remote_context, call_next)

    assert backend.calls == [("maf-call-1", "local", {"value": "x"})]
    assert local_context.result == {
        "value": {"ok": True},
        "stdout": "out",
        "stderr": "",
        "exit_code": 0,
    }
    assert remote_context.result == "remote"
    assert next_calls == 1


class _Handle:
    def __init__(self) -> None:
        self.identity = SimpleNamespace(sandbox_id="sandbox")
        self.results: dict[str, bytes] = {}
        self.requests: list[dict[str, object]] = []
        self.deleted = 0
        self.delete_requests = 0
        self.closed = 0
        self.lifecycle_policies: list[SandboxLifecyclePolicy] = []
        self.shutdown_written = False

    async def write_file(
        self, path: str, content: bytes, *, create_dirs: bool = False
    ) -> None:
        assert create_dirs
        if "/requests/" not in path:
            if path.endswith("/shutdown"):
                self.shutdown_written = True
            return
        request = json.loads(content)
        self.requests.append(request)
        call_id = str(request["call_id"])
        result = HybridToolInvocationResult(
            protocol_version="1",
            call_id=call_id,
            tool_name=str(request["tool_name"]),
            status=HybridInvocationStatus.SUCCESS,
            value={"sequence": len(self.requests)},
            stdout="",
            stderr="",
            exit_code=None,
            error=None,
            timings=HybridToolInternalTimings(
                queue_wait_ms=1,
                execution_ms=2,
                serialization_ms=3,
            ),
        )
        self.results[path.replace("/requests/", "/results/")] = canonical_hybrid_json_bytes(
            result
        )

    async def read_file(self, path: str) -> bytes:
        try:
            return self.results[path]
        except KeyError:
            raise SandboxFileNotFoundError("missing") from None

    async def delete(self) -> None:
        self.deleted += 1

    async def request_delete(self) -> None:
        self.delete_requests += 1

    async def set_lifecycle_policy(self, policy: SandboxLifecyclePolicy) -> None:
        self.lifecycle_policies.append(policy)

    async def close(self) -> None:
        self.closed += 1


class _Provider:
    def __init__(self) -> None:
        self.closed = 0
        self.deleted: list[str] = []

    async def close(self) -> None:
        self.closed += 1

    async def delete_sandbox(self, sandbox_id: str) -> None:
        self.deleted.append(sandbox_id)


class _AcquireHandle(_Handle):
    pass


class _AcquireProvider(_Provider):
    def __init__(self, handle: _AcquireHandle) -> None:
        super().__init__()
        self.group = SimpleNamespace(resource_id=_GROUP_ID, region="westus2")
        self.handle = handle
        self.create_calls = 0
        self.requests: list[SandboxCreateRequest] = []

    async def create(
        self,
        request: SandboxCreateRequest,
        *,
        persisted_group: object,
    ) -> _AcquireHandle:
        assert persisted_group is not None
        self.create_calls += 1
        self.requests.append(request)
        return self.handle


def _settings() -> HybridSandboxSettings:
    return HybridSandboxSettings(
        group_resource_id=_GROUP_ID,
        region="westus2",
        allowed_hosts=(),
        sandbox_disk="python-3.13",
        create_timeout_seconds=5,
        ready_timeout_seconds=5,
        drain_timeout_seconds=1,
        orphan_age_seconds=1200,
    )


def _manifest() -> HybridToolManifest:
    return HybridToolManifest(
        protocol_version="1",
        tools=(
            HybridToolDescriptor(
                name="customer_probe",
                description="probe",
                parameters={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                    "additionalProperties": False,
                },
                provenance=HybridToolProvenance.LOCAL,
            ),
        ),
    )


def test_hybrid_package_root_defaults_to_function_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools.get_app_root",
        lambda: tmp_path,
    )

    assert _hybrid_package_root(_settings()) == tmp_path


@pytest.mark.asyncio
async def test_lease_shares_one_handle_and_hands_off_cleanup_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = _Handle()
    provider = _Provider()
    recorded_values: list[tuple[HybridMetric, float]] = []
    progress: list[tuple[HybridProgressPhase, HybridProgressStatus]] = []
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools.record_hybrid_value",
        lambda metric, value: recorded_values.append((metric, value)),
    )
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools.record_hybrid_progress",
        lambda phase, status, **_kwargs: progress.append((phase, status)),
    )
    lease = InvocationSandboxLease(
        settings=_settings(),
        operation_id="operation",
        provider=provider,  # type: ignore[arg-type]
        handle=handle,  # type: ignore[arg-type]
        manifest=_manifest(),
    )
    deadline = asyncio.get_running_loop().time() + 5
    tools, _middleware = lease.build_tools(deadline=deadline)

    with pytest.raises(RuntimeError, match="did not intercept"):
        await tools[0].invoke(message="not-routed")

    first = await lease.invoke(
        call_id="first",
        tool_name="customer_probe",
        arguments={"message": "a"},
        deadline=deadline,
    )
    second = await lease.invoke(
        call_id="second",
        tool_name="customer_probe",
        arguments={"message": "b"},
        deadline=deadline,
    )
    await lease.close()
    await lease.close()

    assert first.value == {"sequence": 1}
    assert second.value == {"sequence": 2}
    assert [request["call_id"] for request in handle.requests] == ["first", "second"]
    assert handle.deleted == 0
    assert handle.delete_requests == 1
    assert handle.shutdown_written
    assert handle.lifecycle_policies == [
        SandboxLifecyclePolicy.create(
            auto_suspend_seconds=300,
            auto_suspend_mode="Disk",
            auto_delete_seconds=600,
        )
    ]
    assert handle.closed == 1
    assert provider.closed == 1
    assert progress == [
        (HybridProgressPhase.TOOL_EXECUTION, HybridProgressStatus.STARTED),
        (HybridProgressPhase.TOOL_EXECUTION, HybridProgressStatus.COMPLETED),
        (HybridProgressPhase.TOOL_EXECUTION, HybridProgressStatus.STARTED),
        (HybridProgressPhase.TOOL_EXECUTION, HybridProgressStatus.COMPLETED),
        (HybridProgressPhase.CLEANUP_HANDOFF, HybridProgressStatus.STARTED),
        (HybridProgressPhase.CLEANUP_HANDOFF, HybridProgressStatus.COMPLETED),
        (HybridProgressPhase.CLEANUP_COMPLETE, HybridProgressStatus.COMPLETED),
    ]
    assert (
        len(
            [
                value
                for metric, value in recorded_values
                if metric is HybridMetric.TOOL_QUEUE_DURATION
            ]
        )
        == 2
    )


@pytest.mark.asyncio
async def test_parallel_calls_queue_before_writing_second_request() -> None:
    class _BlockingHandle(_Handle):
        def __init__(self) -> None:
            super().__init__()
            self.first_written = asyncio.Event()
            self.release_first = asyncio.Event()

        async def write_file(
            self, path: str, content: bytes, *, create_dirs: bool = False
        ) -> None:
            await super().write_file(path, content, create_dirs=create_dirs)
            if "/requests/" in path and len(self.requests) == 1:
                self.first_written.set()
                await self.release_first.wait()

    handle = _BlockingHandle()
    provider = _Provider()
    lease = InvocationSandboxLease(
        settings=_settings(),
        operation_id="operation",
        provider=provider,  # type: ignore[arg-type]
        handle=handle,  # type: ignore[arg-type]
        manifest=_manifest(),
    )
    deadline = asyncio.get_running_loop().time() + 5
    first = asyncio.create_task(
        lease.invoke(
            call_id="first",
            tool_name="customer_probe",
            arguments={"message": "a"},
            deadline=deadline,
        )
    )
    await handle.first_written.wait()
    second = asyncio.create_task(
        lease.invoke(
            call_id="second",
            tool_name="customer_probe",
            arguments={"message": "b"},
            deadline=deadline,
        )
    )
    await asyncio.sleep(0)

    assert [request["call_id"] for request in handle.requests] == ["first"]
    handle.release_first.set()
    await asyncio.gather(first, second)
    assert [request["call_id"] for request in handle.requests] == ["first", "second"]
    await lease.close()


@pytest.mark.asyncio
async def test_hybrid_context_passes_tools_through_when_gate_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(HYBRID_SANDBOX_GROUP_ENV, raising=False)
    tools = [_tool("worker")]

    async with open_hybrid_invocation(
        timeout_seconds=30,
        tools=tools,
        sandbox_tools=None,
        skill_paths=None,
        web_request_tools=None,
        workflow_enabled=False,
        subagents=None,
    ) as invocation:
        assert invocation.tools is tools
        assert invocation.middleware is None


@pytest.mark.asyncio
async def test_hybrid_context_closes_lease_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Lease:
        closed = False

        def build_tools(
            self, *, deadline: float
        ) -> tuple[list[FunctionTool], HybridToolMiddleware]:
            assert deadline > asyncio.get_running_loop().time()
            backend = _Backend()
            tool = _tool("local")
            return [tool], HybridToolMiddleware(backend, [tool], deadline=deadline)

        async def close(self, *, cancelled: bool = False) -> None:
            assert cancelled
            self.closed = True

    lease = _Lease()

    async def acquire(
        settings: HybridSandboxSettings,
        **kwargs: object,
    ) -> _Lease:
        assert settings.region == "westus2"
        assert kwargs["maximum_run_seconds"] == 30
        return lease

    monkeypatch.setenv(HYBRID_SANDBOX_GROUP_ENV, "group")
    monkeypatch.setenv(HYBRID_SANDBOX_REGION_ENV, "westus2")
    monkeypatch.setattr(InvocationSandboxLease, "acquire", acquire)

    with pytest.raises(asyncio.CancelledError):
        async with open_hybrid_invocation(
            timeout_seconds=30,
            tools=[],
            sandbox_tools=None,
            skill_paths=None,
            web_request_tools=None,
            workflow_enabled=False,
            subagents=None,
        ):
            raise asyncio.CancelledError

    assert lease.closed


@pytest.mark.asyncio
async def test_cancellation_hands_off_terminal_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress: list[tuple[HybridProgressPhase, HybridProgressStatus]] = []
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools.record_hybrid_progress",
        lambda phase, status, **_kwargs: progress.append((phase, status)),
    )
    handle = _Handle()
    provider = _Provider()
    lease = InvocationSandboxLease(
        settings=_settings(),
        operation_id="operation",
        provider=provider,  # type: ignore[arg-type]
        handle=handle,  # type: ignore[arg-type]
        manifest=_manifest(),
    )

    async def acquire(
        _settings: HybridSandboxSettings,
        **_kwargs: object,
    ) -> InvocationSandboxLease:
        return lease

    monkeypatch.setenv(HYBRID_SANDBOX_GROUP_ENV, "group")
    monkeypatch.setenv(HYBRID_SANDBOX_REGION_ENV, "westus2")
    monkeypatch.setattr(InvocationSandboxLease, "acquire", acquire)

    with pytest.raises(asyncio.CancelledError):
        async with open_hybrid_invocation(
            timeout_seconds=30,
            tools=[],
            sandbox_tools=None,
            skill_paths=None,
            web_request_tools=None,
            workflow_enabled=False,
            subagents=None,
        ):
            raise asyncio.CancelledError

    assert handle.delete_requests == 1
    assert handle.deleted == 0
    assert handle.lifecycle_policies[-1].auto_suspend_seconds == 300
    assert handle.closed == 1
    assert provider.closed == 1
    assert progress[-1] == (
        HybridProgressPhase.CLEANUP_COMPLETE,
        HybridProgressStatus.CANCELLED,
    )


@pytest.mark.asyncio
async def test_acquire_disables_suspend_before_package_delivery(
    monkeypatch: pytest.MonkeyPatch,
    deterministic_content_package: CapturedContentPackage,
) -> None:
    events: list[str] = []
    package_roots: list[object] = []

    class _OrderingHandle(_AcquireHandle):
        async def set_lifecycle_policy(self, policy: SandboxLifecyclePolicy) -> None:
            events.append("policy")
            await super().set_lifecycle_policy(policy)

    handle = _OrderingHandle()
    provider = _AcquireProvider(handle)

    async def provider_factory() -> _AcquireProvider:
        return provider

    async def package_factory(root: object) -> CapturedContentPackage:
        events.append("package_capture")
        package_roots.append(root)
        return deterministic_content_package

    async def deliver(_handle: object, _package: object) -> None:
        events.append("package_delivery")

    async def discover(
        _handle: object,
        _timeout: float,
        _digest: str,
    ) -> HybridToolManifest:
        events.append("discovery")
        return _manifest()

    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools._deliver_executor",
        deliver,
    )
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools._start_and_discover",
        discover,
    )
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools.hybrid_app_hash",
        lambda: f"a1-{'a' * 52}",
    )

    bundle_root = Path("resolved-bundle")
    lease = await InvocationSandboxLease.acquire(
        replace(_settings(), tool_bundle_root=bundle_root),
        maximum_run_seconds=900,
        provider_factory=provider_factory,  # type: ignore[arg-type]
        package_factory=package_factory,  # type: ignore[arg-type]
    )

    assert provider.requests[0].auto_suspend_seconds == 3600
    assert package_roots == [bundle_root]
    assert events == ["policy", "package_capture", "package_delivery", "discovery"]
    assert handle.lifecycle_policies[0] == SandboxLifecyclePolicy.create(
        auto_suspend_seconds=None,
        auto_delete_seconds=600,
    )
    await lease.close()


@pytest.mark.asyncio
async def test_package_delivery_does_not_read_application_archive_back(
    deterministic_content_package: CapturedContentPackage,
) -> None:
    class _DeliveryHandle(_Handle):
        def __init__(self) -> None:
            super().__init__()
            self.files: dict[str, bytes] = {}
            self.read_paths: list[str] = []

        async def write_file(
            self,
            path: str,
            content: bytes,
            *,
            create_dirs: bool = False,
        ) -> None:
            assert create_dirs
            self.files[path] = content

        async def read_file(self, path: str) -> bytes:
            self.read_paths.append(path)
            return self.files[path]

    handle = _DeliveryHandle()

    await _deliver_executor(
        handle,  # type: ignore[arg-type]
        deterministic_content_package,
    )

    assert _APP_ZIP_PATH in handle.files
    assert handle.read_paths == [_EXECUTOR_PATH]


@pytest.mark.asyncio
async def test_startup_failure_marker_fails_fast_without_waiting_for_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = _Handle()
    handle.results["/startup-failure"] = json.dumps(
        {
            "exception_type": "PackageDigestMismatchError",
            "phase": "package_verify",
            "protocol_version": "1",
            "startup_failed": True,
        }
    ).encode()
    recorded: list[HybridMetric] = []
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools.record_hybrid_count",
        lambda metric: recorded.append(metric),
    )
    started = asyncio.get_running_loop().time()

    with pytest.raises(RuntimeError, match="PackageDigestMismatchError"):
        await _poll_startup_file(
            handle,  # type: ignore[arg-type]
            "/ready",
            "/startup-failure",
            started + 30,
        )

    assert asyncio.get_running_loop().time() - started < 0.5
    assert HybridMetric.PACKAGE_VERIFY_FAILURES in recorded


@pytest.mark.asyncio
async def test_acquire_policy_failure_deletes_before_package_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _PolicyFailureHandle(_AcquireHandle):
        async def set_lifecycle_policy(self, _policy: SandboxLifecyclePolicy) -> None:
            raise RuntimeError("policy failed")

    package_called = False
    handle = _PolicyFailureHandle()
    provider = _AcquireProvider(handle)

    async def provider_factory() -> _AcquireProvider:
        return provider

    async def package_factory(_root: object) -> object:
        nonlocal package_called
        package_called = True
        return object()

    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools.hybrid_app_hash",
        lambda: f"a1-{'a' * 52}",
    )

    with pytest.raises(RuntimeError, match="policy failed"):
        await InvocationSandboxLease.acquire(
            _settings(),
            maximum_run_seconds=900,
            provider_factory=provider_factory,  # type: ignore[arg-type]
            package_factory=package_factory,  # type: ignore[arg-type]
        )

    assert package_called is False
    assert handle.deleted == 1
    assert handle.closed == 1
    assert provider.closed == 1


@pytest.mark.asyncio
async def test_acquire_rolls_back_sandbox_when_package_delivery_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = _AcquireHandle()
    provider = _AcquireProvider(handle)

    async def provider_factory() -> _AcquireProvider:
        return provider

    async def package_factory(_root: object) -> object:
        return object()

    async def fail_delivery(_handle: object, _package: object) -> None:
        raise RuntimeError("delivery failed")

    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools._deliver_executor",
        fail_delivery,
    )
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools.hybrid_app_hash",
        lambda: f"a1-{'a' * 52}",
    )

    with pytest.raises(RuntimeError, match="delivery failed"):
        await InvocationSandboxLease.acquire(
            _settings(),
            provider_factory=provider_factory,  # type: ignore[arg-type]
            package_factory=package_factory,  # type: ignore[arg-type]
        )

    assert provider.create_calls == 1
    assert len(handle.lifecycle_policies) == 1
    assert handle.deleted == 1
    assert handle.closed == 1
    assert provider.closed == 1


@pytest.mark.asyncio
async def test_acquire_rollback_uses_provider_delete_after_handle_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingHandle(_AcquireHandle):
        async def delete(self) -> None:
            self.deleted += 1
            raise RuntimeError("transient handle delete")

    handle = _FailingHandle()
    provider = _AcquireProvider(handle)

    async def provider_factory() -> _AcquireProvider:
        return provider

    async def package_factory(_root: object) -> object:
        return object()

    async def fail_delivery(_handle: object, _package: object) -> None:
        raise RuntimeError("delivery failed")

    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools._deliver_executor",
        fail_delivery,
    )
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools.hybrid_app_hash",
        lambda: f"a1-{'a' * 52}",
    )

    with pytest.raises(RuntimeError, match="delivery failed"):
        await InvocationSandboxLease.acquire(
            _settings(),
            provider_factory=provider_factory,  # type: ignore[arg-type]
            package_factory=package_factory,  # type: ignore[arg-type]
        )

    assert handle.deleted == 1
    assert provider.deleted == ["sandbox"]
    assert handle.closed == 1
    assert provider.closed == 1


@pytest.mark.asyncio
async def test_delete_failure_uses_provider_fallback_without_raising() -> None:
    class _FailingDeleteHandle(_Handle):
        async def set_lifecycle_policy(self, _policy: SandboxLifecyclePolicy) -> None:
            raise RuntimeError("terminal policy failed")

        async def delete(self) -> None:
            self.deleted += 1
            raise RuntimeError("delete failed")

    handle = _FailingDeleteHandle()
    provider = _Provider()
    lease = InvocationSandboxLease(
        settings=_settings(),
        operation_id="operation",
        provider=provider,  # type: ignore[arg-type]
        handle=handle,  # type: ignore[arg-type]
        manifest=_manifest(),
    )

    await lease.close()

    assert handle.deleted == 1
    assert provider.deleted == ["sandbox"]
    assert handle.closed == 1
    assert provider.closed == 1


@pytest.mark.asyncio
async def test_delete_request_exception_uses_provider_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RequestFailureHandle(_Handle):
        async def request_delete(self) -> None:
            self.delete_requests += 1
            raise RuntimeError("request failed")

    recorded: list[HybridMetric] = []
    durations: list[HybridMetric] = []
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools.record_hybrid_count",
        lambda metric: recorded.append(metric),
    )
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools.record_hybrid_duration",
        lambda metric, _started_at: durations.append(metric),
    )
    handle = _RequestFailureHandle()
    provider = _Provider()
    lease = InvocationSandboxLease(
        settings=_settings(),
        operation_id="operation",
        provider=provider,  # type: ignore[arg-type]
        handle=handle,  # type: ignore[arg-type]
        manifest=_manifest(),
    )

    await lease.close()

    assert handle.delete_requests == 1
    assert handle.deleted == 0
    assert provider.deleted == ["sandbox"]
    assert handle.lifecycle_policies[-1] == SandboxLifecyclePolicy.create(
        auto_suspend_seconds=300,
        auto_suspend_mode="Disk",
        auto_delete_seconds=600,
    )
    assert HybridMetric.SANDBOX_DELETE_FALLBACKS in recorded
    assert HybridMetric.SANDBOX_DELETES in recorded
    assert HybridMetric.SANDBOX_DELETE_FAILURES not in recorded
    assert HybridMetric.SANDBOX_DELETE_DURATION in durations
    assert handle.closed == 1
    assert provider.closed == 1


@pytest.mark.asyncio
async def test_hung_delete_request_is_cancelled_before_provider_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _HangingRequestHandle(_Handle):
        def __init__(self) -> None:
            super().__init__()
            self.request_cancelled = False

        async def request_delete(self) -> None:
            self.delete_requests += 1
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.request_cancelled = True
                raise

    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools._DELETE_REQUEST_TIMEOUT_SECONDS",
        0.01,
    )
    handle = _HangingRequestHandle()
    provider = _Provider()
    lease = InvocationSandboxLease(
        settings=_settings(),
        operation_id="operation",
        provider=provider,  # type: ignore[arg-type]
        handle=handle,  # type: ignore[arg-type]
        manifest=_manifest(),
    )

    await lease.close()

    assert handle.request_cancelled is True
    assert provider.deleted == ["sandbox"]
    assert handle.closed == 1
    assert provider.closed == 1


@pytest.mark.asyncio
async def test_provider_fallback_preserves_completed_output_and_closes_resources() -> None:
    class _RequestFailureHandle(_Handle):
        async def request_delete(self) -> None:
            self.delete_requests += 1
            raise RuntimeError("request failed")

    handle = _RequestFailureHandle()
    provider = _Provider()
    lease = InvocationSandboxLease(
        settings=_settings(),
        operation_id="operation",
        provider=provider,  # type: ignore[arg-type]
        handle=handle,  # type: ignore[arg-type]
        manifest=_manifest(),
    )
    result = await lease.invoke(
        call_id="completed-call",
        tool_name="customer_probe",
        arguments={"message": "alpha"},
        deadline=asyncio.get_running_loop().time() + 30,
    )

    await lease.close()

    assert result.status is HybridInvocationStatus.SUCCESS
    assert result.value == {"sequence": 1}
    assert provider.deleted == ["sandbox"]
    assert handle.closed == 1
    assert provider.closed == 1


@pytest.mark.asyncio
async def test_provider_fallback_failure_leaves_lifecycle_and_reaper_backstops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RequestFailureHandle(_Handle):
        async def request_delete(self) -> None:
            self.delete_requests += 1
            raise RuntimeError("request failed")

    class _DeleteFailureProvider(_Provider):
        async def delete_sandbox(self, sandbox_id: str) -> None:
            self.deleted.append(sandbox_id)
            raise RuntimeError("provider delete failed")

    recorded: list[HybridMetric] = []
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools.record_hybrid_count",
        lambda metric: recorded.append(metric),
    )
    handle = _RequestFailureHandle()
    provider = _DeleteFailureProvider()
    lease = InvocationSandboxLease(
        settings=_settings(),
        operation_id="operation",
        provider=provider,  # type: ignore[arg-type]
        handle=handle,  # type: ignore[arg-type]
        manifest=_manifest(),
    )

    await lease.close()

    assert provider.deleted == ["sandbox"]
    assert handle.lifecycle_policies[-1] == SandboxLifecyclePolicy.create(
        auto_suspend_seconds=300,
        auto_suspend_mode="Disk",
        auto_delete_seconds=600,
    )
    assert HybridMetric.SANDBOX_DELETE_FALLBACKS in recorded
    assert HybridMetric.SANDBOX_DELETE_FAILURES in recorded
    assert handle.closed == 1
    assert provider.closed == 1


@pytest.mark.asyncio
async def test_hung_provider_fallback_is_bounded_and_records_overall_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RequestFailureHandle(_Handle):
        async def request_delete(self) -> None:
            raise RuntimeError("request failed")

    class _HangingProvider(_Provider):
        async def delete_sandbox(self, sandbox_id: str) -> None:
            self.deleted.append(sandbox_id)
            await asyncio.Event().wait()

    recorded: list[HybridMetric] = []
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools.record_hybrid_count",
        lambda metric: recorded.append(metric),
    )
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools._post_run_delete_seconds",
        lambda: 0.02,
    )
    handle = _RequestFailureHandle()
    provider = _HangingProvider()
    lease = InvocationSandboxLease(
        settings=_settings(),
        operation_id="operation",
        provider=provider,  # type: ignore[arg-type]
        handle=handle,  # type: ignore[arg-type]
        manifest=_manifest(),
    )
    started = asyncio.get_running_loop().time()

    await lease.close()

    assert asyncio.get_running_loop().time() - started < 0.2
    assert provider.deleted == ["sandbox"]
    assert HybridMetric.SANDBOX_DELETE_FALLBACKS in recorded
    assert HybridMetric.SANDBOX_DELETE_FAILURES in recorded
    assert handle.closed == 1
    assert provider.closed == 1


@pytest.mark.asyncio
async def test_successful_delete_request_is_not_reported_as_completed_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[HybridMetric] = []
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools.record_hybrid_count",
        lambda metric: recorded.append(metric),
    )
    handle = _Handle()
    provider = _Provider()
    lease = InvocationSandboxLease(
        settings=_settings(),
        operation_id="operation",
        provider=provider,  # type: ignore[arg-type]
        handle=handle,  # type: ignore[arg-type]
        manifest=_manifest(),
    )

    await lease.close()

    assert HybridMetric.SANDBOX_LIFECYCLE_HANDOFFS in recorded
    assert HybridMetric.SANDBOX_DELETE_REQUESTS_ACCEPTED in recorded
    assert HybridMetric.SANDBOX_DELETE_FALLBACKS not in recorded
    assert HybridMetric.SANDBOX_DELETES not in recorded
    assert provider.deleted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_at", ["request", "fallback"])
async def test_terminal_delete_not_found_is_success(
    missing_at: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _MissingHandle(_Handle):
        async def request_delete(self) -> None:
            self.delete_requests += 1
            if missing_at == "request":
                raise SandboxNotFoundError("already gone")
            raise RuntimeError("request failed")

    class _MissingProvider(_Provider):
        async def delete_sandbox(self, sandbox_id: str) -> None:
            self.deleted.append(sandbox_id)
            raise SandboxNotFoundError("already gone")

    recorded: list[HybridMetric] = []
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools.record_hybrid_count",
        lambda metric: recorded.append(metric),
    )
    handle = _MissingHandle()
    provider = _MissingProvider()
    lease = InvocationSandboxLease(
        settings=_settings(),
        operation_id="operation",
        provider=provider,  # type: ignore[arg-type]
        handle=handle,  # type: ignore[arg-type]
        manifest=_manifest(),
    )

    await lease.close()

    if missing_at == "request":
        assert provider.deleted == []
        assert HybridMetric.SANDBOX_DELETE_REQUESTS_ACCEPTED in recorded
        assert HybridMetric.SANDBOX_DELETE_FALLBACKS not in recorded
    else:
        assert provider.deleted == ["sandbox"]
        assert HybridMetric.SANDBOX_DELETE_FALLBACKS in recorded
        assert HybridMetric.SANDBOX_DELETES in recorded
    assert HybridMetric.SANDBOX_DELETE_FAILURES not in recorded


@pytest.mark.asyncio
async def test_hung_handle_delete_leaves_budget_for_provider_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _HangingDeleteHandle(_Handle):
        def __init__(self) -> None:
            super().__init__()
            self.delete_cancelled = False

        async def delete(self) -> None:
            self.deleted += 1
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.delete_cancelled = True
                raise

        async def set_lifecycle_policy(self, _policy: SandboxLifecyclePolicy) -> None:
            raise RuntimeError("terminal policy failed")

    recorded: list[HybridMetric] = []
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools.record_hybrid_count",
        lambda metric: recorded.append(metric),
    )
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools._POST_RUN_DELETE_TIMEOUT_SECONDS",
        0.3,
    )
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools._MIN_DELETE_ATTEMPT_SECONDS",
        0.05,
    )
    handle = _HangingDeleteHandle()
    provider = _Provider()
    lease = InvocationSandboxLease(
        settings=replace(
            _settings(),
            create_timeout_seconds=90,
            drain_timeout_seconds=0.3,
        ),
        operation_id="operation",
        provider=provider,  # type: ignore[arg-type]
        handle=handle,  # type: ignore[arg-type]
        manifest=_manifest(),
    )
    started = asyncio.get_running_loop().time()

    await lease.close()

    elapsed = asyncio.get_running_loop().time() - started
    assert handle.deleted == 1
    assert handle.delete_cancelled is True
    assert provider.deleted == ["sandbox"]
    assert handle.closed == 1
    assert provider.closed == 1
    assert HybridMetric.SANDBOX_DELETE_FAILURES not in recorded
    assert elapsed < 0.3 * _ROLLBACK_DELETE_ATTEMPTS


@pytest.mark.asyncio
async def test_rollback_hung_handle_delete_falls_back_within_bounded_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _HangingDeleteHandle(_Handle):
        async def delete(self) -> None:
            self.deleted += 1
            await asyncio.Event().wait()

    recorded: list[HybridMetric] = []
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools.record_hybrid_count",
        lambda metric: recorded.append(metric),
    )
    handle = _HangingDeleteHandle()
    provider = _Provider()
    started = asyncio.get_running_loop().time()

    await _best_effort_delete(
        handle,  # type: ignore[arg-type]
        provider,  # type: ignore[arg-type]
        timeout_seconds=0.3,
    )

    elapsed = asyncio.get_running_loop().time() - started
    assert handle.deleted == 1
    assert provider.deleted == ["sandbox"]
    assert HybridMetric.SANDBOX_DELETE_FAILURES not in recorded
    assert elapsed < 0.3 * _ROLLBACK_DELETE_ATTEMPTS


def test_delete_attempt_budget_reserves_a_slice_for_each_remaining_seam() -> None:
    assert _delete_attempt_seconds(90.0, _ROLLBACK_DELETE_ATTEMPTS) == 30.0
    assert _delete_attempt_seconds(60.0, 2) == 30.0
    assert _delete_attempt_seconds(30.0, 1) == 30.0
    assert (
        _delete_attempt_seconds(_ROLLBACK_DELETE_TIMEOUT_SECONDS, _ROLLBACK_DELETE_ATTEMPTS)
        < _ROLLBACK_DELETE_TIMEOUT_SECONDS
    )


def test_delete_attempt_budget_never_extends_past_the_remaining_time() -> None:
    for remaining in (0.003, 1.0, _MIN_DELETE_ATTEMPT_SECONDS, 11.0):
        for attempts_remaining in range(1, _ROLLBACK_DELETE_ATTEMPTS + 1):
            assert _delete_attempt_seconds(remaining, attempts_remaining) <= remaining
    assert _delete_attempt_seconds(0.003, _ROLLBACK_DELETE_ATTEMPTS) == pytest.approx(0.001)
    assert _delete_attempt_seconds(-5.0, _ROLLBACK_DELETE_ATTEMPTS) == 0.0
    assert _delete_attempt_seconds(-5.0, 1) == 0.0


def test_post_run_delete_budget_covers_observed_deletion_latency() -> None:
    budget = _post_run_delete_seconds()
    first_slice = _delete_attempt_seconds(budget, _ROLLBACK_DELETE_ATTEMPTS)

    assert budget == _POST_RUN_DELETE_TIMEOUT_SECONDS
    assert budget >= _MIN_DELETE_ATTEMPT_SECONDS * _ROLLBACK_DELETE_ATTEMPTS
    # Every seam must outlast one transport long-running-operation poll cycle.
    assert first_slice > _TRANSPORT_POLL_INTERVAL_SECONDS
    assert first_slice >= _MIN_DELETE_ATTEMPT_SECONDS
    # First slice covers the recorded clean-window maximum delete of 6.929 s,
    # and the whole budget covers the diagnostic maximum of 15.280 s.
    assert first_slice >= 6.929
    assert budget >= 15.280
    # Completed-run cleanup still stays far inside the failed-acquire rollback window.
    assert budget < _ROLLBACK_DELETE_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_post_run_delete_budget_is_independent_of_the_drain_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budgets: list[float] = []

    async def _capture(
        handle: object,
        provider: object,
        *,
        timeout_seconds: float = 0.0,
    ) -> None:
        budgets.append(timeout_seconds)

    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools._best_effort_delete",
        _capture,
    )
    class _FailedHandoffHandle(_Handle):
        async def set_lifecycle_policy(self, _policy: SandboxLifecyclePolicy) -> None:
            raise RuntimeError("terminal policy failed")

    provider = _Provider()
    lease = InvocationSandboxLease(
        settings=replace(_settings(), create_timeout_seconds=90, drain_timeout_seconds=0.01),
        operation_id="operation",
        provider=provider,  # type: ignore[arg-type]
        handle=_FailedHandoffHandle(),  # type: ignore[arg-type]
        manifest=_manifest(),
    )

    await lease.close()

    assert budgets == [_POST_RUN_DELETE_TIMEOUT_SECONDS]
    assert provider.closed == 1


def test_provisioning_labels_use_stable_app_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_hash = f"a1-{'a' * 52}"
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools.hybrid_app_hash",
        lambda: app_hash,
    )

    labels = _provisioning_labels("operation")

    assert labels.app_hash == app_hash


def test_provisioning_labels_fail_closed_without_stable_app_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in ("WEBSITE_OWNER_NAME", "WEBSITE_SITE_NAME", "WEBSITE_SLOT_NAME"):
        monkeypatch.delenv(variable, raising=False)

    with pytest.raises(AppIdentityResolutionError):
        _provisioning_labels("operation")


def test_provisioning_and_reaper_derive_the_same_app_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEBSITE_OWNER_NAME", f"{'0' * 8}-0000-0000-0000-{'0' * 12}+ws")
    monkeypatch.setenv("WEBSITE_SITE_NAME", "func-hybrid")
    monkeypatch.delenv("WEBSITE_SLOT_NAME", raising=False)

    assert _provisioning_labels("operation").app_hash == hybrid_app_hash()


@pytest.mark.asyncio
async def test_unexpected_cleanup_failure_is_recorded_without_propagating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_delete(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("cleanup exploded")

    recorded: list[HybridMetric] = []
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools._best_effort_delete",
        unexpected_delete,
    )
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools.record_hybrid_count",
        lambda metric: recorded.append(metric),
    )
    class _FailedHandoffHandle(_Handle):
        async def set_lifecycle_policy(self, _policy: SandboxLifecyclePolicy) -> None:
            raise RuntimeError("terminal policy failed")

    provider = _Provider()
    lease = InvocationSandboxLease(
        settings=_settings(),
        operation_id="operation",
        provider=provider,  # type: ignore[arg-type]
        handle=_FailedHandoffHandle(),  # type: ignore[arg-type]
        manifest=_manifest(),
    )

    await lease.close()

    assert recorded.count(HybridMetric.SANDBOX_DELETE_FAILURES) == 1
    assert provider.closed == 1


@pytest.mark.asyncio
async def test_tool_transport_failure_increments_failure_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingWriteHandle(_Handle):
        async def write_file(
            self, path: str, content: bytes, *, create_dirs: bool = False
        ) -> None:
            if "/requests/" in path:
                raise RuntimeError("transport failed")
            await super().write_file(path, content, create_dirs=create_dirs)

    recorded: list[HybridMetric] = []
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools.record_hybrid_count",
        lambda metric: recorded.append(metric),
    )
    lease = InvocationSandboxLease(
        settings=_settings(),
        operation_id="operation",
        provider=_Provider(),  # type: ignore[arg-type]
        handle=_FailingWriteHandle(),  # type: ignore[arg-type]
        manifest=_manifest(),
    )

    with pytest.raises(RuntimeError, match="transport failed"):
        await lease.invoke(
            call_id="failed",
            tool_name="customer_probe",
            arguments={"message": "a"},
            deadline=asyncio.get_running_loop().time() + 5,
        )

    assert recorded.count(HybridMetric.TOOL_FAILURES) == 1
    await lease.close()


@pytest.mark.asyncio
async def test_tool_deadline_failure_increments_failure_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[HybridMetric] = []
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools.record_hybrid_count",
        lambda metric: recorded.append(metric),
    )
    lease = InvocationSandboxLease(
        settings=_settings(),
        operation_id="operation",
        provider=_Provider(),  # type: ignore[arg-type]
        handle=_Handle(),  # type: ignore[arg-type]
        manifest=_manifest(),
    )

    with pytest.raises(TimeoutError, match="deadline elapsed"):
        await lease.invoke(
            call_id="expired",
            tool_name="customer_probe",
            arguments={"message": "a"},
            deadline=asyncio.get_running_loop().time() - 1,
        )

    assert recorded.count(HybridMetric.TOOL_CALLS) == 1
    assert recorded.count(HybridMetric.TOOL_FAILURES) == 1
    await lease.close()


@pytest.mark.asyncio
async def test_tool_protocol_failure_increments_failure_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _InvalidResultHandle(_Handle):
        async def write_file(
            self, path: str, content: bytes, *, create_dirs: bool = False
        ) -> None:
            await super().write_file(path, content, create_dirs=create_dirs)
            if "/requests/" in path:
                self.results[path.replace("/requests/", "/results/")] = b"{}"

    recorded: list[HybridMetric] = []
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools.record_hybrid_count",
        lambda metric: recorded.append(metric),
    )
    lease = InvocationSandboxLease(
        settings=_settings(),
        operation_id="operation",
        provider=_Provider(),  # type: ignore[arg-type]
        handle=_InvalidResultHandle(),  # type: ignore[arg-type]
        manifest=_manifest(),
    )

    with pytest.raises(ValueError):
        await lease.invoke(
            call_id="invalid",
            tool_name="customer_probe",
            arguments={"message": "a"},
            deadline=asyncio.get_running_loop().time() + 5,
        )

    assert recorded.count(HybridMetric.TOOL_FAILURES) == 1
    await lease.close()


@pytest.mark.asyncio
async def test_tool_cancellation_does_not_increment_failure_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BlockingWriteHandle(_Handle):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()

        async def write_file(
            self, path: str, content: bytes, *, create_dirs: bool = False
        ) -> None:
            if "/requests/" in path:
                self.started.set()
                await asyncio.Event().wait()
            await super().write_file(path, content, create_dirs=create_dirs)

    recorded: list[HybridMetric] = []
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools.record_hybrid_count",
        lambda metric: recorded.append(metric),
    )
    handle = _BlockingWriteHandle()
    lease = InvocationSandboxLease(
        settings=_settings(),
        operation_id="operation",
        provider=_Provider(),  # type: ignore[arg-type]
        handle=handle,  # type: ignore[arg-type]
        manifest=_manifest(),
    )
    invocation = asyncio.create_task(
        lease.invoke(
            call_id="cancelled",
            tool_name="customer_probe",
            arguments={"message": "a"},
            deadline=asyncio.get_running_loop().time() + 5,
        )
    )
    await handle.started.wait()

    invocation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await invocation

    assert HybridMetric.TOOL_FAILURES not in recorded
    await lease.close()


class _ExhaustedDeleteHandle(_Handle):
    async def set_lifecycle_policy(self, _policy: SandboxLifecyclePolicy) -> None:
        raise RuntimeError("terminal policy failed")

    async def delete(self) -> None:
        self.deleted += 1
        raise RuntimeError("handle delete failed")


class _ExhaustedDeleteProvider(_Provider):
    async def delete_sandbox(self, sandbox_id: str) -> None:
        self.deleted.append(sandbox_id)
        raise RuntimeError("provider delete failed")


def _exhausted_cleanup_lease() -> tuple[
    InvocationSandboxLease,
    _ExhaustedDeleteHandle,
    _ExhaustedDeleteProvider,
]:
    handle = _ExhaustedDeleteHandle()
    provider = _ExhaustedDeleteProvider()
    lease = InvocationSandboxLease(
        settings=replace(
            _settings(),
            create_timeout_seconds=0.01,
            drain_timeout_seconds=0.05,
        ),
        operation_id="operation",
        provider=provider,  # type: ignore[arg-type]
        handle=handle,  # type: ignore[arg-type]
        manifest=_manifest(),
    )
    return lease, handle, provider


@pytest.mark.asyncio
async def test_post_run_delete_failure_does_not_replace_nonstream_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease, handle, provider = _exhausted_cleanup_lease()

    async def acquire(
        _settings: HybridSandboxSettings,
        **_kwargs: object,
    ) -> InvocationSandboxLease:
        return lease

    async def run_in_invocation(_prompt: str, **_kwargs: object) -> str:
        return "completed"

    monkeypatch.setenv(HYBRID_SANDBOX_GROUP_ENV, "group")
    monkeypatch.setenv(HYBRID_SANDBOX_REGION_ENV, "westus2")
    monkeypatch.setattr(InvocationSandboxLease, "acquire", acquire)
    monkeypatch.setattr(runner, "_run_agent_in_invocation", run_in_invocation)

    result = await runner.run_agent("prompt", timeout=30, tools=[], mcp_tools=[])

    assert result == "completed"
    assert handle.deleted == 1
    assert provider.deleted == ["sandbox"] * (_ROLLBACK_DELETE_ATTEMPTS - 1)
    assert handle.closed == 1
    assert provider.closed == 1


@pytest.mark.asyncio
async def test_post_run_delete_failure_does_not_append_error_after_stream_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease, handle, provider = _exhausted_cleanup_lease()

    async def acquire(
        _settings: HybridSandboxSettings,
        **_kwargs: object,
    ) -> InvocationSandboxLease:
        return lease

    async def stream_in_invocation(
        _prompt: str,
        **_kwargs: object,
    ) -> object:
        yield {"type": "done"}

    monkeypatch.setenv(HYBRID_SANDBOX_GROUP_ENV, "group")
    monkeypatch.setenv(HYBRID_SANDBOX_REGION_ENV, "westus2")
    monkeypatch.setattr(InvocationSandboxLease, "acquire", acquire)
    monkeypatch.setattr(
        runner,
        "_run_agent_event_stream_in_invocation",
        stream_in_invocation,
    )

    events = [
        event
        async for event in runner.run_agent_events(
            "prompt",
            timeout=30,
            tools=[],
            mcp_tools=[],
        )
    ]

    assert events == [{"type": "done"}]
    assert handle.deleted == 1
    assert provider.deleted == ["sandbox"] * (_ROLLBACK_DELETE_ATTEMPTS - 1)
    assert handle.closed == 1
    assert provider.closed == 1


@pytest.mark.asyncio
async def test_stream_disconnect_exits_outer_hybrid_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_exited = False
    inner_exited = False

    @asynccontextmanager
    async def fake_open_hybrid_invocation(
        **_kwargs: object,
    ) -> object:
        nonlocal lease_exited
        try:
            yield HybridPreparedInvocation(tools=[], middleware=[])
        finally:
            lease_exited = True

    async def fake_inner_stream(
        _prompt: str,
        **_kwargs: object,
    ) -> object:
        nonlocal inner_exited
        try:
            yield {"type": "text", "content": "first"}
            await asyncio.Event().wait()
        finally:
            inner_exited = True

    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_tools.open_hybrid_invocation",
        fake_open_hybrid_invocation,
    )
    monkeypatch.setattr(
        runner,
        "_run_agent_event_stream_in_invocation",
        fake_inner_stream,
    )
    stream = runner.run_agent_events("prompt", tools=[])

    assert await anext(stream) == {"type": "text", "content": "first"}
    await stream.aclose()

    assert inner_exited
    assert lease_exited


@pytest.mark.asyncio
async def test_stream_disconnect_requests_terminal_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = _Handle()
    provider = _Provider()
    lease = InvocationSandboxLease(
        settings=_settings(),
        operation_id="operation",
        provider=provider,  # type: ignore[arg-type]
        handle=handle,  # type: ignore[arg-type]
        manifest=_manifest(),
    )

    async def acquire(
        _settings: HybridSandboxSettings,
        **_kwargs: object,
    ) -> InvocationSandboxLease:
        return lease

    async def fake_inner_stream(
        _prompt: str,
        **_kwargs: object,
    ) -> object:
        yield {"type": "text", "content": "first"}
        await asyncio.Event().wait()

    monkeypatch.setenv(HYBRID_SANDBOX_GROUP_ENV, "group")
    monkeypatch.setenv(HYBRID_SANDBOX_REGION_ENV, "westus2")
    monkeypatch.setattr(InvocationSandboxLease, "acquire", acquire)
    monkeypatch.setattr(
        runner,
        "_run_agent_event_stream_in_invocation",
        fake_inner_stream,
    )
    stream = runner.run_agent_events("prompt", timeout=30, tools=[], mcp_tools=[])

    assert await anext(stream) == {"type": "text", "content": "first"}
    await stream.aclose()

    assert handle.delete_requests == 1
    assert handle.deleted == 0
    assert handle.lifecycle_policies[-1].auto_suspend_seconds == 300
    assert handle.closed == 1
    assert provider.closed == 1
