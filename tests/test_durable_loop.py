from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from azure_functions_agents.experimental.durable_loop import (
    DurableLoopExecutionResult,
    DurableLoopFaultInjector,
    DurableLoopPlan,
    DurableLoopRunner,
    HumanEventDeliveryDisposition,
    InMemoryHumanEventPublisher,
    create_run_identity,
)
from azure_functions_agents.experimental.durable_loop_activities import (
    DeterministicContextCompactor,
    ExactBindingResumeValidator,
    InMemoryDurableContentStore,
    ResumeContextDisposition,
    ScriptedModelStep,
    ScriptedOneStepModelProvider,
)
from azure_functions_agents.experimental.durable_loop_config import (
    DURABLE_LOOP_CONTEXT_MAX_BYTES_ENV,
    DURABLE_LOOP_CONTINUE_AS_NEW_CHECKPOINTS_ENV,
    DURABLE_LOOP_ENABLED_ENV,
    DURABLE_LOOP_INPUT_COST_RATE_ENV,
    DURABLE_LOOP_MAX_ARGUMENT_BYTES_ENV,
    DURABLE_LOOP_MAX_COST_MICROUNITS_ENV,
    DURABLE_LOOP_MAX_EXTERNAL_CONTENT_BYTES_ENV,
    DURABLE_LOOP_MAX_MODEL_STEPS_ENV,
    DURABLE_LOOP_MAX_RESULT_BYTES_ENV,
    DURABLE_LOOP_MAX_TOTAL_TOKENS_ENV,
    DURABLE_LOOP_OUTPUT_COST_RATE_ENV,
    DurableLoopSettings,
)
from azure_functions_agents.experimental.durable_loop_protocol import (
    DurableLoopRunStatus,
    ErrorDisposition,
    FrozenToolDescriptorV1,
    ToolBehavior,
    ToolProvenance,
)
from azure_functions_agents.experimental.durable_loop_state import (
    DurableLoopHumanInputConflictError,
    DurableLoopIdempotencyConflictError,
    InMemoryActivityJournal,
    InMemoryDurableLoopStateStore,
)
from azure_functions_agents.experimental.durable_loop_tools import (
    DurableToolAmbiguousError,
    DurableToolRegistry,
)

_HASH = "a" * 64


class _FailOnce(DurableLoopFaultInjector):
    def __init__(self, point: str) -> None:
        self._point = point
        self._failed = False

    async def reach(self, point: str, run_id: str) -> None:
        del run_id
        if point == self._point and not self._failed:
            self._failed = True
            raise RuntimeError(f"injected {point}")


def _settings(**overrides: str) -> DurableLoopSettings:
    settings = DurableLoopSettings.from_environment(
        {DURABLE_LOOP_ENABLED_ENV: "true", **overrides}
    )
    assert settings is not None
    return settings


