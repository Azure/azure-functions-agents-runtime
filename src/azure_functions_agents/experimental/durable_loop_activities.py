"""Provider, content, compaction, and background-operation activity seams."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from importlib.metadata import version
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import BaseModel

from .._credential import build_async_credential_with_client_id
from ..client_manager import ClientManager
from ..strict_json import assert_json_value, canonical_json_bytes
from .durable_loop_config import (
    DURABLE_LOOP_CONTENT_BLOB_URI_ENV,
    DURABLE_LOOP_CONTENT_CLIENT_ID_ENV,
    DURABLE_LOOP_CONTENT_CONTAINER_ENV,
)
from .durable_loop_protocol import (
    BackgroundPollResultV1,
    BackgroundStartDisposition,
    BackgroundStartResultV1,
    ContentRefV1,
    DurableRunIdentityV1,
    ErrorDisposition,
    ErrorEnvelopeV1,
    FrozenToolCatalogV1,
    HumanEventDeliveryResultV1,
    MAFMessageBundleV1,
    ModelDecisionEnvelopeV1,
    ModelOperationStatus,
    ModelOperationV1,
    ModelToolCallV1,
    ToolResultV1,
    UsageV1,
    WorkingContextV1,
    canonical_hash,
    deterministic_model_step_key,
    parse_durable_loop_document,
)


class DurableLoopContentError(RuntimeError):
    """External durable content is unavailable, corrupt, or oversized."""


class DurableLoopModelError(RuntimeError):
    """One model step violated the explicit one-step contract."""


class DurableLoopProviderTerminalError(DurableLoopModelError):
    """The provider returned a terminal response that is not a complete decision."""

    def __init__(
        self,
        *,
        code: str,
        finish_reason: str,
        usage: UsageV1,
    ) -> None:
        super().__init__(f"{code}:{finish_reason}")
        self.code = code
        self.finish_reason = finish_reason
        self.usage = usage


class DurableLoopContextOverflowError(RuntimeError):
    """Deterministic compaction could not fit the working context."""


class ResumeContextDisposition(StrEnum):
    """How a long-parked model context can resume."""

    EXACT = "exact"
    REHYDRATE = "rehydrate"
    UNAVAILABLE = "unavailable"


@runtime_checkable
class DurableContentStore(Protocol):
    """Opaque immutable content storage used by activities."""

    async def put_bytes(
        self,
        *,
        kind: str,
        payload: bytes,
        media_type: str,
        retention_class: str,
    ) -> ContentRefV1:
        """Commit one immutable object and return its non-authorizing reference."""

    async def get_bytes(self, reference: ContentRefV1) -> bytes:
        """Read and integrity-check one immutable object."""


class InMemoryDurableContentStore:
    """Local-test content store with immutable hash-addressed objects."""

    def __init__(self, *, maximum_bytes: int = 32 * 1024 * 1024) -> None:
        self._maximum_bytes = maximum_bytes
        self._objects: dict[str, bytes] = {}
        self._lock = asyncio.Lock()

    @property
    def object_count(self) -> int:
        """Return the number of committed objects for deterministic tests."""
        return len(self._objects)

    async def put_bytes(
        self,
        *,
        kind: str,
        payload: bytes,
        media_type: str,
        retention_class: str,
    ) -> ContentRefV1:
        if len(payload) > self._maximum_bytes:
            raise DurableLoopContentError("content exceeds the configured byte limit")
        digest = canonical_hash(
            {
                "kind": kind,
                "payload_sha256": _sha256_bytes(payload),
                "retention_class": retention_class,
            }
        )
        object_id = f"content/{kind}/{digest}"
        async with self._lock:
            existing = self._objects.get(object_id)
            if existing is not None and existing != payload:
                raise DurableLoopContentError("content address collision")
            self._objects[object_id] = bytes(payload)
        return ContentRefV1(
            object_id=object_id,
            sha256=_sha256_bytes(payload),
            byte_length=len(payload),
            media_type=media_type,
            encryption_version="local-test-v1",
            retention_class=retention_class,
        )

    async def get_bytes(self, reference: ContentRefV1) -> bytes:
        async with self._lock:
            try:
                payload = self._objects[reference.object_id]
            except KeyError:
                raise DurableLoopContentError("content reference was not found") from None
        if len(payload) != reference.byte_length or _sha256_bytes(payload) != reference.sha256:
            raise DurableLoopContentError("content reference integrity check failed")
        return bytes(payload)

    async def put_json(
        self,
        *,
        kind: str,
        value: object,
        retention_class: str,
    ) -> ContentRefV1:
        """Commit one canonical JSON value."""
        return await self.put_bytes(
            kind=kind,
            payload=canonical_json_bytes(value),
            media_type="application/json",
            retention_class=retention_class,
        )

    async def get_json(self, reference: ContentRefV1) -> object:
        """Read one canonical JSON value."""
        import json

        return json.loads(await self.get_bytes(reference))

    async def put_text(
        self,
        *,
        kind: str,
        value: str,
        retention_class: str,
    ) -> ContentRefV1:
        """Commit one UTF-8 text value."""
        return await self.put_bytes(
            kind=kind,
            payload=value.encode("utf-8"),
            media_type="text/plain; charset=utf-8",
            retention_class=retention_class,
        )

    async def get_text(self, reference: ContentRefV1) -> str:
        """Read one UTF-8 text value."""
        return (await self.get_bytes(reference)).decode("utf-8")


class BlobDurableContentStore:
    """Immutable block-blob content store for refs-only Durable history."""

    def __init__(
        self,
        service_client: Any,
        *,
        container_name: str = "azure-functions-agents-durable",
        maximum_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        self._service_client = service_client
        self._container_name = container_name
        self._maximum_bytes = maximum_bytes
        self._ensure_lock = asyncio.Lock()
        self._ensured = False

    @classmethod
    def from_environment(cls) -> BlobDurableContentStore:
        """Build from explicit durable-content settings, never host storage defaults."""
        from azure.storage.blob.aio import BlobServiceClient

        service_url = os.environ.get(DURABLE_LOOP_CONTENT_BLOB_URI_ENV, "").strip()
        if not service_url:
            raise DurableLoopContentError(
                "durable-loop content storage requires "
                f"{DURABLE_LOOP_CONTENT_BLOB_URI_ENV}"
            )
        parsed = urlsplit(service_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise DurableLoopContentError(
                f"{DURABLE_LOOP_CONTENT_BLOB_URI_ENV} must be a credential-free HTTPS origin"
            )
        container_name = os.environ.get(
            DURABLE_LOOP_CONTENT_CONTAINER_ENV,
            "",
        ).strip()
        if not _valid_container_name(container_name):
            raise DurableLoopContentError(
                f"{DURABLE_LOOP_CONTENT_CONTAINER_ENV} must be a valid explicit container name"
            )
        client_id = os.environ.get(
            DURABLE_LOOP_CONTENT_CLIENT_ID_ENV,
            "",
        ).strip() or None
        return cls(
            BlobServiceClient(
                account_url=service_url,
                credential=build_async_credential_with_client_id(client_id),
            ),
            container_name=container_name,
        )

    async def put_bytes(
        self,
        *,
        kind: str,
        payload: bytes,
        media_type: str,
        retention_class: str,
    ) -> ContentRefV1:
        if len(payload) > self._maximum_bytes:
            raise DurableLoopContentError("content exceeds the configured byte limit")
        await self._ensure_container()
        digest = _sha256_bytes(payload)
        object_id = f"objects/{kind}/{digest}"
        client = self._service_client.get_blob_client(
            container=self._container_name,
            blob=object_id,
        )
        from azure.core.exceptions import ResourceExistsError

        try:
            await client.upload_blob(payload, overwrite=False)
        except ResourceExistsError as exc:
            downloader = await client.download_blob()
            existing = await downloader.readall()
            if existing != payload:
                raise DurableLoopContentError("content address collision") from exc
        return ContentRefV1(
            object_id=object_id,
            sha256=digest,
            byte_length=len(payload),
            media_type=media_type,
            encryption_version="azure-storage-service-v1",
            retention_class=retention_class,
        )

    async def get_bytes(self, reference: ContentRefV1) -> bytes:
        await self._ensure_container()
        client = self._service_client.get_blob_client(
            container=self._container_name,
            blob=reference.object_id,
        )
        downloader = await client.download_blob()
        payload = await downloader.readall()
        if not isinstance(payload, bytes):
            payload = bytes(payload)
        if len(payload) != reference.byte_length or _sha256_bytes(payload) != reference.sha256:
            raise DurableLoopContentError("content reference integrity check failed")
        return payload

    async def _ensure_container(self) -> None:
        if self._ensured:
            return
        async with self._ensure_lock:
            if self._ensured:
                return
            from azure.core.exceptions import ResourceExistsError

            container = self._service_client.get_container_client(
                self._container_name
            )
            with contextlib.suppress(ResourceExistsError):
                await container.create_container()
            self._ensured = True


async def put_protocol_model(
    store: DurableContentStore,
    *,
    kind: str,
    model: object,
    retention_class: str = "run",
) -> ContentRefV1:
    """Persist one strict protocol model outside Durable history."""
    return await store.put_bytes(
        kind=kind,
        payload=canonical_json_bytes(model),
        media_type="application/json",
        retention_class=retention_class,
    )


async def get_protocol_model[ModelT: BaseModel](
    store: DurableContentStore,
    reference: ContentRefV1,
    model: type[ModelT],
) -> ModelT:
    """Read and validate one strict protocol model."""
    payload = (await store.get_bytes(reference)).decode("utf-8")
    return parse_durable_loop_document(
        payload,
        model,
        maximum_bytes=reference.byte_length,
    )


@dataclass(frozen=True, slots=True)
class OneStepModelRequest:
    """Complete explicit input to one stateless MAF inference."""

    identity: DurableRunIdentityV1
    step_index: int
    instructions: str
    working_context: WorkingContextV1
    catalog: FrozenToolCatalogV1
    model_settings: Mapping[str, object]
    effective_active_deadline: datetime | None = None

    def canonical_request_hash(self) -> str:
        """Hash the complete provider-visible request."""
        assert_json_value(self.model_settings)
        return canonical_hash(
            {
                "catalog": self.catalog.model_dump(mode="json"),
                "deployment_hash": self.identity.deployment_hash,
                "instructions": self.instructions,
                "messages": self.working_context.bundle.model_dump(mode="json"),
                "model_settings": dict(self.model_settings),
                "run_id": self.identity.run_id,
                "step_index": self.step_index,
                "effective_active_deadline": (
                    self.effective_active_deadline.isoformat()
                    if self.effective_active_deadline is not None
                    else self.identity.active_deadline.isoformat()
                ),
            }
        )


@runtime_checkable
class OneStepModelProvider(Protocol):
    """Narrow model-call seam for one explicit, tool-disabled inference."""

    async def run_one_step(
        self,
        request: OneStepModelRequest,
    ) -> ModelDecisionEnvelopeV1:
        """Return final text or ordered calls after exactly one inference."""


@runtime_checkable
class BackgroundModelProvider(Protocol):
    """Start/poll/cancel seam for one backend-bound background response."""

    async def start(
        self,
        request: OneStepModelRequest,
    ) -> BackgroundStartResultV1:
        """Start one operation or return an immediate terminal decision."""

    async def poll(
        self,
        operation: ModelOperationV1,
    ) -> BackgroundPollResultV1:
        """Poll the same backend-bound provider operation."""

    async def cancel(self, operation: ModelOperationV1) -> ModelOperationV1:
        """Request provider cancellation without changing local authority."""


@runtime_checkable
class HumanEventDeliveryPort(Protocol):
    """Host adapter used by the durable human-answer outbox."""

    async def deliver(
        self,
        *,
        run_id: str,
        event_name: str,
        event_data: Mapping[str, object],
    ) -> HumanEventDeliveryResultV1:
        """Deliver one at-least-once external-event wake-up."""


@runtime_checkable
class ResumeContextValidator(Protocol):
    """Validate exact replay after a long human wait."""

    async def validate(
        self,
        context: WorkingContextV1,
        *,
        maf_core_version: str,
        provider: str,
        model: str,
        api_version: str,
    ) -> ResumeContextDisposition:
        """Return exact, controlled rehydration, or unavailable."""


@runtime_checkable
class ContextRehydrator(Protocol):
    """Create a new working context without rewriting immutable audit history."""

    async def rehydrate(
        self,
        context: WorkingContextV1,
        *,
        maf_core_version: str,
        provider: str,
        model: str,
        api_version: str,
    ) -> WorkingContextV1:
        """Return one versioned context for the next model step."""


class ExactBindingResumeValidator:
    """Require exact MAF/provider/model/API binding or request rehydration."""

    async def validate(
        self,
        context: WorkingContextV1,
        *,
        maf_core_version: str,
        provider: str,
        model: str,
        api_version: str,
    ) -> ResumeContextDisposition:
        binding = context.bundle
        if (
            binding.maf_core_version == maf_core_version
            and binding.provider == provider
            and binding.model == model
            and binding.api_version == api_version
        ):
            return ResumeContextDisposition.EXACT
        return ResumeContextDisposition.REHYDRATE


class DeterministicContextRehydrator:
    """Add an explicit replay marker while retaining the original audit hash."""

    async def rehydrate(
        self,
        context: WorkingContextV1,
        *,
        maf_core_version: str,
        provider: str,
        model: str,
        api_version: str,
    ) -> WorkingContextV1:
        marker: dict[str, object] = {
            "contents": [
                {
                    "text": (
                        "The durable runtime rehydrated this context after a "
                        "validated provider-binding change. Completed tool calls "
                        "must not be repeated."
                    ),
                    "type": "text",
                }
            ],
            "role": "system",
        }
        bundle = MAFMessageBundleV1.create(
            messages=(*context.bundle.messages, marker),
            maf_core_version=maf_core_version,
            provider=provider,
            model=model,
            api_version=api_version,
        )
        return context.model_copy(
            update={
                "bundle": bundle,
                "compaction_generation": context.compaction_generation + 1,
                "parent_context_hash": canonical_hash(
                    context.model_dump(mode="json")
                ),
            }
        )


@dataclass(frozen=True, slots=True)
class ScriptedModelStep:
    """One deterministic fake model decision."""

    final_text: str | None = None
    calls: tuple[tuple[str, str, dict[str, object]], ...] = ()
    assistant_message: dict[str, object] | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_microunits: int | None = None

    def build(
        self,
        request: OneStepModelRequest,
    ) -> ModelDecisionEnvelopeV1:
        """Bind the scripted response to the current durable request."""
        tool_calls = tuple(
            ModelToolCallV1(call_id=call_id, name=name, arguments=arguments)
            for call_id, name, arguments in self.calls
        )
        assistant_message = self.assistant_message or _assistant_message(
            self.final_text,
            tool_calls,
        )
        return ModelDecisionEnvelopeV1(
            run_id=request.identity.run_id,
            step_index=request.step_index,
            model_call_key=deterministic_model_step_key(
                request.identity.run_id,
                request.step_index,
            ),
            response_id_hash=canonical_hash(
                {"response_id": f"response-{request.step_index}"}
            ),
            deployment_hash=request.identity.deployment_hash,
            assistant_message=assistant_message,
            tool_calls=tool_calls,
            final_text=self.final_text,
            usage=UsageV1(
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                reasoning_tokens=self.reasoning_tokens,
                cost_microunits=self.cost_microunits,
            ),
            finish_reason="tool_calls" if tool_calls else "stop",
        )


class ScriptedOneStepModelProvider:
    """Deterministic provider used by local samples and replay tests."""

    def __init__(self, steps: Sequence[ScriptedModelStep]) -> None:
        self._steps = tuple(steps)
        self.calls: list[OneStepModelRequest] = []

    async def run_one_step(
        self,
        request: OneStepModelRequest,
    ) -> ModelDecisionEnvelopeV1:
        self.calls.append(request)
        try:
            step = self._steps[request.step_index]
        except IndexError:
            raise DurableLoopModelError("scripted model has no response for this step") from None
        return step.build(request)


class MafOneStepModelProvider:
    """Exact MAF 1.17 one-step Agent adapter with tool invocation disabled."""

    def __init__(self, client_manager: ClientManager) -> None:
        self._client_manager = client_manager

    async def run_one_step(
        self,
        request: OneStepModelRequest,
    ) -> ModelDecisionEnvelopeV1:
        """Rebuild a fresh Agent and perform exactly one explicit inference."""
        _require_target_maf_versions()
        from agent_framework import Agent, FunctionTool, Message

        client, target = self._client_manager.build_chat_client_with_target(
            request.working_context.bundle.model
        )
        if any(
            value is not None
            for value in (
                target.provider,
                target.model,
                target.endpoint,
                target.api_version,
            )
        ):
            current_provider = target.provider or self._client_manager.name
            current_model = target.model or request.working_context.bundle.model
            current_api_version = (
                target.api_version or request.working_context.bundle.api_version
            )
            current_hash = canonical_hash(
                {
                    "api_version": current_api_version,
                    "endpoint": (target.endpoint or "").rstrip("/"),
                    "model": current_model,
                    "provider": current_provider,
                }
            )
            if (
                current_hash != request.identity.deployment_hash
                or current_provider != request.working_context.bundle.provider
                or current_model != request.working_context.bundle.model
                or current_api_version
                != request.working_context.bundle.api_version
            ):
                raise DurableLoopModelError(
                    "MAF client target does not match the frozen deployment binding"
                )
        configuration = getattr(client, "function_invocation_configuration", None)
        if not isinstance(configuration, dict):
            raise DurableLoopModelError(
                "MAF chat client does not expose function invocation configuration"
            )
        configuration["enabled"] = False
        options = _durable_model_options(request)
        tools = [
            FunctionTool(
                name=descriptor.name,
                description=descriptor.description,
                func=None,
                input_model=descriptor.parameters,
            )
            for descriptor in request.catalog.tools
        ]
        _validate_encrypted_reasoning_messages(
            request.working_context.bundle.messages
        )
        messages = [
            Message.from_dict(dict(message))
            for message in request.working_context.bundle.messages
        ]
        response = await Agent(
            client=client,
            instructions=request.instructions,
            tools=tools,
            context_providers=[],
            default_options=options,
        ).run(
            messages,
            session=None,
        )
        finish_reason = _finish_reason_value(response.finish_reason)
        usage = response.usage_details or {}
        normalized_usage = UsageV1(
            input_tokens=_usage_count(usage, "input_token_count"),
            output_tokens=_usage_count(usage, "output_token_count"),
            reasoning_tokens=_usage_count_any(
                usage,
                "reasoning_token_count",
                "openai.reasoning_tokens",
                "completion/reasoning_tokens",
            ),
        )
        normalized_usage = normalized_usage.model_copy(
            update={
                "cost_microunits": _model_cost_microunits(
                    request,
                    normalized_usage,
                )
            }
        )
        if finish_reason not in {"stop", "tool_calls"}:
            raise DurableLoopProviderTerminalError(
                code=_provider_terminal_error_code(finish_reason),
                finish_reason=finish_reason,
                usage=normalized_usage,
            )
        if not response.messages:
            raise DurableLoopModelError("MAF returned no response message")
        assistant_message = response.messages[-1]
        serialized = assistant_message.to_dict()
        calls = tuple(
            ModelToolCallV1(
                call_id=_required_content_text(content.call_id, "call_id"),
                name=_required_content_text(content.name, "tool name"),
                arguments=_normalize_call_arguments(content.arguments),
            )
            for content in assistant_message.contents
            if content.type == "function_call"
        )
        final_text = None if calls else response.text
        if calls and finish_reason != "tool_calls":
            raise DurableLoopProviderTerminalError(
                code=_provider_terminal_error_code(finish_reason),
                finish_reason=finish_reason,
                usage=normalized_usage,
            )
        if (
            not calls
            and (
                not isinstance(final_text, str)
                or not final_text.strip()
                or finish_reason != "stop"
            )
        ):
            if not isinstance(final_text, str) or not final_text.strip():
                finish_reason = "empty"
            raise DurableLoopProviderTerminalError(
                code=_provider_terminal_error_code(finish_reason),
                finish_reason=finish_reason,
                usage=normalized_usage,
            )
        if final_text is None and not calls:
            raise DurableLoopModelError(
                "MAF response contains neither final text nor function calls"
            )
        return ModelDecisionEnvelopeV1(
            run_id=request.identity.run_id,
            step_index=request.step_index,
            model_call_key=deterministic_model_step_key(
                request.identity.run_id,
                request.step_index,
            ),
            response_id_hash=(
                canonical_hash({"response_id": response.response_id})
                if response.response_id
                else None
            ),
            deployment_hash=request.identity.deployment_hash,
            assistant_message=serialized,
            tool_calls=calls,
            final_text=final_text,
            usage=normalized_usage,
            finish_reason=finish_reason,
        )


class FakeBackgroundModelProvider:
    """Deterministic background provider with affinity and retention metadata."""

    def __init__(
        self,
        decision: ModelDecisionEnvelopeV1,
        *,
        polls_before_terminal: int = 1,
        lose_start_acknowledgement: bool = False,
        backend_binding_hash: str | None = None,
        now: datetime | None = None,
    ) -> None:
        self._decision = decision
        self._remaining = polls_before_terminal
        self._lose_start_acknowledgement = lose_start_acknowledgement
        self._backend_binding_hash = backend_binding_hash or canonical_hash(
            {"backend": "fake-background"}
        )
        self._now = now or datetime.now(UTC)
        self.starts = 0
        self.polls = 0
        self.cancels = 0

    async def start(
        self,
        request: OneStepModelRequest,
    ) -> BackgroundStartResultV1:
        self.starts += 1
        if self._lose_start_acknowledgement:
            return BackgroundStartResultV1(
                disposition=BackgroundStartDisposition.LOST_ACKNOWLEDGEMENT,
                error=ErrorEnvelopeV1(
                    code="model_start_acknowledgement_lost",
                    classification="model",
                    retryable=False,
                    disposition=ErrorDisposition.AMBIGUOUS,
                    possibly_committed=True,
                    phase="model_step",
                    step_index=request.step_index,
                ),
            )
        operation = ModelOperationV1(
            operation_key=deterministic_model_step_key(
                request.identity.run_id,
                request.step_index,
            ),
            run_id=request.identity.run_id,
            step_index=request.step_index,
            status=ModelOperationStatus.QUEUED,
            backend_binding_hash=self._backend_binding_hash,
            deployment_hash=request.identity.deployment_hash,
            provider_response_ref=_provider_identifier_ref(
                f"background-{request.step_index}"
            ),
            accepted_at=self._now,
            deadline=request.effective_active_deadline
            or request.identity.active_deadline,
            poll_count=0,
            terminal_retrieval_expires_at=self._now + timedelta(minutes=10),
            retrieval_is_repeatable=True,
        )
        return BackgroundStartResultV1(
            disposition=BackgroundStartDisposition.ACCEPTED,
            operation=operation,
        )

    async def poll(
        self,
        operation: ModelOperationV1,
    ) -> BackgroundPollResultV1:
        if operation.backend_binding_hash != self._backend_binding_hash:
            raise DurableLoopModelError("background operation backend binding changed")
        self.polls += 1
        if self._remaining > 0:
            self._remaining -= 1
            return BackgroundPollResultV1(
                operation=operation.model_copy(
                    update={
                        "status": ModelOperationStatus.IN_PROGRESS,
                        "last_polled_at": self._now
                        + timedelta(seconds=self.polls),
                        "poll_count": operation.poll_count + 1,
                    }
                )
            )
        return BackgroundPollResultV1(decision=self._decision)

    async def cancel(self, operation: ModelOperationV1) -> ModelOperationV1:
        if operation.backend_binding_hash != self._backend_binding_hash:
            raise DurableLoopModelError("background operation backend binding changed")
        self.cancels += 1
        return operation.model_copy(update={"status": ModelOperationStatus.CANCELLED})


class DeterministicContextCompactor:
    """Local deterministic compactor preserving complete call/result groups."""

    def __init__(self, *, retain_groups: int = 4) -> None:
        if retain_groups < 1:
            raise ValueError("retain_groups must be positive")
        self._retain_groups = retain_groups

    async def compact(
        self,
        context: WorkingContextV1,
        *,
        maximum_bytes: int,
    ) -> WorkingContextV1:
        """Return a smaller working context without changing the audit bundle."""
        groups = _atomic_message_groups(context.bundle.messages)
        if len(groups) <= self._retain_groups:
            raise DurableLoopContextOverflowError(
                "working context cannot be compacted without dropping required groups"
            )
        retained_groups = groups[-self._retain_groups :]
        prefix_messages = _required_prefix_messages(groups[: -self._retain_groups])
        omitted = tuple(
            message
            for group in groups[: -self._retain_groups]
            for message in group
            if message not in prefix_messages
        )
        summary: dict[str, object] = {
            "role": "user",
            "contents": [
                {
                    "type": "text",
                    "text": _deterministic_semantic_summary(
                        omitted,
                        omitted_group_count=len(groups) - self._retain_groups,
                    ),
                }
            ],
        }
        messages = (
            *prefix_messages,
            summary,
            *(message for group in retained_groups for message in group),
        )
        bundle = MAFMessageBundleV1.create(
            messages=messages,
            maf_core_version=context.bundle.maf_core_version,
            provider=context.bundle.provider,
            model=context.bundle.model,
            api_version=context.bundle.api_version,
        )
        if len(canonical_json_bytes(bundle.messages)) > maximum_bytes:
            raise DurableLoopContextOverflowError(
                "compacted working context still exceeds the byte limit"
            )
        return WorkingContextV1(
            bundle=bundle,
            compaction_generation=context.compaction_generation + 1,
            summary_ref=None,
            source_audit_hash=context.source_audit_hash,
            source_start=context.source_start,
            source_end=context.source_end,
            estimated_tokens=max(1, len(canonical_json_bytes(messages)) // 4),
            actual_tokens=None,
            parent_context_hash=canonical_hash(context.model_dump(mode="json")),
        )


def append_model_and_tool_messages(
    context: WorkingContextV1,
    decision: ModelDecisionEnvelopeV1,
    results: Sequence[ToolResultV1],
) -> WorkingContextV1:
    """Append one atomic assistant-call/result group in provider order."""
    ordered = sorted(results, key=lambda result: result.call_ordinal)
    expected_ids = [call.call_id for call in decision.tool_calls]
    if [result.provider_call_id for result in ordered] != expected_ids:
        raise DurableLoopModelError("tool results do not align with model call order")
    messages = list(context.bundle.messages)
    messages.append(decision.assistant_message)
    messages.extend(_tool_result_message(result) for result in ordered)
    return _replace_working_messages(context, tuple(messages))


def append_audit_model_and_tool(
    bundle: MAFMessageBundleV1,
    decision: ModelDecisionEnvelopeV1,
    results: Sequence[ToolResultV1],
) -> MAFMessageBundleV1:
    """Append one complete assistant-call/result group to immutable audit."""
    ordered = sorted(results, key=lambda result: result.call_ordinal)
    return _replace_bundle_messages(
        bundle,
        (
            *bundle.messages,
            decision.assistant_message,
            *(_tool_result_message(result) for result in ordered),
        ),
    )


def append_audit_final(
    bundle: MAFMessageBundleV1,
    decision: ModelDecisionEnvelopeV1,
) -> MAFMessageBundleV1:
    """Append one final assistant message to immutable audit."""
    return _replace_bundle_messages(
        bundle,
        (*bundle.messages, decision.assistant_message),
    )


def append_audit_assistant(
    bundle: MAFMessageBundleV1,
    decision: ModelDecisionEnvelopeV1,
) -> MAFMessageBundleV1:
    """Append an assistant call before a human wait opens."""
    return append_audit_final(bundle, decision)


def append_audit_tool_results(
    bundle: MAFMessageBundleV1,
    results: Sequence[ToolResultV1],
) -> MAFMessageBundleV1:
    """Append results for an assistant call already present in audit."""
    ordered = sorted(results, key=lambda result: result.call_ordinal)
    return _replace_bundle_messages(
        bundle,
        (
            *bundle.messages,
            *(_tool_result_message(result) for result in ordered),
        ),
    )


def append_assistant_message(
    context: WorkingContextV1,
    decision: ModelDecisionEnvelopeV1,
) -> WorkingContextV1:
    """Append a model decision before a human wait opens."""
    return _replace_working_messages(
        context,
        (*context.bundle.messages, decision.assistant_message),
    )


def append_tool_result_messages(
    context: WorkingContextV1,
    results: Sequence[ToolResultV1],
) -> WorkingContextV1:
    """Append ordered results for an already-checkpointed assistant call."""
    ordered = sorted(results, key=lambda result: result.call_ordinal)
    return _replace_working_messages(
        context,
        (
            *context.bundle.messages,
            *(_tool_result_message(result) for result in ordered),
        ),
    )


def append_final_message(
    context: WorkingContextV1,
    decision: ModelDecisionEnvelopeV1,
) -> WorkingContextV1:
    """Append one final assistant response to the working context."""
    if decision.final_text is None:
        raise DurableLoopModelError("final model message is missing text")
    return _replace_working_messages(
        context,
        (*context.bundle.messages, decision.assistant_message),
    )


def _replace_working_messages(
    context: WorkingContextV1,
    messages: tuple[dict[str, object], ...],
) -> WorkingContextV1:
    bundle = MAFMessageBundleV1.create(
        messages=messages,
        maf_core_version=context.bundle.maf_core_version,
        provider=context.bundle.provider,
        model=context.bundle.model,
        api_version=context.bundle.api_version,
    )
    return context.model_copy(
        update={
            "bundle": bundle,
            "source_end": len(messages),
            "estimated_tokens": max(1, len(canonical_json_bytes(messages)) // 4),
        }
    )


def _replace_bundle_messages(
    bundle: MAFMessageBundleV1,
    messages: tuple[dict[str, object], ...],
) -> MAFMessageBundleV1:
    return MAFMessageBundleV1.create(
        messages=messages,
        maf_core_version=bundle.maf_core_version,
        provider=bundle.provider,
        model=bundle.model,
        api_version=bundle.api_version,
    )


def _assistant_message(
    final_text: str | None,
    calls: Sequence[ModelToolCallV1],
) -> dict[str, object]:
    contents: list[dict[str, object]] = []
    if final_text is not None:
        contents.append({"type": "text", "text": final_text})
    else:
        contents.extend(
            {
                "type": "function_call",
                "call_id": call.call_id,
                "name": call.name,
                "arguments": call.arguments,
            }
            for call in calls
        )
    return {"role": "assistant", "contents": contents}


def _tool_result_message(result: ToolResultV1) -> dict[str, object]:
    value: object
    if result.status.value == "succeeded":
        value = result.value
    else:
        assert result.error is not None
        value = {
            "error_code": result.error.code,
            "status": result.status.value,
        }
    return {
        "role": "tool",
        "contents": [
            {
                "type": "function_result",
                "call_id": result.provider_call_id,
                "name": result.tool_name,
                "result": value,
            }
        ],
    }


def _atomic_message_groups(
    messages: tuple[dict[str, object], ...],
) -> tuple[tuple[dict[str, object], ...], ...]:
    groups: list[tuple[dict[str, object], ...]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if _contains_function_call(message):
            group = [message]
            index += 1
            while index < len(messages) and messages[index].get("role") == "tool":
                group.append(messages[index])
                index += 1
            groups.append(tuple(group))
            continue
        groups.append((message,))
        index += 1
    return tuple(groups)


def _required_prefix_messages(
    groups: Sequence[tuple[dict[str, object], ...]],
) -> tuple[dict[str, object], ...]:
    system_messages: list[dict[str, object]] = []
    user_messages: list[dict[str, object]] = []
    for group in groups:
        for message in group:
            if message.get("role") == "system" and message not in system_messages:
                system_messages.append(message)
            elif message.get("role") == "user" and message not in user_messages:
                user_messages.append(message)
    return (*system_messages, *user_messages[-1:])


def _deterministic_semantic_summary(
    messages: Sequence[dict[str, object]],
    *,
    omitted_group_count: int,
) -> str:
    lines = [
        "Deterministic durable-context summary.",
        f"Omitted message hash: {canonical_hash(tuple(messages))}.",
        f"Omitted groups: {omitted_group_count}.",
    ]
    for message in messages:
        role = str(message.get("role") or "unknown")
        contents = message.get("contents")
        if not isinstance(contents, list | tuple):
            continue
        for content in contents:
            if not isinstance(content, Mapping):
                continue
            content_type = content.get("type")
            if content_type == "text" and isinstance(content.get("text"), str):
                lines.append(f"{role}: {content['text']}")
            elif content_type == "function_call":
                lines.append(
                    f"{role} called {content.get('name', 'unknown')} with "
                    f"{_bounded_summary_value(content.get('arguments'))}"
                )
            elif content_type == "function_result":
                lines.append(
                    f"{role} returned {content.get('name', 'unknown')}: "
                    f"{_bounded_summary_value(content.get('result'))}"
                )
            elif content_type == "reasoning":
                summary = content.get("summary")
                if summary:
                    lines.append(
                        f"{role} reasoning summary: "
                        f"{_bounded_summary_value(summary)}"
                    )
    encoded = "\n".join(lines).encode("utf-8")
    if len(encoded) <= 16 * 1024:
        return encoded.decode("utf-8")
    suffix = f"\nSummary truncated; full source hash: {canonical_hash(tuple(messages))}."
    available = 16 * 1024 - len(suffix.encode("utf-8"))
    return encoded[:available].decode("utf-8", errors="ignore") + suffix


def _bounded_summary_value(value: object) -> str:
    try:
        rendered = canonical_json_bytes(value).decode("utf-8")
    except (TypeError, ValueError):
        rendered = str(value)
    if len(rendered.encode("utf-8")) <= 1024:
        return rendered
    return rendered.encode("utf-8")[:1024].decode(
        "utf-8",
        errors="ignore",
    ) + "..."


def _contains_function_call(message: Mapping[str, object]) -> bool:
    contents = message.get("contents")
    return isinstance(contents, list | tuple) and any(
        isinstance(item, dict) and item.get("type") == "function_call"
        for item in contents
    )


def _sha256_bytes(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def _require_target_maf_versions() -> None:
    expected = {
        "agent-framework-core": "1.17.0",
        "agent-framework-openai": "1.14.2",
        "agent-framework-foundry": "1.12.0",
    }
    actual = {package: version(package) for package in expected}
    if actual != expected:
        raise DurableLoopModelError(
            "durable one-step MAF adapter requires the finalized exact package set"
        )


def _required_content_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise DurableLoopModelError(f"MAF function call is missing {field_name}")
    return value


def _normalize_call_arguments(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        assert_json_value(value)
        return dict(value)
    if isinstance(value, str):
        try:
            decoded: object = json.loads(value)
        except ValueError as exc:
            raise DurableLoopModelError("MAF function arguments are not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise DurableLoopModelError("MAF function arguments must be a JSON object")
        assert_json_value(decoded)
        return decoded
    if value is None:
        return {}
    raise DurableLoopModelError("MAF function arguments have an unsupported shape")


def _validate_encrypted_reasoning_messages(
    messages: Sequence[dict[str, object]],
) -> None:
    for message in messages:
        contents = message.get("contents")
        if not isinstance(contents, list | tuple):
            continue
        for content in contents:
            if not isinstance(content, Mapping) or content.get("type") != "reasoning":
                continue
            protected = content.get("protected_data")
            encrypted = content.get("encrypted_content")
            if not (
                (isinstance(protected, str) and protected)
                or (isinstance(encrypted, str) and encrypted)
            ):
                raise DurableLoopModelError(
                    "serialized reasoning content is missing encrypted data"
                )


def _usage_count(usage: Mapping[str, object], name: str) -> int:
    value = usage.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _usage_count_any(usage: Mapping[str, object], *names: str) -> int:
    for name in names:
        value = _usage_count(usage, name)
        if value:
            return value
    return 0


def _finish_reason_value(value: object) -> str:
    if value is None:
        return "missing"
    raw = getattr(value, "value", value)
    return str(raw).strip().lower() or "missing"


def _provider_terminal_error_code(finish_reason: str) -> str:
    if finish_reason == "content_filter":
        return "model_response_filtered"
    if finish_reason in {"length", "max_output_tokens", "empty"}:
        return "model_response_incomplete"
    return "model_response_nonterminal"


def _model_cost_microunits(
    request: OneStepModelRequest,
    usage: UsageV1,
) -> int | None:
    budget = request.identity.budget
    if budget.max_cost_microunits == 0:
        return None
    input_cost = (
        usage.input_tokens
        * budget.input_cost_microunits_per_million_tokens
    )
    output_cost = (
        usage.output_tokens
        * budget.output_cost_microunits_per_million_tokens
    )
    return math.ceil((input_cost + output_cost) / 1_000_000)


def _provider_identifier_ref(value: str) -> ContentRefV1:
    payload = value.encode("utf-8")
    digest = _sha256_bytes(payload)
    return ContentRefV1(
        object_id=f"content/provider-response/{digest}",
        sha256=digest,
        byte_length=len(payload),
        media_type="text/plain; charset=utf-8",
        encryption_version="local-test-v1",
        retention_class="provider-operation",
    )


def _valid_container_name(value: str) -> bool:
    return (
        3 <= len(value) <= 63
        and value[0].isalnum()
        and value[-1].isalnum()
        and all(character.islower() or character.isdigit() or character == "-" for character in value)
        and "--" not in value
    )


def _durable_model_options(request: OneStepModelRequest) -> dict[str, object]:
    options = dict(request.model_settings)
    forbidden = {
        "continuation_token",
        "conversation",
        "conversation_id",
        "previous_response_id",
    }
    present = sorted(forbidden & options.keys())
    if present:
        raise DurableLoopModelError(
            f"durable model settings contain stateful fields: {present}"
        )
    configured_model = options.get("model")
    if configured_model is not None and configured_model != request.working_context.bundle.model:
        raise DurableLoopModelError(
            "durable model setting does not match the frozen model binding"
        )
    if options.get("store") not in {None, False}:
        raise DurableLoopModelError("durable one-step inference requires store=False")
    include = options.get("include")
    if include is None:
        resolved_include: list[str] = []
    elif isinstance(include, list) and all(isinstance(item, str) for item in include):
        resolved_include = list(include)
    else:
        raise DurableLoopModelError("durable model include must be a list of strings")
    if "reasoning.encrypted_content" not in resolved_include:
        resolved_include.append("reasoning.encrypted_content")
    options.update(
        {
            "include": resolved_include,
            "model": request.working_context.bundle.model,
            "store": False,
        }
    )
    return options
