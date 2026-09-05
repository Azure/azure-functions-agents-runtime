"""Invocation-scoped ACA Sandbox lease and MAF tool middleware."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import shlex
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from agent_framework import (
    FunctionInvocationContext,
    FunctionMiddleware,
    FunctionTool,
)
from pydantic import BaseModel

from .._logger import logger
from .._observability import current_operation_id, start_span
from ..config.paths import get_app_root
from ..controller.package import CapturedContentPackage, get_content_package
from ..egress.policy import MAX_EGRESS_POLICY_RULES
from ..harness import SANDBOX_MARKER_ENV_VAR
from ..transport.ports import (
    SandboxSessionHandle,
    SandboxSessionProvider,
)
from ..transport.transport_models import (
    DiskSource,
    SandboxCreateRequest,
    SandboxEgressHostRule,
    SandboxEgressPolicy,
    SandboxFileNotFoundError,
    SandboxGroupBinding,
    SandboxLifecyclePolicy,
    SandboxNotFoundError,
    SandboxProvisioningLabels,
)
from . import hybrid_executor
from .hybrid_config import (
    HybridSandboxSettings,
    hybrid_enabled,
    validate_hybrid_runner_inputs,
)
from .hybrid_observability import (
    HybridMetric,
    HybridProgressPhase,
    HybridProgressStatus,
    record_hybrid_count,
    record_hybrid_duration,
    record_hybrid_progress,
    record_hybrid_value,
)
from .hybrid_protocol import (
    HYBRID_TOOL_MANIFEST_FILENAME,
    HYBRID_TOOL_PACKAGE_VERIFICATION_FILENAME,
    HYBRID_TOOL_PID_FILENAME,
    HYBRID_TOOL_PROTOCOL_VERSION,
    HYBRID_TOOL_READINESS_FILENAME,
    HYBRID_TOOL_REQUEST_DIRECTORY,
    HYBRID_TOOL_RESULT_DIRECTORY,
    HYBRID_TOOL_SHUTDOWN_FILENAME,
    HYBRID_TOOL_STARTUP_FAILURE_FILENAME,
    HybridInvocationStatus,
    HybridToolDescriptor,
    HybridToolInvocationRequest,
    HybridToolInvocationResult,
    HybridToolManifest,
    canonical_hybrid_json_bytes,
    parse_hybrid_tool_manifest,
    parse_hybrid_tool_result,
)
from .hybrid_reaper import HYBRID_OWNER_KIND, hybrid_app_hash

_HYBRID_ROOT = "/tmp/azure-functions-agents-runtime/hybrid"
_APP_ZIP_PATH = f"{_HYBRID_ROOT}/app.zip"
_EXECUTOR_PATH = f"{_HYBRID_ROOT}/hybrid_executor.py"
_EXTRACTION_PATH = f"{_HYBRID_ROOT}/application"
_JOURNAL_PATH = f"{_HYBRID_ROOT}/journal"
_WORKSPACE_PATH = f"{_HYBRID_ROOT}/workspace"
_EXECUTOR_LOG_PATH = f"{_HYBRID_ROOT}/executor.log"
_POLL_INTERVAL_SECONDS = 0.1
_MAX_TOOL_SECONDS = 30.0
_ACTIVE_CREATE_AUTO_SUSPEND_SECONDS = 3600
_ACTIVE_AUTO_DELETE_SECONDS = 600
_TERMINAL_AUTO_SUSPEND_SECONDS = 300
_TERMINAL_AUTO_DELETE_SECONDS = 600
_DELETE_REQUEST_TIMEOUT_SECONDS = 5.0
_ROLLBACK_DELETE_TIMEOUT_SECONDS = 90.0
_ROLLBACK_DELETE_ATTEMPTS = 3
# Mirrors ``_CONTROL_OPERATION_POLL_INTERVAL_SECONDS`` in ``transport/aca_sdk``;
# a delete attempt shorter than one poll cycle can never observe a terminal state.
_TRANSPORT_POLL_INTERVAL_SECONDS = 3.0
# Every seam must clear one poll cycle with headroom over the recorded delete
# range (clean-window average 3.793 s, maximum 6.929 s; diagnostic p95 14.381 s,
# maximum 15.280 s; live reaper delete 8.475 s).
_MIN_DELETE_ATTEMPT_SECONDS = 4.0
_POST_RUN_DELETE_TIMEOUT_SECONDS = 24.0
_EXECUTOR_EXCEPTION_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")


class ToolExecutionBackend(Protocol):
    """Narrow backend used only by local MAF function middleware."""

    async def invoke(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: Mapping[str, object],
        deadline: float,
    ) -> HybridToolInvocationResult:
        """Invoke one idempotent local tool before the monotonic deadline."""


@dataclass(frozen=True, slots=True)
class HybridPreparedInvocation:
    """Runner inputs produced for one top-level invocation."""

    tools: list[Any] | None
    middleware: list[Any] | None


class HybridToolMiddleware(FunctionMiddleware):
    """Route exact runtime-owned stubs and call next for framework/MCP tools."""

    def __init__(
        self,
        backend: ToolExecutionBackend,
        local_tools: Sequence[FunctionTool],
        *,
        deadline: float,
    ) -> None:
        self._backend = backend
        self._local_tool_ids = frozenset(id(tool) for tool in local_tools)
        self._deadline = deadline

    async def process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        if id(context.function) not in self._local_tool_ids:
            await call_next()
            return
        call_id_value = context.metadata.get("call_id")
        call_id = (
            call_id_value
            if isinstance(call_id_value, str) and call_id_value
            else uuid.uuid4().hex
        )
        arguments = (
            context.arguments.model_dump(mode="json")
            if isinstance(context.arguments, BaseModel)
            else dict(context.arguments)
        )
        result = await self._backend.invoke(
            call_id=call_id,
            tool_name=context.function.name,
            arguments=arguments,
            deadline=self._deadline,
        )
        if result.status is HybridInvocationStatus.ERROR:
            assert result.error is not None
            raise RuntimeError(f"{result.error.code.value}: {result.error.message}")
        context.result = {
            "value": result.value,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        }


class InvocationSandboxLease(ToolExecutionBackend):
    """One fresh ACA Sandbox shared only by one top-level MAF invocation."""

    def __init__(
        self,
        *,
        settings: HybridSandboxSettings,
        operation_id: str,
        provider: SandboxSessionProvider,
        handle: SandboxSessionHandle,
        manifest: HybridToolManifest,
    ) -> None:
        self._settings = settings
        self._operation_id = operation_id
        self._provider = provider
        self._handle = handle
        self._manifest = manifest
        self._admitting = True
        self._active_calls = 0
        self._active_condition = asyncio.Condition()
        self._queue_lock = asyncio.Lock()
        self._closed = False

    @classmethod
    async def acquire(
        cls,
        settings: HybridSandboxSettings,
        *,
        maximum_run_seconds: float | None = None,
        provider_factory: Callable[[], Awaitable[SandboxSessionProvider]] | None = None,
        package_factory: Callable[[Path], Awaitable[CapturedContentPackage]] = get_content_package,
    ) -> InvocationSandboxLease:
        """Create, package, start, and discover one invocation sandbox."""
        operation_id = current_operation_id() or uuid.uuid4().hex
        _validate_active_lifecycle_bound(
            settings,
            maximum_run_seconds=maximum_run_seconds,
        )
        factory = provider_factory or _provider_factory(
            settings.group_resource_id,
            settings.region,
        )
        provider = await factory()
        handle: SandboxSessionHandle | None = None
        try:
            create_started = time.perf_counter()
            record_hybrid_count(HybridMetric.SANDBOX_CREATES)
            record_hybrid_progress(
                HybridProgressPhase.SANDBOX_CREATE,
                HybridProgressStatus.STARTED,
            )
            labels = _provisioning_labels(operation_id)
            request = _create_request(settings, labels)
            persisted_group = SandboxGroupBinding.create(
                provider.group.resource_id,
                provider.group.region,
            )
            try:
                handle = await asyncio.wait_for(
                    provider.create(request, persisted_group=persisted_group),
                    timeout=settings.create_timeout_seconds,
                )
            except asyncio.CancelledError:
                record_hybrid_progress(
                    HybridProgressPhase.SANDBOX_CREATE,
                    HybridProgressStatus.CANCELLED,
                )
                record_hybrid_count(HybridMetric.SANDBOX_CREATE_FAILURES)
                raise
            except BaseException:
                record_hybrid_progress(
                    HybridProgressPhase.SANDBOX_CREATE,
                    HybridProgressStatus.FAILED,
                )
                record_hybrid_count(HybridMetric.SANDBOX_CREATE_FAILURES)
                raise
            else:
                record_hybrid_progress(
                    HybridProgressPhase.SANDBOX_CREATE,
                    HybridProgressStatus.COMPLETED,
                    duration_seconds=time.perf_counter() - create_started,
                )
            finally:
                record_hybrid_duration(
                    HybridMetric.SANDBOX_CREATE_DURATION,
                    create_started,
                )
            await handle.set_lifecycle_policy(
                SandboxLifecyclePolicy.create(
                    auto_suspend_seconds=None,
                    auto_delete_seconds=_ACTIVE_AUTO_DELETE_SECONDS,
                )
            )
            package = await package_factory(_hybrid_package_root(settings))
            await _deliver_executor(handle, package)
            manifest = await _start_and_discover(
                handle,
                settings.ready_timeout_seconds,
                package.digest,
            )
            return cls(
                settings=settings,
                operation_id=operation_id,
                provider=provider,
                handle=handle,
                manifest=manifest,
            )
        except BaseException:
            if handle is not None:
                await _best_effort_delete(handle, provider)
            await provider.close()
            raise

    def build_tools(self, *, deadline: float) -> tuple[list[FunctionTool], HybridToolMiddleware]:
        """Build executable fail-closed stubs plus exact-identity middleware."""
        tools = [_build_stub(descriptor) for descriptor in self._manifest.tools]
        return tools, HybridToolMiddleware(self, tools, deadline=deadline)

    async def invoke(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: Mapping[str, object],
        deadline: float,
    ) -> HybridToolInvocationResult:
        """Transfer one request/result through the serialized file journal."""
        async with self._active_condition:
            if not self._admitting:
                raise RuntimeError("Hybrid invocation is no longer accepting tool calls.")
            self._active_calls += 1
        queued_at = time.perf_counter()
        tool_started = queued_at
        record_hybrid_progress(
            HybridProgressPhase.TOOL_EXECUTION,
            HybridProgressStatus.STARTED,
        )
        try:
            async with self._queue_lock:
                queue_seconds = time.perf_counter() - queued_at
                record_hybrid_count(HybridMetric.TOOL_CALLS)
                remaining = _remaining_tool_seconds(deadline)
                transfer_started = time.perf_counter()
                request = HybridToolInvocationRequest(
                    protocol_version=HYBRID_TOOL_PROTOCOL_VERSION,
                    call_id=call_id,
                    tool_name=tool_name,
                    arguments=dict(arguments),
                    deadline_unix_seconds=time.time() + min(remaining, _MAX_TOOL_SECONDS),
                    traceparent=_current_traceparent(),
                    operation_id=self._operation_id,
                )
                request_path = (
                    f"{_JOURNAL_PATH}/{HYBRID_TOOL_REQUEST_DIRECTORY}/{call_id}.json"
                )
                result_path = (
                    f"{_JOURNAL_PATH}/{HYBRID_TOOL_RESULT_DIRECTORY}/{call_id}.json"
                )
                await self._handle.write_file(
                    request_path,
                    canonical_hybrid_json_bytes(request),
                    create_dirs=True,
                )
                result = await _poll_result(
                    self._handle,
                    result_path,
                    timeout_seconds=min(remaining, _MAX_TOOL_SECONDS),
                )
                record_hybrid_duration(HybridMetric.TOOL_TRANSFER_DURATION, transfer_started)
                record_hybrid_value(
                    HybridMetric.TOOL_EXECUTION_DURATION,
                    result.timings.execution_ms / 1000.0,
                )
                record_hybrid_value(
                    HybridMetric.TOOL_QUEUE_DURATION,
                    queue_seconds + (result.timings.queue_wait_ms / 1000.0),
                )
                if result.status is HybridInvocationStatus.ERROR:
                    record_hybrid_count(HybridMetric.TOOL_FAILURES)
                    record_hybrid_progress(
                        HybridProgressPhase.TOOL_EXECUTION,
                        HybridProgressStatus.FAILED,
                        duration_seconds=time.perf_counter() - tool_started,
                    )
                else:
                    record_hybrid_progress(
                        HybridProgressPhase.TOOL_EXECUTION,
                        HybridProgressStatus.COMPLETED,
                        duration_seconds=time.perf_counter() - tool_started,
                    )
                return result
        except asyncio.CancelledError:
            record_hybrid_progress(
                HybridProgressPhase.TOOL_EXECUTION,
                HybridProgressStatus.CANCELLED,
                duration_seconds=time.perf_counter() - tool_started,
            )
            raise
        except Exception:
            record_hybrid_progress(
                HybridProgressPhase.TOOL_EXECUTION,
                HybridProgressStatus.FAILED,
                duration_seconds=time.perf_counter() - tool_started,
            )
            record_hybrid_count(HybridMetric.TOOL_FAILURES)
            raise
        finally:
            async with self._active_condition:
                self._active_calls -= 1
                self._active_condition.notify_all()

    async def close(self, *, cancelled: bool = False) -> None:
        """Stop admissions and hand terminal cleanup to ACA without polling delete."""
        if self._closed:
            return
        self._closed = True
        progress_started = time.perf_counter()
        record_hybrid_progress(
            HybridProgressPhase.CLEANUP_HANDOFF,
            HybridProgressStatus.STARTED,
        )
        cleanup_status = (
            HybridProgressStatus.CANCELLED
            if cancelled
            else HybridProgressStatus.COMPLETED
        )
        try:
            handoff_ready = await self._prepare_terminal_handoff()
            if handoff_ready:
                handoff_ready = await self._apply_terminal_lifecycle()
            await self._finish_terminal_cleanup(
                confirmed_delete_required=not handoff_ready,
                progress_started=progress_started,
            )
        except asyncio.CancelledError:
            cleanup_status = HybridProgressStatus.CANCELLED
            raise
        finally:
            try:
                await self._provider.close()
            except Exception:
                logger.warning("Hybrid sandbox provider close failed.", exc_info=True)
            record_hybrid_progress(
                HybridProgressPhase.CLEANUP_COMPLETE,
                cleanup_status,
            )

    async def _prepare_terminal_handoff(self) -> bool:
        async with self._active_condition:
            self._admitting = False
            try:
                await asyncio.wait_for(
                    self._active_condition.wait_for(lambda: self._active_calls == 0),
                    timeout=self._settings.drain_timeout_seconds,
                )
            except TimeoutError:
                record_hybrid_count(HybridMetric.SANDBOX_LIFECYCLE_HANDOFF_FAILURES)
                logger.warning(
                    "Hybrid sandbox drain timed out; falling back to confirmed deletion."
                )
                return False
        try:
            await self._handle.write_file(
                f"{_JOURNAL_PATH}/{HYBRID_TOOL_SHUTDOWN_FILENAME}",
                b"",
                create_dirs=True,
            )
        except Exception:
            record_hybrid_count(HybridMetric.SANDBOX_LIFECYCLE_HANDOFF_FAILURES)
            logger.warning("Hybrid executor shutdown signal failed.", exc_info=True)
            return False
        return True

    async def _apply_terminal_lifecycle(self) -> bool:
        policy_started = time.perf_counter()
        try:
            await self._handle.set_lifecycle_policy(_terminal_lifecycle_policy())
        except SandboxNotFoundError:
            record_hybrid_count(HybridMetric.SANDBOX_LIFECYCLE_HANDOFFS)
            return True
        except Exception:
            record_hybrid_count(HybridMetric.SANDBOX_LIFECYCLE_HANDOFF_FAILURES)
            logger.warning(
                "Hybrid sandbox terminal lifecycle handoff failed; "
                "falling back to confirmed deletion.",
                exc_info=True,
            )
            return False
        else:
            record_hybrid_count(HybridMetric.SANDBOX_LIFECYCLE_HANDOFFS)
            return True
        finally:
            record_hybrid_duration(
                HybridMetric.SANDBOX_LIFECYCLE_HANDOFF_DURATION,
                policy_started,
            )

    async def _finish_terminal_cleanup(
        self,
        *,
        confirmed_delete_required: bool,
        progress_started: float,
    ) -> None:
        status = (
            HybridProgressStatus.FAILED
            if confirmed_delete_required
            else HybridProgressStatus.COMPLETED
        )
        record_hybrid_progress(
            HybridProgressPhase.CLEANUP_HANDOFF,
            status,
            duration_seconds=time.perf_counter() - progress_started,
        )
        if confirmed_delete_required:
            await self._confirmed_post_run_delete()
        else:
            await self._request_terminal_delete()

    async def _request_terminal_delete(self) -> None:
        try:
            await asyncio.wait_for(
                self._handle.request_delete(),
                timeout=_DELETE_REQUEST_TIMEOUT_SECONDS,
            )
        except SandboxNotFoundError:
            record_hybrid_count(HybridMetric.SANDBOX_DELETE_REQUESTS_ACCEPTED)
        except Exception:
            record_hybrid_count(HybridMetric.SANDBOX_DELETE_FALLBACKS)
            logger.warning(
                "Hybrid sandbox delete request was not accepted; "
                "trying bounded exact-ID provider deletion.",
                exc_info=True,
            )
            await self._provider_delete_after_request_failure()
        else:
            record_hybrid_count(HybridMetric.SANDBOX_DELETE_REQUESTS_ACCEPTED)
        finally:
            try:
                await self._handle.close()
            except Exception:
                logger.warning("Hybrid sandbox handle close failed.", exc_info=True)

    async def _provider_delete_after_request_failure(self) -> None:
        started = time.perf_counter()
        try:
            await asyncio.wait_for(
                self._provider.delete_sandbox(self._handle.identity.sandbox_id),
                timeout=_post_run_delete_seconds(),
            )
        except SandboxNotFoundError:
            record_hybrid_count(HybridMetric.SANDBOX_DELETES)
        except Exception:
            record_hybrid_count(HybridMetric.SANDBOX_DELETE_FAILURES)
            logger.error(
                "Hybrid sandbox exact-ID provider deletion failed; "
                "terminal lifecycle and reaper backstops remain armed.",
                exc_info=True,
            )
        else:
            record_hybrid_count(HybridMetric.SANDBOX_DELETES)
        finally:
            record_hybrid_duration(HybridMetric.SANDBOX_DELETE_DURATION, started)

    async def _confirmed_post_run_delete(self) -> None:
        try:
            await _best_effort_delete(
                self._handle,
                self._provider,
                timeout_seconds=_post_run_delete_seconds(),
            )
        except Exception:
            record_hybrid_count(HybridMetric.SANDBOX_DELETE_FAILURES)
            logger.error(
                "Hybrid sandbox cleanup failed after the run completed.",
                exc_info=True,
            )


@asynccontextmanager
async def open_hybrid_invocation(
    *,
    timeout_seconds: float,
    tools: list[Any] | None,
    sandbox_tools: list[Any] | None,
    skill_paths: list[Path] | None,
    web_request_tools: list[Any] | None,
    workflow_enabled: bool,
    subagents: Sequence[object] | None,
) -> AsyncIterator[HybridPreparedInvocation]:
    """Yield unchanged inputs or one invocation-scoped sandbox tool set."""
    if not hybrid_enabled():
        yield HybridPreparedInvocation(tools=tools, middleware=None)
        return
    validate_hybrid_runner_inputs(
        tools=tools,
        sandbox_tools=sandbox_tools,
        skill_paths=skill_paths,
        web_request_tools=web_request_tools,
        workflow_enabled=workflow_enabled,
        subagents=subagents,
    )
    settings = HybridSandboxSettings.from_environment()
    assert settings is not None
    settings.validate_reaper_bound(timeout_seconds)
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    request_started = time.perf_counter()
    record_hybrid_count(HybridMetric.REQUESTS)
    with start_span("hybrid.invocation"):
        try:
            lease = await InvocationSandboxLease.acquire(
                settings,
                maximum_run_seconds=timeout_seconds,
            )
            local_tools, middleware = lease.build_tools(deadline=deadline)
            invocation_cancelled = False
            try:
                yield HybridPreparedInvocation(
                    tools=list(local_tools),
                    middleware=[middleware],
                )
            except asyncio.CancelledError:
                invocation_cancelled = True
                raise
            finally:
                await lease.close(cancelled=invocation_cancelled)
        except asyncio.CancelledError:
            record_hybrid_count(HybridMetric.REQUEST_FAILURES)
            raise
        except BaseException:
            record_hybrid_count(HybridMetric.REQUEST_FAILURES)
            raise
        finally:
            record_hybrid_duration(HybridMetric.REQUEST_DURATION, request_started)


def _provider_factory(
    group_resource_id: str,
    region: str,
) -> Callable[[], Awaitable[SandboxSessionProvider]]:
    async def open_provider() -> SandboxSessionProvider:
        from ..transport.aca_sdk import AcaSandboxAdapter

        return await AcaSandboxAdapter.open(group_resource_id, region=region)

    return open_provider


def _hybrid_package_root(settings: HybridSandboxSettings) -> Path:
    return settings.tool_bundle_root or get_app_root()


def _create_request(
    settings: HybridSandboxSettings,
    labels: SandboxProvisioningLabels,
) -> SandboxCreateRequest:
    if len(settings.allowed_hosts) > MAX_EGRESS_POLICY_RULES:
        raise RuntimeError("Hybrid egress allowlist exceeds the supported rule limit.")
    egress = SandboxEgressPolicy.create(
        default_action="Deny",
        traffic_inspection="Full",
        host_rules=tuple(
            SandboxEgressHostRule.create(host=host, action="Allow")
            for host in settings.allowed_hosts
        ),
    )
    return SandboxCreateRequest.create(
        source=DiskSource.create(settings.sandbox_disk),
        labels=labels,
        remaining_setup_budget_seconds=settings.create_timeout_seconds,
        auto_suspend_seconds=_ACTIVE_CREATE_AUTO_SUSPEND_SECONDS,
        environment={SANDBOX_MARKER_ENV_VAR: "1"},
        egress_policy=egress,
        ports=(),
        skip_egress_proxy=False,
    )


def _terminal_lifecycle_policy() -> SandboxLifecyclePolicy:
    return SandboxLifecyclePolicy.create(
        auto_suspend_seconds=_TERMINAL_AUTO_SUSPEND_SECONDS,
        auto_suspend_mode="Disk",
        auto_delete_seconds=_TERMINAL_AUTO_DELETE_SECONDS,
    )


def _validate_active_lifecycle_bound(
    settings: HybridSandboxSettings,
    *,
    maximum_run_seconds: float | None,
) -> None:
    if maximum_run_seconds is None:
        return
    required_seconds = maximum_run_seconds + settings.create_timeout_seconds
    if required_seconds < _ACTIVE_CREATE_AUTO_SUSPEND_SECONDS:
        return
    raise RuntimeError(
        "Hybrid active sandbox policy does not cover the create and run bound."
    )


def _provisioning_labels(operation_id: str) -> SandboxProvisioningLabels:
    digest = hashlib.sha256(operation_id.encode("ascii")).hexdigest()
    return SandboxProvisioningLabels.create(
        owner_hash_version="h1",
        owner_kind=HYBRID_OWNER_KIND,
        owner_hash=f"h1-{digest[:52]}",
        app_hash=hybrid_app_hash(),
        session_id=f"hybrid-{uuid.uuid4().hex}",
        operation_label=uuid.uuid4().hex,
    )


async def _deliver_executor(
    handle: SandboxSessionHandle,
    package: CapturedContentPackage,
) -> None:
    started = time.perf_counter()
    record_hybrid_progress(
        HybridProgressPhase.PACKAGE_UPLOAD,
        HybridProgressStatus.STARTED,
    )
    source_path = Path(hybrid_executor.__file__ or "")
    source = source_path.read_bytes()
    try:
        await handle.write_file(_APP_ZIP_PATH, package.archive_bytes, create_dirs=True)
        await handle.write_file(_EXECUTOR_PATH, source, create_dirs=True)
        _validate_executor_delivery(await handle.read_file(_EXECUTOR_PATH), source)
    except asyncio.CancelledError:
        record_hybrid_progress(
            HybridProgressPhase.PACKAGE_UPLOAD,
            HybridProgressStatus.CANCELLED,
        )
        raise
    except Exception:
        record_hybrid_progress(
            HybridProgressPhase.PACKAGE_UPLOAD,
            HybridProgressStatus.FAILED,
        )
        raise
    record_hybrid_progress(
        HybridProgressPhase.PACKAGE_UPLOAD,
        HybridProgressStatus.COMPLETED,
        duration_seconds=time.perf_counter() - started,
    )
    record_hybrid_duration(HybridMetric.PACKAGE_UPLOAD_DURATION, started)


def _validate_executor_delivery(observed: bytes, expected: bytes) -> None:
    if observed != expected:
        raise RuntimeError("Hybrid executor verification failed.")


async def _start_and_discover(
    handle: SandboxSessionHandle,
    timeout_seconds: float,
    app_digest: str,
) -> HybridToolManifest:
    started = time.perf_counter()
    record_hybrid_progress(
        HybridProgressPhase.PACKAGE_VERIFY,
        HybridProgressStatus.STARTED,
    )
    command = " ".join(
        (
            "nohup",
            "python3",
            "-E",
            "-S",
            shlex.quote(_EXECUTOR_PATH),
            "--app-zip",
            shlex.quote(_APP_ZIP_PATH),
            "--app-digest",
            shlex.quote(app_digest),
            "--extraction-root",
            shlex.quote(_EXTRACTION_PATH),
            "--journal-root",
            shlex.quote(_JOURNAL_PATH),
            "--workspace-root",
            shlex.quote(_WORKSPACE_PATH),
            f">{shlex.quote(_EXECUTOR_LOG_PATH)}",
            "2>&1",
            "</dev/null",
            "&",
            "echo",
            "$!",
        )
    )
    result = await handle.exec(command, timeout_seconds=min(timeout_seconds, 10.0))
    if result.exit_code != 0:
        raise RuntimeError("Hybrid executor launch failed.")
    executor_pid = result.stdout.strip()
    if not executor_pid.isascii() or not executor_pid.isdigit():
        raise RuntimeError("Hybrid executor launch returned invalid process metadata.")
    readiness_path = f"{_JOURNAL_PATH}/{HYBRID_TOOL_READINESS_FILENAME}"
    verification_path = (
        f"{_JOURNAL_PATH}/{HYBRID_TOOL_PACKAGE_VERIFICATION_FILENAME}"
    )
    failure_path = f"{_JOURNAL_PATH}/{HYBRID_TOOL_STARTUP_FAILURE_FILENAME}"
    manifest_path = f"{_JOURNAL_PATH}/{HYBRID_TOOL_MANIFEST_FILENAME}"
    pid_path = f"{_JOURNAL_PATH}/{HYBRID_TOOL_PID_FILENAME}"
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    verification_complete = False
    try:
        verification = await _poll_startup_file(
            handle,
            verification_path,
            failure_path,
            deadline,
        )
        verification_seconds = _parse_package_verification_seconds(verification)
        record_hybrid_value(HybridMetric.PACKAGE_VERIFY_DURATION, verification_seconds)
        record_hybrid_progress(
            HybridProgressPhase.PACKAGE_VERIFY,
            HybridProgressStatus.COMPLETED,
            duration_seconds=verification_seconds,
        )
        verification_complete = True
        record_hybrid_progress(
            HybridProgressPhase.EXECUTOR_READY,
            HybridProgressStatus.STARTED,
        )
        readiness = await _poll_startup_file(
            handle,
            readiness_path,
            failure_path,
            deadline,
        )
        pid = await _poll_startup_file(handle, pid_path, failure_path, deadline)
    except asyncio.CancelledError:
        record_hybrid_progress(
            (
                HybridProgressPhase.EXECUTOR_READY
                if verification_complete
                else HybridProgressPhase.PACKAGE_VERIFY
            ),
            HybridProgressStatus.CANCELLED,
        )
        raise
    except TimeoutError as exc:
        record_hybrid_progress(
            (
                HybridProgressPhase.EXECUTOR_READY
                if verification_complete
                else HybridProgressPhase.PACKAGE_VERIFY
            ),
            HybridProgressStatus.FAILED,
        )
        diagnostic = await _executor_startup_diagnostic(handle, executor_pid)
        raise RuntimeError(
            f"Hybrid executor readiness timed out ({diagnostic})."
        ) from exc
    _validate_readiness(readiness, pid)
    record_hybrid_progress(
        HybridProgressPhase.EXECUTOR_READY,
        HybridProgressStatus.COMPLETED,
        duration_seconds=time.perf_counter() - started,
    )
    record_hybrid_duration(HybridMetric.EXECUTOR_READY_DURATION, started)
    discovery_started = time.perf_counter()
    record_hybrid_progress(
        HybridProgressPhase.DISCOVERY,
        HybridProgressStatus.STARTED,
    )
    manifest = parse_hybrid_tool_manifest(
        await _poll_file(handle, manifest_path, deadline)
    )
    record_hybrid_progress(
        HybridProgressPhase.DISCOVERY,
        HybridProgressStatus.COMPLETED,
        duration_seconds=time.perf_counter() - discovery_started,
    )
    record_hybrid_duration(HybridMetric.DISCOVERY_DURATION, discovery_started)
    return manifest


async def _poll_startup_file(
    handle: SandboxSessionHandle,
    path: str,
    failure_path: str,
    deadline: float,
) -> bytes:
    while True:
        try:
            return await handle.read_file(path)
        except SandboxFileNotFoundError:
            try:
                failure = await handle.read_file(failure_path)
            except SandboxFileNotFoundError:
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("Hybrid sandbox file wait timed out.") from None
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                continue
            phase, exception_type = _parse_startup_failure(failure)
            if phase == "package_verify":
                record_hybrid_count(HybridMetric.PACKAGE_VERIFY_FAILURES)
                record_hybrid_progress(
                    HybridProgressPhase.PACKAGE_VERIFY,
                    HybridProgressStatus.FAILED,
                )
            else:
                record_hybrid_progress(
                    HybridProgressPhase.EXECUTOR_READY,
                    HybridProgressStatus.FAILED,
                )
            raise RuntimeError(
                "Hybrid executor startup failed "
                f"(phase={phase}, terminal_exception={exception_type})."
            ) from None


def _parse_package_verification_seconds(payload: bytes) -> float:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Hybrid package verification metadata is invalid.") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"duration_ms", "protocol_version", "verified"}
        or value.get("protocol_version") != HYBRID_TOOL_PROTOCOL_VERSION
        or value.get("verified") is not True
        or not isinstance(value.get("duration_ms"), (int, float))
        or isinstance(value.get("duration_ms"), bool)
        or not math.isfinite(value["duration_ms"])
        or value["duration_ms"] < 0
    ):
        raise RuntimeError("Hybrid package verification metadata is invalid.")
    return float(value["duration_ms"]) / 1000.0


def _parse_startup_failure(payload: bytes) -> tuple[str, str]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Hybrid executor failure metadata is invalid.") from exc
    if (
        not isinstance(value, dict)
        or set(value)
        != {"exception_type", "phase", "protocol_version", "startup_failed"}
        or value.get("protocol_version") != HYBRID_TOOL_PROTOCOL_VERSION
        or value.get("startup_failed") is not True
        or value.get("phase") not in {"package_verify", "startup"}
        or not isinstance(value.get("exception_type"), str)
        or not _EXECUTOR_EXCEPTION_TYPE.fullmatch(value["exception_type"])
    ):
        raise RuntimeError("Hybrid executor failure metadata is invalid.")
    return str(value["phase"]), str(value["exception_type"])


async def _executor_startup_diagnostic(
    handle: SandboxSessionHandle,
    executor_pid: str,
) -> str:
    """Return bounded, content-blind process/log metadata for startup failure."""
    command = (
        f"if kill -0 {executor_pid} 2>/dev/null; then state=running; else state=exited; fi; "
        f"printf 'state=%s\\n' \"$state\"; "
        f"if [ -f {shlex.quote(_EXECUTOR_LOG_PATH)} ]; then "
        f"wc -c < {shlex.quote(_EXECUTOR_LOG_PATH)}; "
        f"tail -c 8192 {shlex.quote(_EXECUTOR_LOG_PATH)}; fi"
    )
    try:
        result = await handle.exec(command, timeout_seconds=5.0)
    except Exception:
        return "process=unknown, log=unavailable"
    lines = result.stdout.splitlines()
    state = (
        lines[0].removeprefix("state=")
        if lines and lines[0] in {"state=running", "state=exited"}
        else "unknown"
    )
    log_bytes = lines[1] if len(lines) > 1 and lines[1].isdigit() else "unknown"
    exception_type = "unknown"
    if len(lines) > 2:
        candidate = lines[-1].partition(":")[0].strip()
        if _EXECUTOR_EXCEPTION_TYPE.fullmatch(candidate):
            exception_type = candidate
    return (
        f"process={state}, log_bytes={log_bytes}, "
        f"terminal_exception={exception_type}"
    )


def _validate_readiness(readiness_payload: bytes, pid_payload: bytes) -> None:
    try:
        readiness = json.loads(readiness_payload)
        pid = json.loads(pid_payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Hybrid executor readiness is invalid.") from exc
    expected_fields = {"pid", "protocol_version", "ready"}
    if (
        not isinstance(readiness, dict)
        or set(readiness) != expected_fields
        or readiness.get("protocol_version") != HYBRID_TOOL_PROTOCOL_VERSION
        or readiness.get("ready") is not True
        or not isinstance(pid, dict)
        or set(pid) != {"pid", "protocol_version"}
        or pid.get("protocol_version") != HYBRID_TOOL_PROTOCOL_VERSION
        or pid.get("pid") != readiness.get("pid")
    ):
        raise RuntimeError("Hybrid executor readiness is invalid.")


async def _poll_result(
    handle: SandboxSessionHandle,
    path: str,
    *,
    timeout_seconds: float,
) -> HybridToolInvocationResult:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    return parse_hybrid_tool_result(await _poll_file(handle, path, deadline))


async def _poll_file(
    handle: SandboxSessionHandle,
    path: str,
    deadline: float,
) -> bytes:
    while True:
        try:
            return await handle.read_file(path)
        except SandboxFileNotFoundError:
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("Hybrid sandbox file wait timed out.") from None
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)


def _build_stub(descriptor: HybridToolDescriptor) -> FunctionTool:
    async def fail_closed_stub(**_arguments: object) -> object:
        raise RuntimeError(
            f"Hybrid middleware did not intercept local tool {descriptor.name!r}."
        )

    return FunctionTool(
        name=descriptor.name,
        description=descriptor.description,
        func=fail_closed_stub,
        input_model=descriptor.parameters,
    )


def _current_traceparent() -> str | None:
    try:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
        if context is None or not context.is_valid:
            return None
        return (
            f"00-{context.trace_id:032x}-{context.span_id:016x}-"
            f"{int(context.trace_flags):02x}"
        )
    except Exception:
        return None


async def _best_effort_delete(
    handle: SandboxSessionHandle,
    provider: SandboxSessionProvider,
    *,
    timeout_seconds: float = _ROLLBACK_DELETE_TIMEOUT_SECONDS,
) -> None:
    """Boundedly retry deletion through independent handle and provider seams.

    Each attempt receives an even slice of the time that is still left, so one
    hung seam cannot consume the whole window and no attempt runs past the
    deadline. Callers pass a budget that funds at least one transport poll cycle
    per seam. Exhausted retries stay observable and rely on the sandbox
    auto-delete policy plus the bounded reaper as the backstop.
    """
    started = time.perf_counter()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + min(
        timeout_seconds,
        _ROLLBACK_DELETE_TIMEOUT_SECONDS,
    )
    error: Exception | None = None
    try:
        sandbox_id = handle.identity.sandbox_id
        for attempt in range(_ROLLBACK_DELETE_ATTEMPTS):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            attempt_seconds = _delete_attempt_seconds(
                remaining,
                _ROLLBACK_DELETE_ATTEMPTS - attempt,
            )
            try:
                if attempt == 0:
                    await asyncio.wait_for(handle.delete(), timeout=attempt_seconds)
                else:
                    await asyncio.wait_for(
                        provider.delete_sandbox(sandbox_id),
                        timeout=attempt_seconds,
                    )
                record_hybrid_count(HybridMetric.SANDBOX_DELETES)
                return
            except SandboxNotFoundError:
                record_hybrid_count(HybridMetric.SANDBOX_DELETES)
                return
            except Exception as exc:
                error = exc
                if attempt > 0 and attempt + 1 < _ROLLBACK_DELETE_ATTEMPTS:
                    backoff = deadline - loop.time()
                    await asyncio.sleep(min(0.5 * (attempt + 1), max(0.0, backoff)))
        record_hybrid_count(HybridMetric.SANDBOX_DELETE_FAILURES)
        logger.error(
            "Hybrid sandbox deletion exhausted its bounded retries "
            "(last_error_type=%s).",
            type(error).__name__ if error is not None else "timeout",
        )
    finally:
        record_hybrid_duration(HybridMetric.SANDBOX_DELETE_DURATION, started)
        try:
            await handle.close()
        except Exception:
            logger.warning("Hybrid sandbox handle close failed.", exc_info=True)


def _delete_attempt_seconds(remaining: float, attempts_remaining: int) -> float:
    """Return one slice of the remaining budget without extending the deadline.

    The slice is an even division of the time that is actually left, so a hung
    seam can never consume another seam's share and no attempt outlives the
    caller's deadline. The "at least one transport poll cycle per seam"
    guarantee is enforced where the budget is chosen (see
    ``_post_run_delete_seconds``), not by inflating a slice past the deadline.
    """
    if attempts_remaining <= 1:
        return max(0.0, remaining)
    return max(0.0, remaining) / attempts_remaining


def _post_run_delete_seconds() -> float:
    """Bound completed-run cleanup by observed delete latency, not the drain window.

    The drain window covers in-flight tool calls and is unrelated to control
    plane deletion, so deriving cleanup from it capped the first attempt at
    1.67 s: below the 3 s transport polling interval and below every recorded
    deletion. The budget instead funds ``_ROLLBACK_DELETE_ATTEMPTS`` slices of
    at least ``_MIN_DELETE_ATTEMPT_SECONDS`` and covers the recorded range while
    staying far under the failed-acquire rollback window.
    """
    return max(
        _POST_RUN_DELETE_TIMEOUT_SECONDS,
        _MIN_DELETE_ATTEMPT_SECONDS * _ROLLBACK_DELETE_ATTEMPTS,
    )


def _remaining_tool_seconds(deadline: float) -> float:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError("Hybrid tool deadline elapsed before transfer.")
    return remaining