def _build(
    steps: list[ScriptedModelStep],
    *,
    settings: DurableLoopSettings | None = None,
    fault: DurableLoopFaultInjector | None = None,
    compactor: DeterministicContextCompactor | None = None,
    state: InMemoryDurableLoopStateStore | None = None,
    clock: Callable[[], datetime] | None = None,
):
    registry = DurableToolRegistry()
    resolved_state = state or InMemoryDurableLoopStateStore()
    calls: list[tuple[str, str]] = []

    async def read_handler(arguments, call_key):
        calls.append(("read", call_key))
        await asyncio.sleep(0)
        return {"value": arguments.get("value")}

    def write_handler(arguments, call_key):
        calls.append(("write", call_key))
        return {"written": arguments.get("value")}

    def ambiguous_handler(arguments, call_key):
        calls.append(("ambiguous", call_key))
        del arguments
        raise DurableToolAmbiguousError("write acknowledgement was lost")

    async def cancel_write_handler(arguments, call_key):
        calls.append(("cancel-write", call_key))
        del arguments
        await resolved_state.request_cancel("run-1")
        return {"written": True}

    registry.register(
        FrozenToolDescriptorV1(
            name="read_value",
            description="Read a value.",
            parameters={"additionalProperties": True, "type": "object"},
            provenance=ToolProvenance.REMOTE,
            behavior=ToolBehavior.READ_ONLY,
            parallel_safe=True,
        ),
        read_handler,
    )
    registry.register(
        FrozenToolDescriptorV1(
            name="write_value",
            description="Write a value idempotently.",
            parameters={"additionalProperties": True, "type": "object"},
            provenance=ToolProvenance.REMOTE,
            behavior=ToolBehavior.IDEMPOTENT_WRITE,
        ),
        write_handler,
    )
    registry.register(
        FrozenToolDescriptorV1(
            name="ambiguous_write",
            description="Write with an unreconciled acknowledgement.",
            parameters={"additionalProperties": True, "type": "object"},
            provenance=ToolProvenance.REMOTE,
            behavior=ToolBehavior.MUTATING,
        ),
        ambiguous_handler,
    )
    registry.register(
        FrozenToolDescriptorV1(
            name="cancel_write",
            description="Write once and request cancellation.",
            parameters={"additionalProperties": True, "type": "object"},
            provenance=ToolProvenance.REMOTE,
            behavior=ToolBehavior.MUTATING,
        ),
        cancel_write_handler,
    )
    catalog = registry.catalog(policy_hash=_HASH, package_hash="b" * 64)
    resolved_settings = settings or _settings()
    plan = DurableLoopPlan(
        instructions="Use tools and ask for clarification when needed.",
        catalog=catalog,
        model_settings={"temperature": 0},
        maf_core_version="1.17.0",
        provider="fake",
        model="fake-reasoning",
        api_version="responses-v1",
        settings=resolved_settings,
    )
    state = resolved_state
    content = InMemoryDurableContentStore()
    model = ScriptedOneStepModelProvider(steps)
    journal = InMemoryActivityJournal()
    events = InMemoryHumanEventPublisher()
    runner = DurableLoopRunner(
        state=state,
        content=content,
        model=model,
        tools=registry.build_dispatcher(),
        activity_journal=journal,
        compactor=compactor,
        event_publisher=events,
        fault_injector=fault,
        clock=clock or (lambda: datetime(2026, 9, 4, tzinfo=UTC)),
        nonce_factory=lambda: "nonce-1",
    )
    identity = create_run_identity(
        run_id="run-1",
        session_id="session-1",
        request_id="request-1",
        request_body={"prompt": "do work"},
        owner_hash="c" * 64,
        agent_slug="main",
        agent_hash="d" * 64,
        catalog_hash=catalog.catalog_hash,
        deployment_hash="e" * 64,
        tool_package_hash=catalog.package_hash,
        policy_hash=catalog.policy_hash,
        settings=resolved_settings,
        now=datetime(2026, 9, 4, tzinfo=UTC),
    )
    return runner, state, content, model, journal, events, identity, plan, calls


async def _start(runner, identity, plan) -> DurableLoopExecutionResult:
    return await runner.start(
        identity,
        plan,
        [{"role": "user", "contents": [{"type": "text", "text": "do work"}]}],
    )


@pytest.mark.asyncio
async def test_adaptive_loop_runs_parallel_reads_then_serial_write_and_commits() -> None:
    built = _build(
        [
            ScriptedModelStep(
                calls=(
                    ("call-1", "read_value", {"value": 1}),
                    ("call-2", "read_value", {"value": 2}),
                )
            ),
            ScriptedModelStep(
                calls=(("call-3", "write_value", {"value": 3}),)
            ),
            ScriptedModelStep(final_text="done"),
        ]
    )
    runner, _state, content, model, _journal, _events, identity, plan, calls = built

    result = await _start(runner, identity, plan)

    assert result.status.status is DurableLoopRunStatus.COMPLETED
    assert result.final_result is not None
    assert result.final_result.response_ref is not None
    assert await content.get_bytes(result.final_result.response_ref) == b"done"
    assert len(model.calls) == 3
    assert [kind for kind, _key in calls] == ["read", "read", "write"]
    second_step_messages = model.calls[1].working_context.bundle.messages
    assert [message["role"] for message in second_step_messages[-3:]] == [
        "assistant",
        "tool",
        "tool",
    ]


