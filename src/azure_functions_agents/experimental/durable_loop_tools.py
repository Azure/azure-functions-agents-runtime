"""Typed tool dispatch for the private durable-loop foundation."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..strict_json import canonical_json_bytes
from .durable_loop_protocol import (
    DurableFaultProfile,
    ErrorDisposition,
    ErrorEnvelopeV1,
    FrozenToolCatalogV1,
    FrozenToolDescriptorV1,
    SandboxExecutionProfile,
    ToolBehavior,
    ToolProvenance,
    ToolRequestV1,
    ToolResultStatus,
    ToolResultV1,
    canonical_hash,
)

REQUEST_HUMAN_INPUT_TOOL_NAME = "request_human_input"

type ToolHandler = Callable[
    [Mapping[str, object], str],
    object | Awaitable[object],
]


class DurableToolDispatchError(RuntimeError):
    """A durable tool cannot be safely dispatched."""


class DurableToolAmbiguousError(DurableToolDispatchError):
    """A write may have committed before its acknowledgement was lost."""


class DurableToolInspectionError(RuntimeError):
    """Retained execution state could not be safely inspected."""


@runtime_checkable
class DurableToolDispatchPort(Protocol):
    """Layer-2 transport seam for one request-hash-bound tool call."""

    async def dispatch(self, request: ToolRequestV1) -> ToolResultV1:
        """Dispatch one tool without changing request identity."""


@dataclass(frozen=True, slots=True)
class DurableToolCatalogSnapshot:
    """One frozen catalog plus the exact local package hash."""

    catalog: FrozenToolCatalogV1
    package_hash: str


@runtime_checkable
class DurableToolCatalogPort(Protocol):
    """Private discovery seam used before durable run admission."""

    async def freeze_catalog(
        self,
        *,
        policy_hash: str,
        sandbox_profile: SandboxExecutionProfile,
    ) -> DurableToolCatalogSnapshot:
        """Freeze the exact remote/local inventory for one run."""


@runtime_checkable
class DurableToolCleanupPort(Protocol):
    """Cleanup retained execution-plane resources at a terminal boundary."""

    async def cleanup(
        self,
        *,
        run_id: str,
        session_id: str,
        sandbox_profile: SandboxExecutionProfile,
        fault_profile: DurableFaultProfile,
    ) -> None:
        """Converge app-owned execution resources toward zero."""


@dataclass(frozen=True, slots=True)
class DurableRetainedSandboxInspection:
    """Content-free projection of one retained sandbox instance."""

    sandbox_instance_alias: str
    generation: int
    state: str
    workspace_checkpoint_present: bool


@runtime_checkable
class DurableRetainedSandboxInspectionPort(Protocol):
    """Read-only inspection seam for one owner-authorized retained session."""

    async def inspect_retained_sandbox(
        self,
        *,
        run_id: str,
        session_id: str,
    ) -> DurableRetainedSandboxInspection | None:
        """Return safe retained-sandbox state or ``None`` when none exists."""


@dataclass(frozen=True, slots=True)
class RegisteredDurableTool:
    """One frozen descriptor paired with its local adapter handler."""

    descriptor: FrozenToolDescriptorV1
    handler: ToolHandler


class DurableToolRegistry:
    """Collision-free registry used by local fakes and future transports."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredDurableTool] = {}

    def register(
        self,
        descriptor: FrozenToolDescriptorV1,
        handler: ToolHandler,
    ) -> None:
        """Register one handler after reserved-name and collision checks."""
        if descriptor.name == REQUEST_HUMAN_INPUT_TOOL_NAME:
            raise ValueError("request_human_input is reserved by the runtime")
        if descriptor.provenance is ToolProvenance.RUNTIME:
            raise ValueError("customer tools cannot claim runtime provenance")
        if descriptor.name in self._tools:
            raise ValueError(f"tool {descriptor.name!r} is already registered")
        self._tools[descriptor.name] = RegisteredDurableTool(descriptor, handler)

    def catalog(
        self,
        *,
        policy_hash: str,
        package_hash: str,
        include_human_input: bool = True,
    ) -> FrozenToolCatalogV1:
        """Freeze model-visible schemas with optional runtime clarification."""
        descriptors = [entry.descriptor for entry in self._tools.values()]
        if include_human_input:
            descriptors.append(human_input_tool_descriptor())
        return FrozenToolCatalogV1.create(
            tools=tuple(descriptors),
            policy_hash=policy_hash,
            package_hash=package_hash,
        )

    def build_dispatcher(self) -> RegistryToolDispatcher:
        """Build an immutable dispatcher snapshot."""
        return RegistryToolDispatcher(dict(self._tools))


