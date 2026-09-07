from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from azure_functions_agents.experimental.durable_loop import (
    DurableLoopPlan,
    DurableLoopRunner,
    create_run_identity,
)
from azure_functions_agents.experimental.durable_loop_activities import (
    InMemoryDurableContentStore,
    ScriptedModelStep,
    ScriptedOneStepModelProvider,
)
from azure_functions_agents.experimental.durable_loop_config import (
    DURABLE_LOOP_LOCAL_SAMPLE_ENV,
    DurableLoopSettings,
)
from azure_functions_agents.experimental.durable_loop_protocol import (
    FrozenToolDescriptorV1,
    ToolBehavior,
    ToolProvenance,
)
from azure_functions_agents.experimental.durable_loop_state import (
    InMemoryDurableLoopStateStore,
)
from azure_functions_agents.experimental.durable_loop_tools import (
    DurableToolRegistry,
)

_HASH = "a" * 64


async def main() -> None:
    settings = DurableLoopSettings.from_environment(
        {DURABLE_LOOP_LOCAL_SAMPLE_ENV: "true"}
    )
    assert settings is not None

    registry = DurableToolRegistry()

    async def inspect_region(arguments, call_key):
        return {
            "call_key": call_key,
            "healthy": True,
            "region": arguments["region"],
        }

    registry.register(
        FrozenToolDescriptorV1(
            name="inspect_region",
            description="Read deterministic regional health.",
            parameters={
                "additionalProperties": False,
                "properties": {"region": {"type": "string"}},
                "required": ["region"],
                "type": "object",
            },
            provenance=ToolProvenance.REMOTE,
            behavior=ToolBehavior.READ_ONLY,
            parallel_safe=True,
        ),
        inspect_region,
    )
    catalog = registry.catalog(policy_hash=_HASH, package_hash="b" * 64)
    plan = DurableLoopPlan(
        instructions="Ask for a region, inspect it, and report the result.",
        catalog=catalog,
        model_settings={"temperature": 0},
        maf_core_version="1.17.0",
        provider="fake",
        model="fake-reasoning",
        api_version="responses-v1",
        settings=settings,
    )
    state = InMemoryDurableLoopStateStore()
    content = InMemoryDurableContentStore()
    first_model = ScriptedOneStepModelProvider(
        [
            ScriptedModelStep(
                calls=(
                    (
                        "clarify-region",
                        "request_human_input",
                        {
                            "allow_free_text": False,
                            "choices": ["eastus2", "westus3"],
                            "question": "Which region should I inspect?",
                        },
                    ),
                )
            ),
            ScriptedModelStep(
                calls=(
                    (
                        "inspect-region",
                        "inspect_region",
                        {"region": "westus3"},
                    ),
                )
            ),
            ScriptedModelStep(final_text="westus3 is healthy."),
        ]
    )
    runner = DurableLoopRunner(
        state=state,
        content=content,
        model=first_model,
        tools=registry.build_dispatcher(),
        clock=lambda: datetime(2026, 9, 4, tzinfo=UTC),
        nonce_factory=lambda: "sample-nonce",
    )
    identity = _identity(
        run_id="sample-run-1",
        request_id="sample-request-1",
        request_body={"prompt": "Inspect a production region."},
        plan=plan,
    )
    waiting = await runner.start(
        identity,
        plan,
        [
            {
                "role": "user",
                "contents": [{"type": "text", "text": "Inspect a production region."}],
            }
        ],
    )
    assert waiting.human_input is not None
    await runner.submit_human_input(
        run_id=identity.run_id,
        request_id=waiting.human_input.request_id,
        submission_id="sample-submission",
        actor_hash=identity.owner_hash,
        answer="westus3",
    )
    first_result = await runner.resume(identity.run_id)

    second_model = ScriptedOneStepModelProvider(
        [ScriptedModelStep(final_text="The prior turn confirmed westus3 is healthy.")]
    )
    second_runner = DurableLoopRunner(
        state=state,
        content=content,
        model=second_model,
        tools=registry.build_dispatcher(),
        clock=lambda: datetime(2026, 9, 4, tzinfo=UTC),
    )
    second_identity = _identity(
        run_id="sample-run-2",
        request_id="sample-request-2",
        request_body={"prompt": "What did the previous turn establish?"},
        plan=plan,
    )
    second_result = await second_runner.start(
        second_identity,
        plan,
        [
            {
                "role": "user",
                "contents": [
                    {"type": "text", "text": "What did the previous turn establish?"}
                ],
            }
        ],
    )

    print(
        json.dumps(
            {
                "first_status": first_result.status.status,
                "human_question": waiting.human_input.question,
                "second_status": second_result.status.status,
                "session_id": identity.session_id,
                "turns": 2,
            },
            sort_keys=True,
        )
    )


def _identity(
    *,
    run_id: str,
    request_id: str,
    request_body: object,
    plan: DurableLoopPlan,
):
    return create_run_identity(
        run_id=run_id,
        session_id="sample-session",
        request_id=request_id,
        request_body=request_body,
        owner_hash="c" * 64,
        agent_slug="main",
        agent_hash="d" * 64,
        catalog_hash=plan.catalog.catalog_hash,
        deployment_hash="e" * 64,
        tool_package_hash=plan.catalog.package_hash,
        policy_hash=plan.catalog.policy_hash,
        settings=plan.settings,
        now=datetime(2026, 9, 4, tzinfo=UTC),
    )


if __name__ == "__main__":
    asyncio.run(main())
