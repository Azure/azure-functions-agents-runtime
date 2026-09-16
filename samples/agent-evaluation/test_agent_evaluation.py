"""Example JSONL-driven evaluation suite for an existing Function agent."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from agent_framework import (
    ExpectedToolCall,
    LocalEvaluator,
    evaluate_agent,
    tool_call_args_match,
    tool_calls_present,
)
from azure.identity import DefaultAzureCredential

from azure_functions_agents.evaluation import (
    EntraTokenAuth,
    FunctionAgentAuth,
    FunctionAgentTarget,
    FunctionKeyAuth,
)

os.environ["AGENT_EVAL_TARGET_URL"] = "http://localhost:7071/agents/receipt/chat"

@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    query: str
    expected_output: str | None
    expected_tool_calls: list[ExpectedToolCall]
    tags: list[str]
    repetitions: int


def load_cases(path: Path) -> list[EvaluationCase]:
    cases: list[EvaluationCase] = []
    with path.open(encoding="utf-8") as case_file:
        for line_number, line in enumerate(case_file, start=1):
            if not line.strip():
                continue
            value: Any = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"Case on line {line_number} must be a JSON object")
            expected_calls = [
                ExpectedToolCall(item["name"], item.get("arguments"))
                for item in value.get("expected_tool_calls", [])
            ]
            cases.append(
                EvaluationCase(
                    case_id=value["id"],
                    query=value["query"],
                    expected_output=value.get("expected_output"),
                    expected_tool_calls=expected_calls,
                    tags=value.get("tags", []),
                    repetitions=value.get("repetitions", 1),
                )
            )
    return cases


def target_auth_from_environment() -> FunctionAgentAuth | None:
    function_key = os.getenv("AGENT_EVAL_FUNCTION_KEY")
    if function_key:
        return FunctionKeyAuth(function_key)

    entra_scope = os.getenv("AGENT_EVAL_ENTRA_SCOPE")
    if entra_scope:
        return EntraTokenAuth(
            credential=DefaultAzureCredential(),
            scope=entra_scope,
        )
    return None


CASES = load_cases(Path(__file__).with_name("cases.jsonl"))
TARGET_URL = os.getenv("AGENT_EVAL_TARGET_URL")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.case_id)
def test_agent_behavior(case: EvaluationCase) -> None:
    if not TARGET_URL:
        pytest.skip("Set AGENT_EVAL_TARGET_URL to run the evaluation sample")

    target = FunctionAgentTarget(
        TARGET_URL,
        agent_id="receipt",
        name="Receipt agent",
        auth=target_auth_from_environment(),
    )
    evaluators: list[Any] = [LocalEvaluator(tool_calls_present, tool_call_args_match)]

    if os.getenv("AGENT_EVAL_USE_FOUNDRY") == "true":
        from agent_framework_foundry import FoundryEvals

        evaluators.append(
            FoundryEvals(
                model=os.environ["FOUNDRY_MODEL"],
                evaluators=[FoundryEvals.RELEVANCE, FoundryEvals.TASK_ADHERENCE],
            )
        )

    results = asyncio.run(
        evaluate_agent(
            agent=target,
            queries=[case.query],
            expected_output=[case.expected_output] if case.expected_output else None,
            expected_tool_calls=[case.expected_tool_calls],
            evaluators=evaluators,
            eval_name=f"receipt-agent-{case.case_id}",
            num_repetitions=case.repetitions,
        )
    )

    for result in results:
        if result.report_url:
            print(f"Foundry report: {result.report_url}")
        result.raise_for_status()