class RegistryToolDispatcher:
    """Deterministic local adapter implementing the layer-2 dispatch port."""

    def __init__(self, tools: Mapping[str, RegisteredDurableTool]) -> None:
        self._tools = dict(tools)
        self._ledger = InMemoryToolEffectLedger()

    async def freeze_catalog(
        self,
        *,
        policy_hash: str,
        sandbox_profile: SandboxExecutionProfile,
    ) -> DurableToolCatalogSnapshot:
        """Freeze deterministic test tools without external discovery."""
        del sandbox_profile
        package_hash = canonical_hash(
            {
                "registered_tools": sorted(self._tools),
            }
            if self._tools
            else {"foundation_tools": []}
        )
        catalog = FrozenToolCatalogV1.create(
            tools=(
                *(entry.descriptor for entry in self._tools.values()),
                human_input_tool_descriptor(),
            ),
            policy_hash=policy_hash,
            package_hash=package_hash,
        )
        return DurableToolCatalogSnapshot(
            catalog=catalog,
            package_hash=package_hash,
        )

    async def cleanup(
        self,
        *,
        run_id: str,
        session_id: str,
        sandbox_profile: SandboxExecutionProfile,
        fault_profile: DurableFaultProfile,
    ) -> None:
        """No-op cleanup for the deterministic in-memory dispatcher."""
        del run_id, session_id, sandbox_profile, fault_profile

    async def inspect_retained_sandbox(
        self,
        *,
        run_id: str,
        session_id: str,
    ) -> DurableRetainedSandboxInspection | None:
        """Return no external sandbox for the in-memory dispatcher."""
        del run_id, session_id
        return None

    async def dispatch(self, request: ToolRequestV1) -> ToolResultV1:
        """Execute one fake/local handler with request-bound deduplication."""
        started_at = time.perf_counter()
        if (
            len(canonical_json_bytes(request.arguments))
            > request.argument_byte_limit
        ):
            return _failure_result(
                request,
                code="tool_arguments_too_large",
                classification="budget",
                elapsed_ms=_elapsed_ms(started_at),
            )
        registered = self._tools.get(request.tool_name)
        if registered is None:
            return _failure_result(
                request,
                code="unknown_tool",
                classification="policy",
                elapsed_ms=_elapsed_ms(started_at),
            )
        if registered.descriptor.provenance is not request.provenance:
            return _failure_result(
                request,
                code="tool_provenance_mismatch",
                classification="policy",
                elapsed_ms=_elapsed_ms(started_at),
            )
        if registered.descriptor.behavior is not request.behavior:
            return _failure_result(
                request,
                code="tool_behavior_mismatch",
                classification="policy",
                elapsed_ms=_elapsed_ms(started_at),
            )

        try:
            value, deduplicated = await self._ledger.execute(
                request,
                registered.handler,
            )
        except TimeoutError:
            return _failure_result(
                request,
                code="tool_timeout",
                classification="timeout",
                elapsed_ms=_elapsed_ms(started_at),
                status=ToolResultStatus.TIMED_OUT,
                retryable=request.behavior is ToolBehavior.READ_ONLY,
            )
        except DurableToolAmbiguousError:
            return _failure_result(
                request,
                code="tool_acknowledgement_lost",
                classification="tool",
                elapsed_ms=_elapsed_ms(started_at),
                status=ToolResultStatus.AMBIGUOUS,
                disposition=ErrorDisposition.AMBIGUOUS,
                possibly_committed=True,
            )
        except Exception:
            return _failure_result(
                request,
                code="tool_execution_failed",
                classification="tool",
                elapsed_ms=_elapsed_ms(started_at),
            )
        if len(canonical_json_bytes(value)) > request.result_byte_limit:
            return _failure_result(
                request,
                code="tool_result_too_large",
                classification="budget",
                elapsed_ms=_elapsed_ms(started_at),
            )

        return ToolResultV1(
            run_id=request.run_id,
            step_index=request.step_index,
            call_ordinal=request.call_ordinal,
            provider_call_id=request.provider_call_id,
            call_key=request.call_key,
            request_hash=request.request_hash,
            tool_name=request.tool_name,
            status=ToolResultStatus.SUCCEEDED,
            value=value,
            elapsed_ms=_elapsed_ms(started_at),
            deduplicated=deduplicated,
        )


