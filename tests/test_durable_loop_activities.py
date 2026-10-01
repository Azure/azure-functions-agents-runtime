from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from azure_functions_agents.experimental.durable_loop_activities import (
    BlobDurableContentStore,
    DeterministicContextCompactor,
    DurableLoopContentError,
    DurableLoopContextOverflowError,
    DurableSkillOperationError,
    FakeBackgroundModelProvider,
    InMemoryDurableContentStore,
    OneStepModelRequest,
    ScriptedModelStep,
    compose_durable_skill_instructions,
    get_protocol_model,
    load_frozen_skill,
    put_protocol_model,
    resolve_frozen_skill_content_hash,
    search_frozen_skill_catalog,
)
from azure_functions_agents.experimental.durable_loop_catalog import (
    FrozenDurableSkillCatalogV1,
    MaterializedDurableSkillV1,
)
from azure_functions_agents.experimental.durable_loop_config import (
    DURABLE_LOOP_CONTENT_BLOB_URI_ENV,
    DURABLE_LOOP_CONTENT_CONTAINER_ENV,
)
from azure_functions_agents.experimental.durable_loop_protocol import (
    BackgroundStartDisposition,
    ContentRefV1,
    DurableLoopBudgetV1,
    DurableLoopProtocolDocumentError,
    DurableRunIdentityV1,
    DurableSkillCatalogSnapshotV1,
    DurableSkillContentFileV1,
    DurableSkillContentV1,
    DurableSkillMetadataPageV1,
    DurableSkillMetadataV1,
    ErrorDisposition,
    MAFMessageBundleV1,
    ModelOperationStatus,
    UsageV1,
    WorkingContextV1,
    canonical_hash,
)
from azure_functions_agents.experimental.durable_loop_tools import (
    DurableToolRegistry,
)

_HASH = "a" * 64


def _identity() -> DurableRunIdentityV1:
    now = datetime(2026, 9, 4, tzinfo=UTC)
    return DurableRunIdentityV1(
        run_id="run-1",
        session_id="session-1",
        request_id_hash=_HASH,
        request_hash="b" * 64,
        owner_hash="c" * 64,
        agent_slug="main",
        agent_hash="d" * 64,
        catalog_hash="e" * 64,
        deployment_hash="f" * 64,
        tool_package_hash="1" * 64,
        policy_hash="2" * 64,
        orchestration_version="durable_agent_turn_orchestrator_v1",
        created_at=now,
        active_deadline=now + timedelta(hours=4),
        absolute_deadline=now + timedelta(days=7),
        budget=DurableLoopBudgetV1(
            max_model_steps=48,
            max_tool_calls=128,
            max_elapsed_seconds=14400,
            max_human_waits=8,
            human_wait_seconds=86400,
            max_argument_bytes=262144,
            max_result_bytes=1048576,
            context_max_bytes=4194304,
            context_compaction_percent=75,
            max_parallel_reads=4,
            continue_as_new_checkpoints=20,
        ),
    )


def _request() -> OneStepModelRequest:
    registry = DurableToolRegistry()
    catalog = registry.catalog(policy_hash=_HASH, package_hash="b" * 64)
    messages = ({"role": "user", "contents": [{"type": "text", "text": "hi"}]},)
    context = WorkingContextV1(
        bundle=MAFMessageBundleV1.create(
            messages=messages,
            maf_core_version="1.17.0",
            provider="openai",
            model="model",
            api_version="responses-v1",
        ),
        compaction_generation=0,
        source_audit_hash=canonical_hash(messages),
        source_start=0,
        source_end=1,
        estimated_tokens=4,
    )
    return OneStepModelRequest(
        identity=_identity(),
        step_index=0,
        instructions="Help.",
        working_context=context,
        catalog=catalog,
        model_settings={"temperature": 0},
    )