@pytest.mark.asyncio
async def test_human_clarification_buffers_answer_and_continues_with_matching_call_id() -> None:
    built = _build(
        [
            ScriptedModelStep(
                calls=(
                    (
                        "human-call",
                        "request_human_input",
                        {
                            "question": "Which region?",
                            "choices": ["eastus2", "westus3"],
                            "allow_free_text": False,
                        },
                    ),
                )
            ),
            ScriptedModelStep(final_text="Investigated westus3."),
        ]
    )
    runner, _state, _content, model, _journal, events, identity, plan, calls = built

    waiting = await _start(runner, identity, plan)
    assert waiting.status.status is DurableLoopRunStatus.WAITING
    assert waiting.human_input is not None
    assert waiting.human_input.question == "Which region?"
    submission = await runner.submit_human_input(
        run_id=identity.run_id,
        request_id=waiting.human_input.request_id,
        submission_id="submission-1",
        actor_hash=identity.owner_hash,
        answer="westus3",
    )
    assert submission.delivery is HumanEventDeliveryDisposition.DELIVERED
    assert len(events.events) == 1

    completed = await runner.resume(identity.run_id)

    assert completed.status.status is DurableLoopRunStatus.COMPLETED
    assert not calls
    continuation = model.calls[1].working_context.bundle.messages
    assert continuation[-1]["role"] == "tool"
    result_content = continuation[-1]["contents"][0]  # type: ignore[index]
    assert result_content["call_id"] == "human-call"  # type: ignore[index]
    assert result_content["result"]["answer"] == "westus3"  # type: ignore[index]


@pytest.mark.asyncio
async def test_duplicate_and_conflicting_human_answers_follow_first_answer_ledger() -> None:
    built = _build(
        [
            ScriptedModelStep(
                calls=(
                    (
                        "human-call",
                        "request_human_input",
                        {
                            "question": "Which region?",
                            "choices": ["eastus2", "westus3"],
                            "allow_free_text": False,
                        },
                    ),
                )
            ),
            ScriptedModelStep(final_text="done"),
        ]
    )
    runner, _state, _content, _model, _journal, _events, identity, plan, _calls = built
    waiting = await _start(runner, identity, plan)
    assert waiting.human_input is not None

    first = await runner.submit_human_input(
        run_id=identity.run_id,
        request_id=waiting.human_input.request_id,
        submission_id="submission-1",
        actor_hash=identity.owner_hash,
        answer="westus3",
    )
    duplicate = await runner.submit_human_input(
        run_id=identity.run_id,
        request_id=waiting.human_input.request_id,
        submission_id="submission-1",
        actor_hash=identity.owner_hash,
        answer="westus3",
    )

    assert not first.acceptance.replayed
    assert duplicate.acceptance.replayed
    with pytest.raises(DurableLoopHumanInputConflictError):
        await runner.submit_human_input(
            run_id=identity.run_id,
            request_id=waiting.human_input.request_id,
            submission_id="submission-2",
            actor_hash=identity.owner_hash,
            answer="eastus2",
        )


@pytest.mark.asyncio
async def test_mixed_clarification_executes_nothing_and_allows_one_repair_step() -> None:
    built = _build(
        [
            ScriptedModelStep(
                calls=(
                    (
                        "human-call",
                        "request_human_input",
                        {
                            "question": "Which region?",
                            "choices": ["westus3"],
                            "allow_free_text": False,
                        },
                    ),
                    ("write-call", "write_value", {"value": 1}),
                )
            ),
            ScriptedModelStep(final_text="repaired"),
        ]
    )
    runner, _state, _content, model, _journal, _events, identity, plan, calls = built

    result = await _start(runner, identity, plan)

    assert result.status.status is DurableLoopRunStatus.COMPLETED
    assert not calls
    repair_messages = model.calls[1].working_context.bundle.messages[-3:]
    assert [message["role"] for message in repair_messages] == [
        "assistant",
        "tool",
        "tool",
    ]