class InMemoryToolEffectLedger:
    """Test ledger that models an idempotent external side-effect receipt."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._results: dict[str, tuple[str, object]] = {}
        self._inflight: dict[str, tuple[str, asyncio.Future[object]]] = {}

    async def execute(
        self,
        request: ToolRequestV1,
        handler: ToolHandler,
    ) -> tuple[object, bool]:
        """Execute or replay one call while rejecting changed arguments."""
        async with self._lock:
            recorded = self._results.get(request.call_key)
            if recorded is not None:
                request_hash, value = recorded
                if request_hash != request.request_hash:
                    raise DurableToolDispatchError(
                        "call key was reused with a different request hash"
                    )
                return value, True
            inflight = self._inflight.get(request.call_key)
            if inflight is not None:
                request_hash, future = inflight
                if request_hash != request.request_hash:
                    raise DurableToolDispatchError(
                        "call key was reused with a different request hash"
                    )
                waiter = future
                creator = False
            else:
                waiter = asyncio.get_running_loop().create_future()
                waiter.add_done_callback(_consume_future_exception)
                self._inflight[request.call_key] = (request.request_hash, waiter)
                creator = True
        if not creator:
            return await waiter, True
        try:
            value = handler(request.arguments, request.call_key)
            if inspect.isawaitable(value):
                value = await value
        except BaseException as exc:
            async with self._lock:
                self._inflight.pop(request.call_key, None)
                if not waiter.done():
                    waiter.set_exception(exc)
            raise
        async with self._lock:
            self._results[request.call_key] = (request.request_hash, value)
            self._inflight.pop(request.call_key, None)
            if not waiter.done():
                waiter.set_result(value)
        return value, False


def human_input_tool_descriptor() -> FrozenToolDescriptorV1:
    """Return the runtime-owned clarification schema."""
    return FrozenToolDescriptorV1(
        name=REQUEST_HUMAN_INPUT_TOOL_NAME,
        description=(
            "Request one clarification from the authenticated session owner. "
            "This must be the only tool call in the model step."
        ),
        parameters={
            "additionalProperties": False,
            "properties": {
                "allow_free_text": {"type": "boolean"},
                "choices": {
                    "items": {"maxLength": 256, "type": "string"},
                    "maxItems": 20,
                    "type": "array",
                },
                "question": {"maxLength": 8192, "type": "string"},
                "response_schema": {"type": ["object", "null"]},
            },
            "required": ["question", "choices", "allow_free_text"],
            "type": "object",
        },
        provenance=ToolProvenance.RUNTIME,
        behavior=ToolBehavior.READ_ONLY,
        parallel_safe=False,
    )


def descriptor_hash(descriptor: FrozenToolDescriptorV1) -> str:
    """Return one descriptor's immutable policy hash input."""
    return canonical_hash(descriptor.model_dump(mode="json"))


def _failure_result(
    request: ToolRequestV1,
    *,
    code: str,
    classification: str,
    elapsed_ms: float,
    status: ToolResultStatus = ToolResultStatus.FAILED,
    retryable: bool = False,
    disposition: ErrorDisposition = ErrorDisposition.CERTAIN,
    possibly_committed: bool = False,
) -> ToolResultV1:
    return ToolResultV1(
        run_id=request.run_id,
        step_index=request.step_index,
        call_ordinal=request.call_ordinal,
        provider_call_id=request.provider_call_id,
        call_key=request.call_key,
        request_hash=request.request_hash,
        tool_name=request.tool_name,
        status=status,
        elapsed_ms=elapsed_ms,
        error=ErrorEnvelopeV1(
            code=code,
            classification=classification,
            retryable=retryable,
            disposition=disposition,
            possibly_committed=possibly_committed,
            phase="tool_step",
            step_index=request.step_index,
            call_key=request.call_key,
        ),
    )


def _elapsed_ms(started_at: float) -> float:
    return max(0.0, (time.perf_counter() - started_at) * 1000.0)


def _consume_future_exception(future: asyncio.Future[object]) -> None:
    if not future.cancelled():
        future.exception()