async def _frozen_skill_catalog(
    store: InMemoryDurableContentStore,
) -> tuple[
    ContentRefV1,
    FrozenDurableSkillCatalogV1,
    tuple[DurableSkillContentV1, ...],
]:
    metadata: list[DurableSkillMetadataV1] = []
    materialized: list[MaterializedDurableSkillV1] = []
    contents: list[DurableSkillContentV1] = []
    for skill_id in ("alpha", "beta"):
        content = DurableSkillContentV1.create(
            skill_id=skill_id,
            version="1",
            files=(
                DurableSkillContentFileV1(
                    relative_path="SKILL.md",
                    content=f"Use {skill_id}.",
                ),
            ),
        )
        content_ref = await put_protocol_model(
            store,
            kind="skill-content",
            model=content,
            retention_class="skill",
        )
        metadata.append(
            DurableSkillMetadataV1(
                skill_id=skill_id,
                display_name=skill_id.title(),
                selection_description=f"Guidance for {skill_id}",
                version="1",
                content_hash=content.content_hash,
                tags=("test",),
            )
        )
        materialized.append(
            MaterializedDurableSkillV1(
                skill_id=skill_id,
                version="1",
                content_hash=content.content_hash,
                content_ref=content_ref,
            )
        )
        contents.append(content)
    snapshot = DurableSkillCatalogSnapshotV1.create(
        provider_id="packaged",
        catalog_revision="revision-1",
        metadata=tuple(metadata),
        snapshot_token="token",
        retain_until=datetime(2026, 10, 1, tzinfo=UTC),
    )
    frozen = FrozenDurableSkillCatalogV1(
        snapshot=snapshot,
        materialized=tuple(materialized),
    )
    catalog_ref = await put_protocol_model(
        store,
        kind="skill-catalog",
        model=frozen,
        retention_class="skill",
    )
    return catalog_ref, frozen, tuple(contents)


@pytest.mark.asyncio
async def test_frozen_skill_search_is_metadata_only_and_cursor_bound() -> None:
    store = InMemoryDurableContentStore()
    catalog_ref, frozen, _ = await _frozen_skill_catalog(store)

    result = await search_frozen_skill_catalog(
        store,
        catalog_ref=catalog_ref,
        operation_key=_HASH,
        query="alpha",
        cursor=None,
        maximum_result_bytes=4096,
    )
    page = await get_protocol_model(
        store,
        result.page_ref,
        DurableSkillMetadataPageV1,
    )

    assert [item.skill_id for item in page.metadata] == ["alpha"]
    assert page.catalog_hash == frozen.snapshot.catalog_hash
    assert "content_ref" not in page.model_dump_json()
    with pytest.raises(DurableSkillOperationError, match="invalid_skill_cursor"):
        await search_frozen_skill_catalog(
            store,
            catalog_ref=catalog_ref,
            operation_key="b" * 64,
            query="alpha",
            cursor="invalid",
            maximum_result_bytes=4096,
        )
    with pytest.raises(
        DurableSkillOperationError,
        match="skill_catalog_unavailable",
    ) as unavailable:
        await search_frozen_skill_catalog(
            store,
            catalog_ref=catalog_ref.model_copy(update={"sha256": "f" * 64}),
            operation_key="c" * 64,
            query=None,
            cursor=None,
            maximum_result_bytes=4096,
        )
    assert unavailable.value.terminal is True