@pytest.mark.asyncio
async def test_second_invalid_clarification_batch_fails_closed() -> None:
    invalid = ScriptedModelStep(
        calls=(
            (
                "human-call",
                "request_human_input",
                {
                    "question": "Which region?",
                    "choices": ["westus3"],
                    "allow_free_text": False,
                },
            ),
            ("write-call", "write_value", {"value": 1}),
        )
    )
    built = _build([invalid, invalid])
    runner, _state, _content, _model, _journal, _events, identity, plan, calls = built

    result = await _start(runner, identity, plan)

    assert result.status.status is DurableLoopRunStatus.FAILED
    assert result.status.error is not None
    assert result.status.error.code == "invalid_clarification_batch"
    assert not calls


@pytest.mark.asyncio
async def test_replay_after_model_result_does_not_repeat_model_inference() -> None:
    built = _build(
        [ScriptedModelStep(final_text="done")],
        fault=_FailOnce("after_model_result"),
    )
    runner, _state, _content, model, journal, _events, identity, plan, _calls = built

    with pytest.raises(RuntimeError, match="after_model_result"):
        await _start(runner, identity, plan)
    completed = await runner.resume(identity.run_id)

    assert completed.status.status is DurableLoopRunStatus.COMPLETED
    assert len(model.calls) == 1
    assert sum(journal.executions.values()) == 1


@pytest.mark.asyncio
async def test_replay_after_tool_checkpoint_does_not_repeat_side_effect() -> None:
    built = _build(
        [
            ScriptedModelStep(
                calls=(("write-call", "write_value", {"value": 1}),)
            ),
            ScriptedModelStep(final_text="done"),
        ],
        fault=_FailOnce("after_tool_checkpoint"),
    )
    runner, _state, _content, model, _journal, _events, identity, plan, calls = built

    with pytest.raises(RuntimeError, match="after_tool_checkpoint"):
        await _start(runner, identity, plan)
    completed = await runner.resume(identity.run_id)

    assert completed.status.status is DurableLoopRunStatus.COMPLETED
    assert [kind for kind, _key in calls] == ["write"]
    assert len(model.calls) == 2


@pytest.mark.asyncio
async def test_commit_acknowledgement_loss_returns_recorded_commit_receipt() -> None:
    built = _build(
        [ScriptedModelStep(final_text="done")],
        fault=_FailOnce("after_commit"),
    )
    runner, _state, _content, model, _journal, _events, identity, plan, _calls = built

    with pytest.raises(RuntimeError, match="after_commit"):
        await _start(runner, identity, plan)
    completed = await runner.resume(identity.run_id)

    assert completed.status.status is DurableLoopRunStatus.COMPLETED
    assert completed.final_result is not None
    assert completed.final_result.committed_generation == 1
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_ambiguous_tool_outcome_terminalizes_without_model_recovery() -> None:
    built = _build(
        [
            ScriptedModelStep(
                calls=(("ambiguous-call", "ambiguous_write", {"value": 1}),)
            ),
            ScriptedModelStep(final_text="must not become success"),
        ]
    )
    runner, state, _content, model, _journal, _events, identity, plan, calls = built

    result = await _start(runner, identity, plan)

    assert result.status.status is DurableLoopRunStatus.FAILED
    assert result.status.error is not None
    assert result.status.error.disposition is ErrorDisposition.AMBIGUOUS
    assert result.status.error.possibly_committed is True
    assert len(model.calls) == 1
    assert [kind for kind, _key in calls] == ["ambiguous"]
    assert await state.get_session_generation(identity.session_id) == 0


@pytest.mark.asyncio
async def test_local_runner_stops_serial_tools_after_cancellation() -> None:
    built = _build(
        [
            ScriptedModelStep(
                calls=(
                    ("call-1", "cancel_write", {"value": 1}),
                    ("call-2", "write_value", {"value": 2}),
                )
            )
        ]
    )
    runner, state, _content, model, _journal, _events, identity, plan, calls = built

    result = await _start(runner, identity, plan)
    record = await state.get_run(identity.run_id)

    assert result.status.status is DurableLoopRunStatus.CANCELLED
    assert [kind for kind, _key in calls] == ["cancel-write"]
    assert len(model.calls) == 1
    cancelled_result = record.checkpoint.working_context.bundle.messages[-1]
    assert cancelled_result["contents"][0]["result"]["status"] == "cancelled"  # type: ignore[index]


