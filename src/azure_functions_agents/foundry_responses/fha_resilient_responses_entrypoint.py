"""Resilient Hosted Agent Responses handler and generated-entrypoint contract."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from textwrap import dedent
from typing import Any, Protocol, cast

from ..registration.catalog import AgentCatalog, CatalogEntry
from ..runner import run_agent, run_agent_events
from .fha_model_catalog_gate import FhaV0CatalogError, validate_fha_v0_catalog
from .fha_private_history import (
    FhaCommittedModelStage,
    FhaHistoryFactory,
    FhaResponsesRequestEnvelope,
)


class FhaResilientResponsesError(RuntimeError):
    """The hosted Responses recovery contract cannot safely continue."""


class FhaResponseContext(Protocol):
    """Response fields used by the generated resilient handler."""

    is_recovery: bool
    persisted_response: object | None
    response_id: str

    async def get_input_text(self) -> str: ...


class FhaTextOutputBuilder(Protocol):
    """Response text builder surface used by the FHA V0 adapter."""

    def emit_added(self) -> object: ...

    def emit_delta(self, text: str) -> object: ...

    def emit_text_done(self, text: str) -> object: ...

    def emit_done(self) -> object: ...


class FhaMessageOutputBuilder(Protocol):
    """Response message builder surface used by the FHA V0 adapter."""

    def emit_added(self) -> object: ...

    def add_text_content(self) -> FhaTextOutputBuilder: ...

    def emit_done(self) -> object: ...


class FhaResponseEventStream(Protocol):
    """Minimal ResponseEventStream surface independent of agentserver imports."""

    def emit_created(self) -> object: ...

    def emit_in_progress(self) -> object: ...

    def add_output_item_message(self) -> FhaMessageOutputBuilder: ...

    def checkpoint(self) -> object: ...

    def emit_completed(self) -> object: ...


class FhaResponsesHost(Protocol):
    """Minimal host decorator surface used by the generated entrypoint."""

    def response_handler(
        self,
        handler: Callable[[object, FhaResponseContext, asyncio.Event], AsyncIterator[object]],
    ) -> Callable[[object, FhaResponseContext, asyncio.Event], AsyncIterator[object]]: ...


@dataclass(frozen=True, slots=True)
class FhaAgentServerResponsesApis:
    """Lazy-loaded Agent Server Responses symbols required by the host."""

    responses_server_options: Callable[..., object]
    responses_agent_server_host: Callable[..., FhaResponsesHost]
    response_event_stream: Callable[..., FhaResponseEventStream]
    set_resilient_tasks_enabled: Callable[[bool], None]


@dataclass(frozen=True, slots=True)
class FhaV0StagePlan:
    """One recovery-safe decision for the current FHA V0 turn."""

    emit_created: bool
    invoke_agent: bool
    publish_output: bool
    checkpoint_output: bool


@dataclass(frozen=True, slots=True)
class _FhaStageStreamFailure:
    error: Exception


type FhaV0StageRunner = Callable[[FhaResponsesRequestEnvelope], Awaitable[str]]
type FhaV0StageStreamRunner = Callable[
    [FhaResponsesRequestEnvelope],
    AsyncIterator[str],
]
type FhaResponseStreamFactory = Callable[[object | None], FhaResponseEventStream]
type FhaPersistedOutputCheck = Callable[[object], bool]


def build_fha_v0_stage_plan(
    *,
    is_recovery: bool,
    has_persisted_response: bool,
    has_persisted_output: bool,
    committed_stage: FhaCommittedModelStage | None,
) -> FhaV0StagePlan:
    """Choose a replay-safe FHA V0 turn action without executing it."""
    if is_recovery and not has_persisted_response:
        raise FhaResilientResponsesError(
            "Hosted Responses recovery requires a persisted response snapshot."
        )
    if is_recovery and has_persisted_output and committed_stage is None:
        raise FhaResilientResponsesError(
            "Checkpointed Hosted Responses output has no matching history commit."
        )
    if committed_stage is None:
        return FhaV0StagePlan(
            emit_created=not is_recovery,
            invoke_agent=True,
            publish_output=True,
            checkpoint_output=True,
        )
    return FhaV0StagePlan(
        emit_created=not is_recovery,
        invoke_agent=False,
        publish_output=not has_persisted_output,
        checkpoint_output=not has_persisted_output,
    )


async def stream_fha_resilient_v0_stage(
    envelope: FhaResponsesRequestEnvelope,
    *,
    context: FhaResponseContext,
    history_factory: FhaHistoryFactory,
    stage_runner: FhaV0StageRunner,
    stage_stream_runner: FhaV0StageStreamRunner | None = None,
    stream_factory: FhaResponseStreamFactory,
    persisted_output_check: FhaPersistedOutputCheck,
    cancellation_signal: asyncio.Event,
) -> AsyncIterator[object]:
    """Stream one checkpointed FHA V0 turn with run-idempotent history ordering."""
    is_recovery = context.is_recovery
    persisted_response = context.persisted_response
    stream = stream_factory(persisted_response if is_recovery else None)
    committed_stage = history_factory.read_committed_stage(envelope)
    has_persisted_output = (
        is_recovery
        and persisted_response is not None
        and persisted_output_check(persisted_response)
    )
    plan = build_fha_v0_stage_plan(
        is_recovery=is_recovery,
        has_persisted_response=persisted_response is not None,
        has_persisted_output=has_persisted_output,
        committed_stage=committed_stage,
    )

    if plan.emit_created:
        yield stream.emit_created()
    yield stream.emit_in_progress()

    if cancellation_signal.is_set():
        return

    output_published = False
    if plan.invoke_agent and stage_stream_runner is not None:
        message = stream.add_output_item_message()
        yield message.emit_added()
        text = message.add_text_content()
        yield text.emit_added()
        chunks: list[str] = []
        async for chunk in _stream_fha_stage_chunks(
            stage_stream_runner(envelope),
            cancellation_signal,
        ):
            chunks.append(chunk)
            yield text.emit_delta(chunk)
        if cancellation_signal.is_set():
            return
        output = "".join(chunks)
        committed_stage = history_factory.commit_model_stage(envelope, output)
        yield text.emit_text_done(committed_stage.output)
        yield text.emit_done()
        yield message.emit_done()
        output_published = True
    elif plan.invoke_agent:

        async def run_stage() -> str:
            return await stage_runner(envelope)

        async def wait_for_cancellation() -> object:
            await cancellation_signal.wait()
            return None

        stage_task = asyncio.create_task(run_stage())
        cancellation_task = asyncio.create_task(wait_for_cancellation())
        done, _pending = await asyncio.wait(
            {stage_task, cancellation_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation_task in done:
            stage_task.cancel()
            await asyncio.gather(stage_task, return_exceptions=True)
            return
        cancellation_task.cancel()
        await asyncio.gather(cancellation_task, return_exceptions=True)
        output = await stage_task
        if cancellation_signal.is_set():
            return
        committed_stage = history_factory.commit_model_stage(envelope, output)
    assert committed_stage is not None

    if cancellation_signal.is_set():
        return
    if plan.publish_output and not output_published:
        message = stream.add_output_item_message()
        yield message.emit_added()
        text = message.add_text_content()
        yield text.emit_added()
        yield text.emit_text_done(committed_stage.output)
        yield text.emit_done()
        yield message.emit_done()
    if cancellation_signal.is_set():
        return
    if plan.checkpoint_output:
        yield stream.checkpoint()

    if cancellation_signal.is_set():
        return
    yield stream.emit_completed()


async def execute_fha_v0_stage(
    envelope: FhaResponsesRequestEnvelope,
    *,
    catalog: AgentCatalog,
    history_factory: FhaHistoryFactory,
) -> str:
    """Run one catalog-selected FHA V0 turn with forced private MAF history."""
    _entry, runner_kwargs = _resolve_fha_v0_run(
        envelope,
        catalog=catalog,
        history_factory=history_factory,
    )
    result = await run_agent(envelope.effective_prompt, **runner_kwargs)
    return result.content


async def execute_fha_v0_stage_stream(
    envelope: FhaResponsesRequestEnvelope,
    *,
    catalog: AgentCatalog,
    history_factory: FhaHistoryFactory,
) -> AsyncIterator[str]:
    """Yield one catalog-selected FHA V0 turn as assistant text deltas."""
    entry, runner_kwargs = _resolve_fha_v0_run(
        envelope,
        catalog=catalog,
        history_factory=history_factory,
    )
    emitted_text = False
    async for event in run_agent_events(
        envelope.effective_prompt,
        display_name=entry.resolved.name,
        **runner_kwargs,
    ):
        event_type = event.get("type")
        content = event.get("content")
        should_emit = event_type == "delta" or (
            event_type == "message" and not emitted_text
        )
        if should_emit and isinstance(content, str) and content:
            emitted_text = True
            yield content
        elif event_type == "error":
            raise FhaResilientResponsesError("Hosted MAF streaming failed.")


def _resolve_fha_v0_run(
    envelope: FhaResponsesRequestEnvelope,
    *,
    catalog: AgentCatalog,
    history_factory: FhaHistoryFactory,
) -> tuple[CatalogEntry, dict[str, Any]]:
    try:
        validate_fha_v0_catalog(catalog)
    except FhaV0CatalogError as exc:
        raise FhaResilientResponsesError(
            "Hosted Responses catalog is not FHA V0 compatible."
        ) from exc
    entry = catalog.get(envelope.agent_slug)
    if entry is None:
        raise FhaResilientResponsesError("Hosted Responses requested an unknown agent.")
    return entry, {
        "instructions": entry.resolved.instructions,
        "timeout": entry.resolved.timeout,
        "tools": list(entry.capabilities.filtered_user_tools or []),
        "mcp_tools": list(entry.capabilities.filtered_mcp_tools or []),
        "skill_paths": entry.capabilities.enabled_skill_paths,
        "model": entry.resolved.model,
        "session_id": envelope.runtime_session_id,
        "sandbox_tools": None,
        "workflow_enabled": False,
        "workflow_durable_client": None,
        "agent_name": entry.resolved.slug,
        "web_request_tools": None,
        "subagents": entry.resolved.subagents,
        "catalog": catalog,
        "history_provider": history_factory.create_maf_history_provider(envelope),
    }


async def _stream_fha_stage_chunks(
    stage_stream: AsyncIterator[str],
    cancellation_signal: asyncio.Event,
) -> AsyncIterator[str]:
    queue: asyncio.Queue[str | _FhaStageStreamFailure | None] = asyncio.Queue(maxsize=1)

    async def produce() -> None:
        try:
            async for chunk in stage_stream:
                resolved_chunk = _validate_fha_stage_chunk(chunk)
                if resolved_chunk:
                    await queue.put(resolved_chunk)
        except Exception as exc:
            await queue.put(_FhaStageStreamFailure(exc))
        else:
            await queue.put(None)

    async def watch_cancellation() -> None:
        await cancellation_signal.wait()
        await queue.put(None)

    producer_task = asyncio.create_task(produce())
    cancellation_task = asyncio.create_task(watch_cancellation())
    try:
        while True:
            item = await queue.get()
            if item is None or cancellation_signal.is_set():
                return
            if isinstance(item, _FhaStageStreamFailure):
                raise item.error
            yield item
    finally:
        producer_task.cancel()
        cancellation_task.cancel()
        await asyncio.gather(
            producer_task,
            cancellation_task,
            return_exceptions=True,
        )


def _validate_fha_stage_chunk(chunk: object) -> str:
    if not isinstance(chunk, str):
        raise FhaResilientResponsesError(
            "Hosted MAF stream returned an invalid text delta."
        )
    return chunk


def create_fha_resilient_responses_host(
    catalog: AgentCatalog,
    *,
    history_factory: FhaHistoryFactory | None = None,
) -> FhaResponsesHost:
    """Create a resilient Responses host only after FHA V0 catalog validation."""
    try:
        validate_fha_v0_catalog(catalog)
    except FhaV0CatalogError as exc:
        raise FhaResilientResponsesError(
            "Hosted Responses catalog is not FHA V0 compatible."
        ) from exc

    apis = _load_fha_agentserver_apis()
    try:
        options = apis.responses_server_options(resilient_background=True)
    except TypeError as exc:
        raise FhaResilientResponsesError(
            "Hosted Responses requires Agent Server resilient_background support."
        ) from exc
    host = apis.responses_agent_server_host(options=options)
    apis.set_resilient_tasks_enabled(True)
    resolved_history_factory = history_factory or FhaHistoryFactory()

    @host.response_handler
    async def handler(
        request: object,
        context: FhaResponseContext,
        cancellation_signal: asyncio.Event,
    ) -> AsyncIterator[object]:
        if cancellation_signal.is_set():
            return
        input_text = await context.get_input_text()
        envelope = FhaResponsesRequestEnvelope.parse_json_input(input_text)

        def stream_factory(persisted_response: object | None) -> FhaResponseEventStream:
            if persisted_response is not None:
                return apis.response_event_stream(
                    response_id=context.response_id,
                    response=persisted_response,
                )
            return apis.response_event_stream(response_id=context.response_id, request=request)

        async def stage_stream_runner(
            stage_envelope: FhaResponsesRequestEnvelope,
        ) -> AsyncIterator[str]:
            async for chunk in execute_fha_v0_stage_stream(
                stage_envelope,
                catalog=catalog,
                history_factory=resolved_history_factory,
            ):
                yield chunk

        async def stage_runner(stage_envelope: FhaResponsesRequestEnvelope) -> str:
            return await execute_fha_v0_stage(
                stage_envelope,
                catalog=catalog,
                history_factory=resolved_history_factory,
            )

        async for event in stream_fha_resilient_v0_stage(
            envelope,
            context=context,
            history_factory=resolved_history_factory,
            stage_runner=stage_runner,
            stage_stream_runner=stage_stream_runner,
            stream_factory=stream_factory,
            persisted_output_check=_persisted_response_has_output,
            cancellation_signal=cancellation_signal,
        ):
            yield event

    return host


def render_fha_hosted_responses_entrypoint() -> str:
    """Return the secret-free generated source entrypoint for a hosted agent."""
    return dedent(
        """\
        import os
        from pathlib import Path

        from azure_functions_agents.config.paths import set_app_root
        from azure_functions_agents.foundry_responses.fha_model_catalog_gate import (
            compile_fha_v0_project,
        )
        from azure_functions_agents.foundry_responses.fha_runtime_projection import (
            FHA_RUNTIME_PROJECTION_FILENAME,
            load_fha_runtime_projection,
        )
        from azure_functions_agents.foundry_responses.fha_resilient_responses_entrypoint import (
            create_fha_resilient_responses_host,
        )

        application_root = Path(__file__).resolve().parent
        set_app_root(application_root)
        projection = load_fha_runtime_projection(
            application_root / FHA_RUNTIME_PROJECTION_FILENAME
        )
        os.environ["FOUNDRY_PROJECT_ENDPOINT"] = projection.project_endpoint
        os.environ["FOUNDRY_MODEL"] = projection.default_model
        os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"] = projection.default_model
        compilation = compile_fha_v0_project(
            application_root,
            project_endpoint=projection.project_endpoint,
            default_model=projection.default_model,
            expected_projection=projection,
        )
        app = create_fha_resilient_responses_host(compilation.catalog)

        if __name__ == "__main__":
            app.run()
        """
    )


FhaModelStagePlan = FhaV0StagePlan
FhaModelStageRunner = FhaV0StageRunner


def build_fha_model_stage_plan(
    *,
    is_recovery: bool,
    has_persisted_response: bool,
    has_persisted_output: bool,
    committed_stage: FhaCommittedModelStage | None,
) -> FhaV0StagePlan:
    """Compatibility wrapper for the pre-V0 stage-plan name."""
    return build_fha_v0_stage_plan(
        is_recovery=is_recovery,
        has_persisted_response=has_persisted_response,
        has_persisted_output=has_persisted_output,
        committed_stage=committed_stage,
    )


async def stream_fha_resilient_model_stage(
    envelope: FhaResponsesRequestEnvelope,
    *,
    context: FhaResponseContext,
    history_factory: FhaHistoryFactory,
    model_stage_runner: FhaV0StageRunner,
    stream_factory: FhaResponseStreamFactory,
    persisted_output_check: FhaPersistedOutputCheck,
    cancellation_signal: asyncio.Event,
) -> AsyncIterator[object]:
    """Compatibility wrapper for the pre-V0 stage-stream name."""
    async for event in stream_fha_resilient_v0_stage(
        envelope,
        context=context,
        history_factory=history_factory,
        stage_runner=model_stage_runner,
        stream_factory=stream_factory,
        persisted_output_check=persisted_output_check,
        cancellation_signal=cancellation_signal,
    ):
        yield event


async def execute_fha_model_only_stage(
    envelope: FhaResponsesRequestEnvelope,
    *,
    catalog: AgentCatalog,
    history_factory: FhaHistoryFactory,
) -> str:
    """Compatibility wrapper for the pre-V0 stage-execution name."""
    return await execute_fha_v0_stage(
        envelope,
        catalog=catalog,
        history_factory=history_factory,
    )


def _persisted_response_has_output(persisted_response: object) -> bool:
    if not isinstance(persisted_response, Mapping):
        raise FhaResilientResponsesError("Hosted Responses persisted snapshot is invalid.")
    output = persisted_response.get("output")
    return isinstance(output, list) and bool(output)


def _load_fha_agentserver_apis() -> FhaAgentServerResponsesApis:
    try:
        from azure.ai.agentserver.core.tasks import (  # type: ignore[import-untyped,unused-ignore]
            set_resilient_tasks_enabled,
        )
        from azure.ai.agentserver.responses import (  # type: ignore[import-untyped,unused-ignore]
            ResponseEventStream,
            ResponsesAgentServerHost,
            ResponsesServerOptions,
        )
    except ImportError as exc:
        raise FhaResilientResponsesError(
            "Hosted Responses requires azure-ai-agentserver-core and "
            "azure-ai-agentserver-responses."
        ) from exc
    return FhaAgentServerResponsesApis(
        responses_server_options=ResponsesServerOptions,
        responses_agent_server_host=cast(
            "Callable[..., FhaResponsesHost]",
            ResponsesAgentServerHost,
        ),
        response_event_stream=cast(
            "Callable[..., FhaResponseEventStream]",
            ResponseEventStream,
        ),
        set_resilient_tasks_enabled=set_resilient_tasks_enabled,
    )