@pytest.mark.asyncio
async def test_frozen_skill_load_is_ref_only_and_duplicate_is_idempotent() -> None:
    store = InMemoryDurableContentStore()
    catalog_ref, frozen, contents = await _frozen_skill_catalog(store)

    result, receipt = await load_frozen_skill(
        store,
        catalog_ref=catalog_ref,
        run_id="run-1",
        operation_key=_HASH,
        stable_step_id="skill-step-1",
        skill_id="alpha",
        version="1",
        loaded_receipts=(),
        completed_skill_loads=0,
        maximum_skill_loads=1,
        loaded_skill_bytes=0,
        maximum_loaded_skill_bytes=4096,
        base_context_bytes=0,
        context_max_bytes=8192,
        loaded_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    duplicate, duplicate_receipt = await load_frozen_skill(
        store,
        catalog_ref=catalog_ref,
        run_id="run-1",
        operation_key="b" * 64,
        stable_step_id="skill-step-2",
        skill_id="alpha",
        version="1",
        loaded_receipts=(receipt,),
        completed_skill_loads=1,
        maximum_skill_loads=1,
        loaded_skill_bytes=result.content_ref.byte_length,
        maximum_loaded_skill_bytes=4096,
        base_context_bytes=0,
        context_max_bytes=8192,
        loaded_at=datetime(2026, 9, 4, tzinfo=UTC),
    )

    assert result.content_hash == contents[0].content_hash
    assert "files" not in result.model_dump_json()
    assert duplicate.already_loaded is True
    assert duplicate_receipt.already_loaded is True
    assert await resolve_frozen_skill_content_hash(
        store,
        catalog_ref=catalog_ref,
        skill_id="beta",
        version="1",
    ) == frozen.snapshot.metadata[1].content_hash
    composed = compose_durable_skill_instructions(
        "Root.",
        frozen.snapshot.metadata,
        (contents[0],),
    )
    assert composed.index("Root.") < composed.index("metadata only") < composed.index(
        "Loaded durable instruction skill alpha@1"
    )
    with pytest.raises(
        DurableSkillOperationError,
        match="skill_load_budget_exceeded",
    ):
        await load_frozen_skill(
            store,
            catalog_ref=catalog_ref,
            run_id="run-1",
            operation_key="c" * 64,
            stable_step_id="skill-step-3",
            skill_id="beta",
            version="1",
            loaded_receipts=(receipt,),
            completed_skill_loads=1,
            maximum_skill_loads=1,
            loaded_skill_bytes=result.content_ref.byte_length,
            maximum_loaded_skill_bytes=4096,
            base_context_bytes=0,
            context_max_bytes=8192,
            loaded_at=datetime(2026, 9, 4, tzinfo=UTC),
        )
    with pytest.raises(
        DurableSkillOperationError,
        match="skill_context_budget_exceeded",
    ):
        await load_frozen_skill(
            store,
            catalog_ref=catalog_ref,
            run_id="run-1",
            operation_key="d" * 64,
            stable_step_id="skill-step-4",
            skill_id="beta",
            version="1",
            loaded_receipts=(receipt,),
            completed_skill_loads=1,
            maximum_skill_loads=2,
            loaded_skill_bytes=result.content_ref.byte_length,
            maximum_loaded_skill_bytes=4096,
            base_context_bytes=8190,
            context_max_bytes=8192,
            loaded_at=datetime(2026, 9, 4, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_content_store_is_immutable_and_integrity_checked() -> None:
    store = InMemoryDurableContentStore()
    reference = await store.put_bytes(
        kind="result",
        payload=b"hello",
        media_type="text/plain",
        retention_class="result",
    )

    assert await store.get_bytes(reference) == b"hello"
    with pytest.raises(DurableLoopContentError):
        await store.get_bytes(reference.model_copy(update={"sha256": _HASH}))


def test_blob_content_store_requires_explicit_dedicated_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AzureWebJobsStorage__blobServiceUri",
        "https://hoststorage.blob.core.windows.net",
    )
    monkeypatch.delenv(DURABLE_LOOP_CONTENT_BLOB_URI_ENV, raising=False)
    monkeypatch.delenv(DURABLE_LOOP_CONTENT_CONTAINER_ENV, raising=False)

    with pytest.raises(DurableLoopContentError, match="CONTENT_BLOB_URI"):
        BlobDurableContentStore.from_environment()

    monkeypatch.setenv(
        DURABLE_LOOP_CONTENT_BLOB_URI_ENV,
        "https://content.blob.core.windows.net?sig=secret",
    )
    monkeypatch.setenv(DURABLE_LOOP_CONTENT_CONTAINER_ENV, "durable-content")
    with pytest.raises(DurableLoopContentError, match="credential-free HTTPS"):
        BlobDurableContentStore.from_environment()


@pytest.mark.asyncio
async def test_external_protocol_documents_reject_duplicate_json_keys() -> None:
    store = InMemoryDurableContentStore()
    reference = await store.put_bytes(
        kind="duplicate",
        payload=b'{"input_tokens":1,"input_tokens":2}',
        media_type="application/json",
        retention_class="run",
    )

    with pytest.raises(DurableLoopProtocolDocumentError):
        await get_protocol_model(store, reference, UsageV1)


@pytest.mark.asyncio
async def test_compaction_preserves_atomic_call_result_group() -> None:
    request = _request()
    messages = (
        {"role": "system", "contents": [{"type": "text", "text": "system"}]},
        {"role": "user", "contents": [{"type": "text", "text": "objective"}]},
        {
            "role": "assistant",
            "contents": [
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "read",
                    "arguments": {},
                }
            ],
        },
        {
            "role": "tool",
            "contents": [
                {
                    "type": "function_result",
                    "call_id": "call-1",
                    "name": "read",
                    "result": "one",
                }
            ],
        },
        {
            "role": "assistant",
            "contents": [
                {
                    "type": "function_call",
                    "call_id": "call-2",
                    "name": "read",
                    "arguments": {},
                }
            ],
        },
        {
            "role": "tool",
            "contents": [
                {
                    "type": "function_result",
                    "call_id": "call-2",
                    "name": "read",
                    "result": "two",
                }
            ],
        },
    )
    bundle = MAFMessageBundleV1.create(
        messages=messages,
        maf_core_version="1.17.0",
        provider="openai",
        model="model",
        api_version="responses-v1",
    )
    context = request.working_context.model_copy(
        update={
            "bundle": bundle,
            "source_end": len(messages),
            "source_audit_hash": canonical_hash(messages),
        }
    )

    compacted = await DeterministicContextCompactor(retain_groups=1).compact(
        context,
        maximum_bytes=10000,
    )

    assert compacted.compaction_generation == 1
    assert compacted.source_audit_hash == context.source_audit_hash
    assert compacted.bundle.messages[-2:] == messages[-2:]
    assert compacted.bundle.messages[0] == messages[0]
    assert compacted.bundle.messages[-3]["role"] == "user"
    assert "Omitted message hash" in compacted.bundle.messages[-3]["contents"][0]["text"]  # type: ignore[index]
    assert "returned read" in compacted.bundle.messages[-3]["contents"][0]["text"]  # type: ignore[index]
    assert "one" in compacted.bundle.messages[-3]["contents"][0]["text"]  # type: ignore[index]


@pytest.mark.asyncio
async def test_compaction_fails_when_no_atomic_group_can_be_removed() -> None:
    with pytest.raises(DurableLoopContextOverflowError):
        await DeterministicContextCompactor(retain_groups=4).compact(
            _request().working_context,
            maximum_bytes=100,
        )


@pytest.mark.asyncio
async def test_background_provider_pins_affinity_and_reuses_operation() -> None:
    request = _request()
    decision = ScriptedModelStep(final_text="done").build(request)
    provider = FakeBackgroundModelProvider(
        decision,
        polls_before_terminal=1,
        backend_binding_hash="b" * 64,
    )

    started = await provider.start(request)
    assert started.disposition is BackgroundStartDisposition.ACCEPTED
    assert started.operation is not None
    assert started.operation.provider_response_ref is not None
    assert "background-0" not in started.operation.model_dump_json()
    pending = await provider.poll(started.operation)
    assert pending.operation is not None
    assert pending.operation.status is ModelOperationStatus.IN_PROGRESS
    terminal = await provider.poll(pending.operation)
    assert terminal.decision == decision
    assert provider.starts == 1
    cancelled = await provider.cancel(pending.operation)
    assert cancelled.status is ModelOperationStatus.CANCELLED
    assert provider.cancels == 1


@pytest.mark.asyncio
async def test_background_lost_start_acknowledgement_is_ambiguous() -> None:
    request = _request()
    decision = ScriptedModelStep(final_text="done").build(request)
    provider = FakeBackgroundModelProvider(
        decision,
        lose_start_acknowledgement=True,
    )

    started = await provider.start(request)

    assert started.disposition is BackgroundStartDisposition.LOST_ACKNOWLEDGEMENT
    assert started.error is not None
    assert started.error.disposition is ErrorDisposition.AMBIGUOUS