@pytest.mark.asyncio
async def test_cancellation_wins_race_before_final_commit() -> None:
    class CancelBeforeCommitState(InMemoryDurableLoopStateStore):
        def __init__(self) -> None:
            super().__init__()
            self.injected = False

        async def save_checkpoint(self, run_id, checkpoint):
            if checkpoint.status is DurableLoopRunStatus.COMPLETED and not self.injected:
                self.injected = True
                await self.request_cancel(run_id)
            await super().save_checkpoint(run_id, checkpoint)

    state = CancelBeforeCommitState()
    built = _build(
        [ScriptedModelStep(final_text="must not commit")],
        state=state,
    )
    runner, _state, _content, model, _journal, _events, identity, plan, _calls = built

    cancelled = await _start(runner, identity, plan)

    assert cancelled.status.status is DurableLoopRunStatus.CANCELLED
    assert await state.get_session_generation(identity.session_id) == 0
    assert await state.get_committed_context(identity.session_id) is None
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_cancel_and_model_budget_terminalize_without_more_work() -> None:
    built = _build(
        [
            ScriptedModelStep(
                calls=(("read-call", "read_value", {"value": 1}),)
            ),
            ScriptedModelStep(final_text="should not run"),
        ],
        settings=_settings(**{DURABLE_LOOP_MAX_MODEL_STEPS_ENV: "1"}),
    )
    runner, state, _content, model, _journal, _events, identity, plan, _calls = built

    result = await _start(runner, identity, plan)

    assert result.status.status is DurableLoopRunStatus.FAILED
    assert result.status.error is not None
    assert result.status.error.code == "model_step_budget_exceeded"
    assert len(model.calls) == 1
    assert await state.get_session_generation(identity.session_id) == 0
    assert await state.get_committed_context(identity.session_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settings", "step", "error_code"),
    [
        (
            _settings(**{DURABLE_LOOP_MAX_TOTAL_TOKENS_ENV: "3"}),
            ScriptedModelStep(
                final_text="over budget",
                input_tokens=2,
                output_tokens=2,
                reasoning_tokens=2,
            ),
            "model_token_budget_exceeded",
        ),
        (
            _settings(
                **{
                    DURABLE_LOOP_INPUT_COST_RATE_ENV: "1",
                    DURABLE_LOOP_MAX_COST_MICROUNITS_ENV: "5",
                    DURABLE_LOOP_OUTPUT_COST_RATE_ENV: "1",
                }
            ),
            ScriptedModelStep(
                final_text="over budget",
                cost_microunits=6,
            ),
            "model_cost_budget_exceeded",
        ),
        (
            _settings(
                **{
                    DURABLE_LOOP_MAX_ARGUMENT_BYTES_ENV: "1024",
                    DURABLE_LOOP_MAX_EXTERNAL_CONTENT_BYTES_ENV: "2048",
                    DURABLE_LOOP_MAX_RESULT_BYTES_ENV: "1024",
                }
            ),
            ScriptedModelStep(final_text="x" * 2049),
            "external_content_budget_exceeded",
        ),
    ],
)
async def test_model_usage_budgets_stop_before_commit(
    settings: DurableLoopSettings,
    step: ScriptedModelStep,
    error_code: str,
) -> None:
    built = _build([step], settings=settings)
    runner, state, _content, model, _journal, _events, identity, plan, _calls = built

    result = await _start(runner, identity, plan)
    record = await state.get_run(identity.run_id)

    assert result.status.status is DurableLoopRunStatus.FAILED
    assert result.status.error is not None
    assert result.status.error.code == error_code
    assert result.status.reasoning_tokens == step.reasoning_tokens
    assert len(model.calls) == 1
    assert await state.get_session_generation(identity.session_id) == 0
    assert record.checkpoint.reasoning_tokens == step.reasoning_tokens


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settings", "argument", "error_code", "expected_calls"),
    [
        (
            _settings(
                **{
                    DURABLE_LOOP_MAX_ARGUMENT_BYTES_ENV: "1024",
                    DURABLE_LOOP_MAX_RESULT_BYTES_ENV: "2048",
                }
            ),
            "x" * 1100,
            "tool_arguments_too_large",
            [],
        ),
        (
            _settings(
                **{
                    DURABLE_LOOP_MAX_ARGUMENT_BYTES_ENV: "2048",
                    DURABLE_LOOP_MAX_RESULT_BYTES_ENV: "1024",
                }
            ),
            "x" * 1100,
            "tool_result_too_large",
            ["read"],
        ),
    ],
)
async def test_configured_tool_payload_limits_are_enforced(
    settings: DurableLoopSettings,
    argument: str,
    error_code: str,
    expected_calls: list[str],
) -> None:
    built = _build(
        [
            ScriptedModelStep(
                calls=(("call-1", "read_value", {"value": argument}),)
            ),
            ScriptedModelStep(final_text="handled"),
        ],
        settings=settings,
    )
    runner, _state, _content, model, _journal, _events, identity, plan, calls = built

    completed = await _start(runner, identity, plan)

    assert completed.status.status is DurableLoopRunStatus.COMPLETED
    assert [kind for kind, _ in calls] == expected_calls
    result = model.calls[1].working_context.bundle.messages[-1]["contents"][0]["result"]  # type: ignore[index]
    assert result["error_code"] == error_code  # type: ignore[index]


@pytest.mark.asyncio
async def test_cancelling_human_wait_releases_session_without_committing_history() -> None:
    built = _build(
        [
            ScriptedModelStep(
                calls=(
                    (
                        "human-call",
                        "request_human_input",
                        {
                            "question": "Continue?",
                            "choices": ["yes"],
                            "allow_free_text": False,
                        },
                    ),
                )
            )
        ]
    )
    runner, state, _content, _model, _journal, _events, identity, plan, _calls = built
    waiting = await _start(runner, identity, plan)
    assert waiting.status.status is DurableLoopRunStatus.WAITING

    cancelled = await runner.cancel(identity.run_id)

    assert cancelled.status.status is DurableLoopRunStatus.CANCELLED
    assert await state.get_session_generation(identity.session_id) == 0
    assert await state.get_committed_context(identity.session_id) is None


@pytest.mark.asyncio
async def test_human_timeout_appends_matching_tool_result_and_allows_conclusion() -> None:
    built = _build(
        [
            ScriptedModelStep(
                calls=(
                    (
                        "human-call",
                        "request_human_input",
                        {
                            "question": "Continue?",
                            "choices": ["yes"],
                            "allow_free_text": False,
                        },
                    ),
                )
            ),
            ScriptedModelStep(final_text="No answer was received."),
        ]
    )
    runner, _state, _content, model, _journal, _events, identity, plan, _calls = built
    waiting = await _start(runner, identity, plan)
    assert waiting.human_input is not None

    completed = await runner.timeout_human_input(
        run_id=identity.run_id,
        request_id=waiting.human_input.request_id,
    )

    assert completed.status.status is DurableLoopRunStatus.COMPLETED
    tool_message = model.calls[1].working_context.bundle.messages[-1]
    result = tool_message["contents"][0]  # type: ignore[index]
    assert result["call_id"] == "human-call"  # type: ignore[index]
    assert result["result"]["status"] == "timed_out"  # type: ignore[index]


@pytest.mark.asyncio
async def test_human_park_does_not_consume_active_execution_budget() -> None:
    now = [datetime(2026, 9, 4, tzinfo=UTC)]
    built = _build(
        [
            ScriptedModelStep(
                calls=(
                    (
                        "human-call",
                        "request_human_input",
                        {
                            "question": "Continue?",
                            "choices": ["yes"],
                            "allow_free_text": False,
                        },
                    ),
                )
            ),
            ScriptedModelStep(final_text="resumed"),
        ],
        clock=lambda: now[0],
    )
    runner, state, _content, _model, _journal, _events, identity, plan, _calls = built
    waiting = await _start(runner, identity, plan)
    assert waiting.human_input is not None
    now[0] = now[0].replace(hour=5)
    await runner.submit_human_input(
        run_id=identity.run_id,
        request_id=waiting.human_input.request_id,
        submission_id="submission-1",
        actor_hash=identity.owner_hash,
        answer="yes",
    )

    completed = await runner.resume(identity.run_id)
    record = await state.get_run(identity.run_id)

    assert completed.status.status is DurableLoopRunStatus.COMPLETED
    assert record.checkpoint.parked_seconds == 5 * 60 * 60


@pytest.mark.asyncio
async def test_long_park_rehydration_preserves_audit_and_does_not_repeat_tools() -> None:
    class RehydrateValidator(ExactBindingResumeValidator):
        async def validate(self, *args, **kwargs):
            return ResumeContextDisposition.REHYDRATE

    built = _build(
        [
            ScriptedModelStep(
                calls=(("read-call", "read_value", {"value": 1}),)
            ),
            ScriptedModelStep(
                calls=(
                    (
                        "human-call",
                        "request_human_input",
                        {
                            "question": "Continue?",
                            "choices": ["yes"],
                            "allow_free_text": False,
                        },
                    ),
                )
            ),
            ScriptedModelStep(final_text="done"),
        ]
    )
    (
        _runner,
        state,
        content,
        model,
        journal,
        events,
        identity,
        plan,
        calls,
    ) = built
    runner = DurableLoopRunner(
        state=state,
        content=content,
        model=model,
        tools=DurableToolRegistry().build_dispatcher(),
        activity_journal=journal,
        event_publisher=events,
        resume_validator=RehydrateValidator(),
        clock=lambda: datetime(2026, 9, 4, tzinfo=UTC),
        nonce_factory=lambda: "nonce-1",
    )
    # The read tool is needed only before the human wait.
    tool_registry = DurableToolRegistry()
    descriptor = plan.catalog.by_name()["read_value"]

    async def read_handler(arguments, call_key):
        calls.append(("read", call_key))
        return {"value": arguments["value"]}

    tool_registry.register(descriptor, read_handler)
    runner = DurableLoopRunner(
        state=state,
        content=content,
        model=model,
        tools=tool_registry.build_dispatcher(),
        activity_journal=journal,
        event_publisher=events,
        resume_validator=RehydrateValidator(),
        clock=lambda: datetime(2026, 9, 4, tzinfo=UTC),
        nonce_factory=lambda: "nonce-1",
    )

    waiting = await _start(runner, identity, plan)
    assert waiting.human_input is not None
    await runner.submit_human_input(
        run_id=identity.run_id,
        request_id=waiting.human_input.request_id,
        submission_id="submission-1",
        actor_hash=identity.owner_hash,
        answer="yes",
    )
    completed = await runner.resume(identity.run_id)

    assert completed.status.status is DurableLoopRunStatus.COMPLETED
    assert [kind for kind, _key in calls] == ["read"]
    messages = model.calls[-1].working_context.bundle.messages
    assert any(
        "rehydrated this context" in item.get("text", "")
        for message in messages
        for item in message.get("contents", [])
        if isinstance(item, dict)
    )


@pytest.mark.asyncio
async def test_runner_compaction_keeps_complete_immutable_audit_history() -> None:
    large = "x" * 20_000
    built = _build(
        [
            ScriptedModelStep(
                calls=(("read-1", "read_value", {"value": large}),)
            ),
            ScriptedModelStep(
                calls=(("read-2", "read_value", {"value": large}),)
            ),
            ScriptedModelStep(final_text="done"),
        ],
        settings=_settings(
            **{DURABLE_LOOP_CONTEXT_MAX_BYTES_ENV: str(64 * 1024)}
        ),
        compactor=DeterministicContextCompactor(retain_groups=1),
    )
    runner, state, _content, _model, _journal, _events, identity, plan, _calls = built

    completed = await _start(runner, identity, plan)
    record = await state.get_run(identity.run_id)

    assert completed.status.status is DurableLoopRunStatus.COMPLETED
    assert len(record.checkpoint.audit_bundle.messages) == 6
    assert record.checkpoint.audit_head_hash == record.checkpoint.audit_bundle.bundle_hash
    assert record.checkpoint.working_context.compaction_generation == 1
    assert len(record.checkpoint.working_context.bundle.messages) < 6


@pytest.mark.asyncio
async def test_no_continue_as_new_occurs_with_open_human_request() -> None:
    built = _build(
        [
            ScriptedModelStep(
                calls=(
                    (
                        "human-call",
                        "request_human_input",
                        {
                            "question": "Continue?",
                            "choices": ["yes"],
                            "allow_free_text": False,
                        },
                    ),
                )
            )
        ],
        settings=_settings(**{DURABLE_LOOP_CONTINUE_AS_NEW_CHECKPOINTS_ENV: "1"}),
    )
    runner, state, _content, _model, _journal, _events, identity, plan, _calls = built

    waiting = await _start(runner, identity, plan)
    record = await state.get_run(identity.run_id)

    assert waiting.status.status is DurableLoopRunStatus.WAITING
    assert record.checkpoint.continue_as_new_generation == 0
    assert record.checkpoint.pending_human_request_id is not None


@pytest.mark.asyncio
async def test_duplicate_start_dedupes_and_conflicting_body_fails() -> None:
    built = _build([ScriptedModelStep(final_text="done")])
    runner, _state, _content, model, _journal, _events, identity, plan, _calls = built

    first = await _start(runner, identity, plan)
    duplicate = await _start(runner, identity, plan)

    assert first.final_result == duplicate.final_result
    assert len(model.calls) == 1
    conflict = identity.model_copy(
        update={"run_id": "run-2", "request_hash": "f" * 64}
    )
    with pytest.raises(DurableLoopIdempotencyConflictError):
        await _start(runner, conflict, plan)


@pytest.mark.asyncio
async def test_event_delivery_terminal_failure_marks_accepted_answer_orphaned() -> None:
    built = _build(
        [
            ScriptedModelStep(
                calls=(
                    (
                        "human-call",
                        "request_human_input",
                        {
                            "question": "Continue?",
                            "choices": ["yes"],
                            "allow_free_text": False,
                        },
                    ),
                )
            )
        ]
    )
    runner, state, _content, _model, _journal, events, identity, plan, _calls = built
    waiting = await _start(runner, identity, plan)
    assert waiting.human_input is not None
    events.next_disposition = HumanEventDeliveryDisposition.RUN_TERMINAL

    submission = await runner.submit_human_input(
        run_id=identity.run_id,
        request_id=waiting.human_input.request_id,
        submission_id="submission-1",
        actor_hash=identity.owner_hash,
        answer="yes",
    )
    record = await state.get_run(identity.run_id)

    assert submission.delivery is HumanEventDeliveryDisposition.RUN_TERMINAL
    assert record.human_response is not None
    assert record.human_response.disposition.value == "orphaned"


@pytest.mark.asyncio
async def test_completed_context_continues_on_same_session_without_failed_attempts() -> None:
    built = _build([ScriptedModelStep(final_text="first answer")])
    runner, state, content, _model, _journal, _events, identity, plan, _calls = built
    first = await _start(runner, identity, plan)
    assert first.status.status is DurableLoopRunStatus.COMPLETED

    second_model = ScriptedOneStepModelProvider(
        [ScriptedModelStep(final_text="second answer")]
    )
    second_runner = DurableLoopRunner(
        state=state,
        content=content,
        model=second_model,
        tools=DurableToolRegistry().build_dispatcher(),
        clock=lambda: datetime(2026, 9, 4, tzinfo=UTC),
    )
    second_identity = create_run_identity(
        run_id="run-2",
        session_id=identity.session_id,
        request_id="request-2",
        request_body={"prompt": "follow up"},
        owner_hash=identity.owner_hash,
        agent_slug=identity.agent_slug,
        agent_hash=identity.agent_hash,
        catalog_hash=plan.catalog.catalog_hash,
        deployment_hash=identity.deployment_hash,
        tool_package_hash=plan.catalog.package_hash,
        policy_hash=plan.catalog.policy_hash,
        settings=plan.settings,
        now=datetime(2026, 9, 4, tzinfo=UTC),
    )

    second = await second_runner.start(
        second_identity,
        plan,
        [{"role": "user", "contents": [{"type": "text", "text": "follow up"}]}],
    )

    assert second.status.status is DurableLoopRunStatus.COMPLETED
    second_messages = second_model.calls[0].working_context.bundle.messages
    assert [message["role"] for message in second_messages[-2:]] == [
        "assistant",
        "user",
    ]
