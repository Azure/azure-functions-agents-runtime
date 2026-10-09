"""Tests for the harness-agent execution path in runner.py."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest
from agent_framework import (
    AgentResponse,
    AgentSession,
    BaseChatClient,
    ChatMiddlewareLayer,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    FunctionInvocationLayer,
    HistoryProvider,
    Message,
    ResponseStream,
    SessionContext,
)

from azure_functions_agents import runner
from azure_functions_agents._function_tool import tool
from azure_functions_agents.config import paths
from azure_functions_agents.config.schema import AgentConfiguration
from azure_functions_agents.discovery.mcp import MCPServerDescriptor
from azure_functions_agents.harness._harness_binding import AppHarness, HarnessKind
from azure_functions_agents.harness._provider_config import InferenceTarget
from azure_functions_agents.harness.agent_framework import _maf_execution as maf


def test_agent_result_preserves_existing_positional_argument_order() -> None:
    intermediate = ["thinking"]
    tool_calls = [{"name": "lookup"}]
    events = [{"type": "completed"}]
    result = runner.AgentResult(
        "session", "answer", intermediate, tool_calls, "reasoning", events, 2
    )
    assert result.content_intermediate is intermediate
    assert result.tool_calls is tool_calls
    assert result.reasoning == "reasoning"
    assert result.events is events
    assert result.delegate_error_count == 2
    assert result.model == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [HarnessKind.MAF, HarnessKind.COPILOT])
async def test_both_bound_runners_ignore_unsupported_values_before_execution(
    kind, monkeypatch, caplog,
):
    from agent_framework import FunctionTool

    from azure_functions_agents.harness.copilot_sdk import _copilot_execution

    calls = []

    def undecorated() -> str:
        calls.append("unsupported")
        return "ignored"

    @tool
    def authored() -> str:
        return "allowed"

    raw = FunctionTool(name="private-raw-name", func=undecorated, max_invocations=1)
    harness = AppHarness(kind, Path.cwd(), default_model="fixture-model")
    invoke = AsyncMock(return_value=runner.AgentResult("session", "captured"))
    backend = maf if kind is HarnessKind.MAF else _copilot_execution
    monkeypatch.setattr(backend, "run", invoke)
    result = await runner.run_agent(
        "fixture", tools=[raw, undecorated, authored], mcp_tools=[],
        sandbox_tools=[raw, undecorated], web_request_tools=[raw, undecorated],
        _harness=harness,
    )
    assert result.content == "captured"
    request = invoke.call_args.args[1]
    assert request.tools == (authored,)
    assert calls == []
    assert "Ignoring unsupported custom tool" in caplog.text
    assert "private-raw-name" not in caplog.text


@pytest.mark.asyncio
async def test_copilot_backend_rejects_authored_approval_before_effects(monkeypatch):
    from azure_functions_agents.harness._harness_binding import UnsupportedCapabilityError
    from azure_functions_agents.harness.copilot_sdk import _copilot_execution

    @tool(approval_mode="always_require")
    def bounded() -> str:
        raise AssertionError("Unsupported approval policy must not execute")

    invoke = AsyncMock()
    monkeypatch.setattr(_copilot_execution, "run", invoke)
    harness = AppHarness(HarnessKind.COPILOT, Path.cwd(), default_model="fixture-model")
    with pytest.raises(UnsupportedCapabilityError, match="approval"):
        await runner.run_agent(
            "fixture", tools=[bounded], mcp_tools=[], _harness=harness,
        )
    invoke.assert_not_called()


def test_run_agent_reports_model_and_tool_evidence_by_assistant_message(monkeypatch: Any) -> None:
    messages = [
        Message("assistant", [
            Content("function_call", call_id="call-1", name="lookup", arguments={"id": 1}),
            Content("function_call", call_id="call-2", name="lookup", arguments={"id": 2}),
        ]),
        Message("tool", [
            Content("function_result", call_id="call-1", result="ok"),
            Content("function_result", call_id="call-2", result='{"error": "failed"}'),
        ]),
        Message("assistant", [
            Content("function_call", call_id="call-3", name="finish", arguments=None),
        ]),
        Message("tool", [Content("function_result", call_id="call-3", result=None)]),
    ]

    class FakeAgent:
        async def run(self, *args: Any, **kwargs: Any) -> Any:
            return AgentResponse(messages=messages)

    async def fake_session_builder(
        **kwargs: Any,
    ) -> tuple[FakeAgent, object, str, None, InferenceTarget]:
        return FakeAgent(), object(), "session", None, InferenceTarget(model="gpt-test")

    monkeypatch.setattr(maf, "_build_agent_session", fake_session_builder)
    result = asyncio.run(runner.run_agent("hello"))
    assert result.model == "gpt-test"
    assert result.tool_calls == [
        {
            "type": "tool_start", "tool_call_id": "call-1", "tool_name": "lookup",
            "arguments": {"id": 1}, "turn_id": "response-0", "result": "ok", "success": True,
        },
        {
            "type": "tool_start", "tool_call_id": "call-2", "tool_name": "lookup",
            "arguments": {"id": 2}, "turn_id": "response-0",
            "result": '{"error": "failed"}', "success": False,
        },
        {
            "type": "tool_start", "tool_call_id": "call-3", "tool_name": "finish",
            "arguments": None, "turn_id": "response-1", "result": None, "success": True,
        },
    ]


def test_run_agent_does_not_guess_batch_or_correlate_missing_ids(monkeypatch: Any) -> None:
    messages = [
        Message("assistant", [Content("function_call", call_id="call-1", name="valid", arguments={})]),
        Message("unknown", [Content("function_call", call_id="call-2", name="unbatched", arguments={})]),
        Message("assistant", [Content("function_call", name="anonymous", arguments={})]),
        Message("tool", [
            Content("function_result", call_id="call-1"),
            Content("function_result", call_id="missing", result="ignored"),
            Content("function_result", result="must-not-attach"),
        ]),
    ]

    class FakeAgent:
        async def run(self, *args: Any, **kwargs: Any) -> Any:
            return AgentResponse(messages=messages)

    async def fake_session_builder(
        **kwargs: Any,
    ) -> tuple[FakeAgent, object, str, None, InferenceTarget]:
        return FakeAgent(), object(), "session", None, InferenceTarget()

    monkeypatch.setattr(maf, "_build_agent_session", fake_session_builder)
    result = asyncio.run(runner.run_agent("hello"))
    assert result.tool_calls == [
        {
            "type": "tool_start", "tool_call_id": "call-1", "tool_name": "valid",
            "arguments": {}, "turn_id": "response-0", "result": None, "success": True,
        },
        {
            "type": "tool_start", "tool_call_id": "call-2", "tool_name": "unbatched",
            "arguments": {},
        },
        {
            "type": "tool_start", "tool_call_id": None, "tool_name": "anonymous",
            "arguments": {}, "turn_id": "response-1",
        },
    ]


@pytest.fixture(autouse=True)
def _isolate_app_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("WEBSITE_OWNER_NAME", "WEBSITE_DEPLOYMENT_ID", "WEBSITE_SITE_NAME"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.asyncio
async def test_public_runner_preserves_the_deadline_set_before_selection(monkeypatch):
    loop = asyncio.get_running_loop()
    selected_at = []
    harness = AppHarness(HarnessKind.MAF, Path.cwd())

    def select():
        selected_at.append(loop.time())
        return harness

    invoke = AsyncMock(return_value=runner.AgentResult("session", "fixture reply"))

    class _SelectedRunner:
        async def run_agent(self, prompt: str, **kwargs: Any) -> runner.AgentResult:
            return await invoke(prompt, **kwargs)

    monkeypatch.setattr(runner, "get_harness", select)
    monkeypatch.setattr(runner, "get_agent_runner", lambda selected: _SelectedRunner())

    result = await runner.run_agent("fixture", timeout=0.1)

    assert result.content == "fixture reply"
    assert invoke.call_args.kwargs["deadline"] <= selected_at[0] + 0.1


@pytest.mark.asyncio
async def test_selection_cannot_restart_an_expired_public_invocation_budget(monkeypatch):
    harness = AppHarness(HarnessKind.MAF, Path.cwd())
    invoke = AsyncMock(return_value=runner.AgentResult("session", "must not run"))

    class _SelectedRunner:
        async def run_agent(self, prompt: str, **kwargs: Any) -> runner.AgentResult:
            if kwargs["deadline"] <= asyncio.get_running_loop().time():
                raise RuntimeError("Agent run timed out after 0.001s")
            return await invoke(prompt, **kwargs)

    def select():
        time.sleep(0.01)
        return harness

    monkeypatch.setattr(runner, "get_harness", select)
    monkeypatch.setattr(runner, "get_agent_runner", lambda selected: _SelectedRunner())

    with pytest.raises(RuntimeError, match=r"Agent run timed out after|timed out"):
        await runner.run_agent("fixture", timeout=0.001)

    invoke.assert_not_called()


@pytest.mark.asyncio
async def test_bound_harness_run_agent_discovers_tools_and_mcp_from_bound_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root_a = tmp_path / "root-a"
    root_a.mkdir()
    (root_a / "tools").mkdir()
    (root_a / "tools" / "local.py").write_text(
        "def user_root_a() -> str:\n    return 'root-a'\n", encoding="utf-8"
    )
    (root_a / "mcp.json").write_text(
        json.dumps({"servers": {"mcp_root_a": {"url": "https://fixture.invalid/mcp"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "_app_root", tmp_path / "unrelated-root")
    harness = AppHarness(HarnessKind.MAF, root_a)
    seen: list[dict[str, Any]] = []

    async def fake_builder(**kwargs: Any) -> tuple[_FakeAgent, object, str, None, InferenceTarget]:
        seen.append(kwargs)
        return _FakeAgent(), object(), "session", None, InferenceTarget()

    monkeypatch.setattr(maf, "_build_agent_session", fake_builder)
    await runner.run_agent("prompt", _harness=harness, tools=None, mcp_tools=None)
    await runner.run_agent("prompt", _harness=harness, tools=[], mcp_tools=[])

    assert seen[0]["app_root"] == root_a
    assert [item.name for item in seen[0]["tools"]] == ["user_root_a"]
    assert [item.name for item in seen[0]["mcp_tools"]] == ["mcp_root_a"]
    assert seen[0]["_harness"] is harness
    resolved_tools, _ = maf.assemble_agent_inputs(
        instructions=None,
        tools=seen[0]["tools"],
        mcp_tools=seen[0]["mcp_tools"],
        app_root=seen[0]["app_root"],
        sandbox_tools=None,
        web_request_tools=None,
        system_addendum=None,
        workflow_enabled=False,
        workflow_durable_client=None,
        workflow_agent_slug=None,
        agent_name=None,
        resolved_id=None,
        delegate_tools=None,
        workflow_policy=None,
    )
    assert {tool.name for tool in resolved_tools} == {"user_root_a", "mcp_root_a"}

    assert seen[1]["tools"] == ()
    assert seen[1]["mcp_tools"] == ()
    assert seen[1]["_harness"] is harness
    assert seen[1]["app_root"] == root_a
    resolved_disabled, _ = maf.assemble_agent_inputs(
        instructions=None,
        tools=seen[1]["tools"],
        mcp_tools=seen[1]["mcp_tools"],
        app_root=seen[1]["app_root"],
        sandbox_tools=None,
        web_request_tools=None,
        system_addendum=None,
        workflow_enabled=False,
        workflow_durable_client=None,
        workflow_agent_slug=None,
        agent_name=None,
        resolved_id=None,
        delegate_tools=None,
        workflow_policy=None,
    )
    assert resolved_disabled == []


# ---------------------------------------------------------------------------
# Minimal fake Agent
# ---------------------------------------------------------------------------


class _FakeAgent:
    def __init__(self, response_text: str = "hello") -> None:
        self._response_text = response_text

    async def run(self, _prompt: str, *, session: Any, options: Any = None) -> Any:
        return AgentResponse(messages=[Message("assistant", [self._response_text])])


class _RecordingStoringChatClient(ChatMiddlewareLayer, BaseChatClient):
    STORES_BY_DEFAULT: ClassVar[bool] = True

    def __init__(self, response_text: str = "response") -> None:
        super().__init__()
        self.calls: list[list[str]] = []
        self.response_text = response_text

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Any:
        assert not stream
        self.calls.append([message.text for message in messages])

        async def get_response() -> ChatResponse[Any]:
            return ChatResponse(messages=[Message("assistant", [self.response_text])])

        return get_response()


class _SharedHistoryProvider(HistoryProvider):
    def __init__(self, messages: list[Message]) -> None:
        super().__init__(source_id="shared_history")
        self.messages = messages

    async def get_messages(
        self,
        session_id: str | None,
        *,
        state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[Message]:
        return list(self.messages)

    async def save_messages(
        self,
        session_id: str | None,
        messages: Sequence[Message],
        *,
        state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.messages.extend(messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("selection", ["collection", "invalid-name", "empty", "unspecified"])
async def test_public_maf_skill_paths_expand_to_selected_roots_before_sdk_loading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: Any, stream: bool, selection: str
) -> None:
    import agent_framework

    class SkillMetadataChatClient(FunctionInvocationLayer[Any], BaseChatClient[Any]):
        def __init__(self) -> None:
            super().__init__()
            self.messages: list[str] = []

        def _inner_get_response(
            self,
            *,
            messages: Sequence[Message],
            stream: bool,
            options: Mapping[str, Any],
            **kwargs: Any,
        ) -> Any:
            self.messages.extend(message.text for message in messages)
            self.messages.append(str(options.get("instructions") or ""))
            if stream:
                async def updates() -> Any:
                    yield ChatResponseUpdate(
                        contents=[Content.from_text("complete")], role="assistant"
                    )

                return ResponseStream(updates(), finalizer=ChatResponse.from_updates)

            async def response() -> ChatResponse[Any]:
                return ChatResponse(messages=[Message("assistant", ["complete"])])

            return response()

    collection = tmp_path / "collection"
    extra = tmp_path / "extra-collection"
    invalid = tmp_path / "invalid-skill"
    for directory, name in (
        (collection / "alpha", "alpha"),
        (collection / "beta", "beta"),
        (extra / "gamma", "gamma"),
        (invalid, "Bad_Name"),
    ):
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Description for {name}\n---\nInstructions.\n",
            encoding="utf-8",
        )
    paths = (
        [extra, collection] if selection == "collection"
        else [invalid, collection] if selection == "invalid-name"
        else [] if selection == "empty" else None
    )
    passed_paths: list[list[Path]] = []
    expected = [item.path for item in runner.describe_skill_catalog(paths or [])] if paths is not None else []
    original_from_paths = agent_framework.SkillsProvider.from_paths

    def from_paths(received: Sequence[Path], **kwargs: Any) -> Any:
        passed_paths.append(list(received))
        return original_from_paths(received, **kwargs)

    monkeypatch.setattr(agent_framework.SkillsProvider, "from_paths", from_paths)
    chat_client = SkillMetadataChatClient()
    monkeypatch.setattr(
        maf,
        "build_chat_client",
        lambda _model: (chat_client, InferenceTarget()),
    )
    monkeypatch.setattr(maf, "_build_history_provider", lambda _slug: None)
    harness = AppHarness(HarnessKind.MAF, tmp_path)
    kwargs = dict(tools=[], mcp_tools=[], skill_paths=paths, _harness=harness)
    if stream:
        events = [
            json.loads(chunk.removeprefix("data: ").strip())
            async for chunk in runner.run_agent_stream("prompt", **kwargs)
        ]
        assert any(event["type"] == "done" for event in events)
        assert not any(event["type"] == "error" for event in events)
    else:
        response = await runner.run_agent("prompt", **kwargs)
        assert response.content == "complete"
    advertised = "\n".join(chat_client.messages)
    if selection in {"collection", "invalid-name"}:
        assert passed_paths == [expected]
        assert "alpha" in advertised and "beta" in advertised
        if selection == "collection":
            assert "gamma" in advertised
        else:
            assert "Bad_Name" not in advertised
            assert any(
                record.name == "agent_framework._skills"
                and "invalid name" in record.getMessage()
                and "Bad_Name" in record.getMessage()
                for record in caplog.records
            )
    else:
        assert passed_paths == []
        assert "alpha" not in advertised and "beta" not in advertised


def test_build_agent_session_forces_provider_managed_history(
    monkeypatch: Any,
) -> None:
    """Fresh request-scoped sessions must reload history from the configured provider."""
    captured: list[dict[str, Any]] = []

    def fake_create_harness_agent(_client: Any, **kwargs: Any) -> _FakeAgent:
        captured.append(kwargs)
        return _FakeAgent()

    import agent_framework

    monkeypatch.setattr(
        agent_framework,
        "create_harness_agent",
        fake_create_harness_agent,
        raising=False,
    )
    monkeypatch.setattr(
        maf,
        "build_chat_client",
        lambda _model: (object(), InferenceTarget()),
    )
    history_calls: list[str] = []
    monkeypatch.setattr(
        maf,
        "_build_history_provider",
        lambda agent_slug: history_calls.append(agent_slug) or object(),
    )

    asyncio.run(
        maf._build_agent_session(
            instructions="do stuff",
            session_id="shared-session",
            tools=[],
            mcp_tools=[],
            skill_paths=None,
            model=None,
            sandbox_tools=None,
            system_addendum=None,
            workflow_enabled=False,
            workflow_durable_client=None,
            agent_name=None,
            web_request_tools=None,
            agent_configuration=AgentConfiguration(),
        )
    )

    assert captured[0]["default_options"] == {"store": False}
    assert history_calls == ["main"]


@pytest.mark.parametrize("agent_name", [None, "", "main", "billing"])
@pytest.mark.parametrize(
    ("site_name", "expected_site_name"),
    [
        (None, None),
        ("", None),
        (" \t ", None),
        ("Contoso-Agents", "Contoso-Agents"),
        ("  Contoso-Agents \t ", "Contoso-Agents"),
    ],
)
def test_build_role_agent_uses_stable_agent_id(
    monkeypatch: pytest.MonkeyPatch,
    agent_name: str | None,
    site_name: str | None,
    expected_site_name: str | None,
) -> None:
    """Fresh harnesses keep stable IDs independent of their readable app-qualified names."""
    captured: list[dict[str, Any]] = []

    def fake_create_harness_agent(_client: Any, **kwargs: Any) -> _FakeAgent:
        captured.append(kwargs)
        return _FakeAgent()

    import agent_framework

    monkeypatch.setenv("WEBSITE_OWNER_NAME", "sub+rg-eastuswebspace")
    monkeypatch.setenv("WEBSITE_DEPLOYMENT_ID", "deployment-123")
    if site_name is not None:
        monkeypatch.setenv("WEBSITE_SITE_NAME", site_name)
    monkeypatch.setattr(
        agent_framework,
        "create_harness_agent",
        fake_create_harness_agent,
        raising=False,
    )

    for _ in range(2):
        maf._build_role_agent(
            object(),
            agent_instructions=None,
            tools=[],
            skill_paths=None,
            agent_name=agent_name,
            history_provider=None,
            agent_configuration=AgentConfiguration(),
        )

    slug = agent_name or "main"
    expected_name = f"{expected_site_name}/{slug}" if expected_site_name else agent_name
    expected_id = f"{expected_site_name.lower() if expected_site_name else 'local'}/{slug}"
    assert [options["id"] for options in captured] == [expected_id] * 2
    assert [options["name"] for options in captured] == [expected_name] * 2


def test_build_agent_session_forwards_system_instructions(monkeypatch: Any) -> None:
    """Markdown and runtime instructions are forwarded without MAF harness guidance."""
    captured: list[dict[str, Any]] = []

    def fake_create_harness_agent(_client: Any, **kwargs: Any) -> _FakeAgent:
        captured.append(kwargs)
        return _FakeAgent()

    import agent_framework

    monkeypatch.setattr(
        agent_framework,
        "create_harness_agent",
        fake_create_harness_agent,
        raising=False,
    )
    monkeypatch.setattr(
        maf,
        "build_chat_client",
        lambda _model: (object(), InferenceTarget()),
    )
    monkeypatch.setattr(maf, "_build_history_provider", lambda agent_slug: object())

    asyncio.run(
        maf._build_agent_session(
            instructions="Markdown system prompt.",
            session_id="instruction-session",
            tools=[],
            mcp_tools=[],
            skill_paths=None,
            model=None,
            sandbox_tools=None,
            system_addendum=" Runtime system addendum.",
            workflow_enabled=False,
            workflow_durable_client=None,
            agent_name=None,
            web_request_tools=None,
            agent_configuration=AgentConfiguration(),
        )
    )

    assert captured[0]["agent_instructions"] == (
        "Markdown system prompt. Runtime system addendum."
    )
    assert captured[0]["harness_instructions"] == ""
    assert captured[0]["tools"] == []
    assert captured[0]["disable_todo"] is True
    assert captured[0]["disable_mode"] is True
    assert captured[0]["disable_file_memory"] is True
    assert captured[0]["disable_web_search"] is True
    assert captured[0]["disable_tool_auto_approval"] is True
    assert captured[0]["default_options"] == {"store": False}


def test_build_role_agent_skips_approval_for_all_skill_tools(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Role agents can use every configured skill tool without an approval surface."""
    captured: dict[str, Any] = {}
    skill_dir = tmp_path / "test-skill"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "scripts").mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: Test skill\n---\n\n# Test skill\n",
        encoding="utf-8",
    )
    (skill_dir / "references" / "guide.md").write_text("Read me.", encoding="utf-8")
    (skill_dir / "scripts" / "run.py").write_text("print('unsafe')", encoding="utf-8")

    def fake_create_harness_agent(_client: Any, **kwargs: Any) -> _FakeAgent:
        captured.update(kwargs)
        return _FakeAgent()

    import agent_framework

    monkeypatch.setattr(
        agent_framework,
        "create_harness_agent",
        fake_create_harness_agent,
        raising=False,
    )

    agent = maf._build_role_agent(
        object(),
        agent_instructions=None,
        tools=[],
        skill_paths=[skill_dir],
        agent_name=None,
        history_provider=None,
        agent_configuration=AgentConfiguration(),
    )
    context = SessionContext(input_messages=[])
    asyncio.run(
        captured["skills_provider"].before_run(
            agent=agent,
            session=AgentSession(),
            context=context,
            state={},
        )
    )

    approval_modes = {tool.name: tool.approval_mode for tool in context.tools}
    assert approval_modes == {
        "load_skill": "never_require",
        "read_skill_resource": "never_require",
        "run_skill_script": "never_require",
    }
    assert "skills_paths" not in captured


def test_build_agent_session_appends_subagent_tools(monkeypatch: Any) -> None:
    """Harness agents receive all shared tools and return their delegation error tracker."""
    captured_agent_options: list[dict[str, Any]] = []
    captured_delegate_options: list[tuple[Any, Any, float]] = []
    local_tool = tool(lambda: "ok", name="local_tool")
    mcp_tool = MCPServerDescriptor(
        name="mcp_tool", url="https://fixture.invalid/mcp", transport="streamable-http",
        headers=(), tools=None, auth_scope=None, client_id=None,
    )
    sandbox_tool = tool(lambda: "ok", name="sandbox_tool")
    web_request_tool = tool(lambda: "ok", name="web_request_tool")
    delegate_tool = tool(lambda: "ok", name="delegate_billing")
    delegate_tracker = runner._DelegateErrorTracker()
    subagents = [SimpleNamespace(agent="billing")]
    catalog = object()
    history_calls: list[str] = []

    def fake_create_harness_agent(_client: Any, **kwargs: Any) -> _FakeAgent:
        captured_agent_options.append(kwargs)
        return _FakeAgent()

    async def fake_build_subagent_tools(
        received_subagents: Any,
        received_catalog: Any,
        *,
        coordinator_deadline: float,
        harness: Any = None,
    ) -> tuple[list[Any], runner._DelegateErrorTracker]:
        captured_delegate_options.append(
            (received_subagents, received_catalog, coordinator_deadline)
        )
        return [delegate_tool], delegate_tracker

    import agent_framework

    monkeypatch.setattr(
        agent_framework,
        "create_harness_agent",
        fake_create_harness_agent,
        raising=False,
    )
    monkeypatch.setattr(
        maf,
        "build_chat_client",
        lambda _model: (object(), InferenceTarget()),
    )
    monkeypatch.setattr(
        maf,
        "_build_history_provider",
        lambda agent_slug: history_calls.append(agent_slug) or object(),
    )
    monkeypatch.setattr(runner, "build_subagent_tools", fake_build_subagent_tools)

    _, _, _, returned_tracker, _ = asyncio.run(
        maf._build_agent_session(
            instructions="coordinate specialists",
            session_id="shared-session",
            tools=[local_tool],
            mcp_tools=[mcp_tool],
            skill_paths=None,
            model=None,
            sandbox_tools=[sandbox_tool],
            system_addendum=None,
            workflow_enabled=False,
            workflow_durable_client=None,
            agent_name="coordinator",
            web_request_tools=[web_request_tool],
            agent_configuration=AgentConfiguration(),
            subagents=subagents,
            catalog=catalog,
            coordinator_deadline=123.0,
        )
    )

    assert captured_delegate_options == [(subagents, catalog, 123.0)]
    assert [tool.name for tool in captured_agent_options[0]["tools"]] == [
        "local_tool",
        "sandbox_tool",
        "web_request_tool",
        "mcp_tool",
        "delegate_billing",
    ]
    assert returned_tracker is delegate_tracker
    assert history_calls == ["coordinator"]


def test_build_history_provider_scopes_local_storage_by_agent(
    monkeypatch: Any, tmp_path: Path
) -> None:
    captured_blob_slugs: list[str] = []
    monkeypatch.setattr(maf, "resolve_config_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "azure_functions_agents.harness.agent_framework._maf_blob_history.build_blob_provider_from_environment",
        lambda *, agent_slug: captured_blob_slugs.append(agent_slug) or None,
    )

    provider = maf._build_history_provider("billing")

    assert captured_blob_slugs == ["billing"]
    assert provider.storage_path == tmp_path / "agent-sessions" / "billing"


@pytest.mark.parametrize("site_name", [None, "Contoso-Agents"])
@pytest.mark.parametrize("agent_name", [None, "billing"])
def test_fresh_harness_agents_reload_history_for_same_session(
    monkeypatch: pytest.MonkeyPatch,
    site_name: str | None,
    agent_name: str | None,
) -> None:
    """Turn two receives turn-one history even though both agents and sessions are fresh."""
    client = _RecordingStoringChatClient()
    stored_messages: list[Message] = []
    history_calls: list[str] = []
    slug = agent_name or "main"
    expected_name = f"{site_name}/{slug}" if site_name else agent_name

    if site_name is not None:
        monkeypatch.setenv("WEBSITE_SITE_NAME", site_name)

    def build_history_provider(agent_slug: str) -> _SharedHistoryProvider:
        history_calls.append(agent_slug)
        return _SharedHistoryProvider(stored_messages)

    monkeypatch.setattr(
        maf,
        "build_chat_client",
        lambda _model: (client, InferenceTarget()),
    )
    monkeypatch.setattr(
        maf,
        "_build_history_provider",
        build_history_provider,
    )

    async def run_two_turns() -> None:
        common = {
            "instructions": "",
            "session_id": "shared-session",
            "tools": [],
            "mcp_tools": [],
            "skill_paths": None,
            "model": None,
            "sandbox_tools": None,
            "system_addendum": None,
            "workflow_enabled": False,
            "workflow_durable_client": None,
            "agent_name": agent_name,
            "web_request_tools": None,
            "agent_configuration": AgentConfiguration(),
        }
        first_agent, first_session, _, _, _ = await maf._build_agent_session(**common)
        assert first_agent.name == expected_name
        assert first_session.session_id == "shared-session"
        await first_agent.run("Use the Premium plan.", session=first_session)
        assert [message.text for message in stored_messages] == [
            "Use the Premium plan.",
            "response",
        ]

        second_agent, second_session, _, _, _ = await maf._build_agent_session(**common)
        assert second_agent.name == expected_name
        assert second_agent.id == first_agent.id
        assert second_session.session_id == "shared-session"
        await second_agent.run("Which plan did I choose?", session=second_session)

    asyncio.run(run_two_turns())

    assert client.calls == [
        ["Use the Premium plan."],
        ["Use the Premium plan.", "response", "Which plan did I choose?"],
    ]
    assert history_calls == [slug, slug]
    assert [
        Message.from_json(message.to_json()).author_name for message in stored_messages
    ] == [None, expected_name or "UnnamedAgent", None, expected_name or "UnnamedAgent"]


@pytest.mark.asyncio
async def test_qualified_agent_name_round_trips_history_without_entering_model_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Replay app-qualified author metadata through the pinned Responses client offline."""
    from agent_framework.openai import OpenAIChatClient
    from httpx import AsyncClient, MockTransport, Request, Response
    from openai import AsyncOpenAI

    requests: list[dict[str, Any]] = []

    def respond(request: Request) -> Response:
        assert request.url.path == "/responses"
        requests.append(json.loads(request.content))
        return Response(
            200,
            json={
                "id": f"resp_{len(requests)}",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": "offline-model",
                "metadata": {},
                "output": [
                    {
                        "id": f"msg_{len(requests)}",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": "response", "annotations": []}
                        ],
                    }
                ],
            },
        )

    monkeypatch.setenv("WEBSITE_SITE_NAME", "Contoso-Agents")
    monkeypatch.setattr(maf, "resolve_config_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "azure_functions_agents.harness.agent_framework._maf_blob_history.build_blob_provider_from_environment",
        lambda **_kwargs: None,
    )
    history_provider = maf._build_history_provider("billing")
    await history_provider.save_messages(
        "shared-session",
        [
            Message("user", ["Use the Premium plan."]),
            Message("assistant", ["response"], author_name="Contoso-Agents/billing"),
        ],
    )
    history = await history_provider.get_messages("shared-session")
    assert history[1].author_name == "Contoso-Agents/billing"

    async with AsyncOpenAI(
        api_key="offline-test-key",
        base_url="https://example.invalid",
        max_retries=0,
        http_client=AsyncClient(transport=MockTransport(respond)),
    ) as provider_client:
        chat_client = OpenAIChatClient(model="offline-model", async_client=provider_client)
        monkeypatch.setattr(
            maf,
            "build_chat_client",
            lambda _model: (chat_client, InferenceTarget()),
        )
        agent, session, resolved_id, _, _ = await maf._build_agent_session(
            instructions=None,
            session_id="shared-session",
            tools=[],
            mcp_tools=[],
            skill_paths=None,
            model=None,
            sandbox_tools=None,
            system_addendum=None,
            workflow_enabled=False,
            workflow_durable_client=None,
            agent_name="billing",
        )
        assert agent.name == "Contoso-Agents/billing"
        assert resolved_id == session.session_id == "shared-session"
        result = await agent.run("Which plan did I choose?", session=session)
        assert result.text == "response"

    history = await maf._build_history_provider("billing").get_messages("shared-session")
    assert [message.text for message in history] == [
        "Use the Premium plan.", "response", "Which plan did I choose?", "response"
    ]
    assert history[1].author_name == "Contoso-Agents/billing"
    assert (tmp_path / "agent-sessions" / "billing" / "shared-session.jsonl").is_file()
    assert not (tmp_path / "agent-sessions" / "Contoso-Agents").exists()
    assert len(requests) == 1
    assert [message["role"] for message in requests[0]["input"]] == [
        "user", "assistant", "user"
    ]
    assert [
        message["content"][0]["text"] for message in requests[0]["input"]
    ] == ["Use the Premium plan.", "response", "Which plan did I choose?"]
    assert all("name" not in message for request in requests for message in request["input"])
    assert "Contoso-Agents/billing" not in json.dumps(requests)


def test_native_compaction_preserves_small_history_without_rewriting_storage(
    monkeypatch: Any,
) -> None:
    """Native defaults leave a small conversation intact in model context and storage."""
    response_text = "prior response detail " * 80
    first_prompt = "first-turn context " * 80
    second_prompt = "current-turn question " * 80
    client = _RecordingStoringChatClient(response_text=response_text)
    stored_messages: list[Message] = []

    monkeypatch.setattr(
        maf,
        "build_chat_client",
        lambda _model: (client, InferenceTarget()),
    )
    monkeypatch.setattr(
        maf,
        "_build_history_provider",
        lambda agent_slug: _SharedHistoryProvider(stored_messages),
    )

    async def run_two_turns() -> None:
        common = {
            "instructions": "",
            "session_id": "compacted-session",
            "tools": [],
            "mcp_tools": [],
            "skill_paths": None,
            "model": None,
            "sandbox_tools": None,
            "system_addendum": None,
            "workflow_enabled": False,
            "workflow_durable_client": None,
            "agent_name": None,
            "web_request_tools": None,
            "agent_configuration": AgentConfiguration(max_output_tokens=100),
        }
        first_agent, first_session, _, _, _ = await maf._build_agent_session(**common)
        await first_agent.run(first_prompt, session=first_session)

        second_agent, second_session, _, _, _ = await maf._build_agent_session(**common)
        await second_agent.run(second_prompt, session=second_session)

    asyncio.run(run_two_turns())

    assert client.calls[0] == [first_prompt]
    assert client.calls[1], "expected a second model call"
    assert client.calls[1][-1] == second_prompt
    assert first_prompt in client.calls[1]
    assert [message.text for message in stored_messages] == [
        first_prompt,
        response_text,
        second_prompt,
        response_text,
    ], "expected provider storage to retain the full, uncompacted conversation"


# ---------------------------------------------------------------------------
# Tests: public runners always dispatch to the harness session builder
# ---------------------------------------------------------------------------


def test_run_agent_uses_session_builder_with_configuration(monkeypatch: Any) -> None:
    session_calls: list[dict[str, Any]] = []
    subagents = [SimpleNamespace(agent="billing")]
    catalog = object()

    async def fake_session_builder(
        **kwargs: Any,
    ) -> tuple[_FakeAgent, object, str, None, InferenceTarget]:
        session_calls.append(kwargs)
        return (
            _FakeAgent("harness response"),
            object(),
            "harness-session",
            None,
            InferenceTarget(),
        )

    monkeypatch.setattr(maf, "_build_agent_session", fake_session_builder)

    result = asyncio.run(
        runner.run_agent(
            "hello",
            agent_configuration=AgentConfiguration(),
            subagents=subagents,
            catalog=catalog,
        )
    )

    assert len(session_calls) == 1
    assert session_calls[0]["subagents"] is subagents
    assert session_calls[0]["catalog"] is catalog
    assert isinstance(session_calls[0]["coordinator_deadline"], float)
    assert result.content == "harness response"
    assert result.session_id == "harness-session"


def test_run_agent_uses_session_builder_with_default_configuration(monkeypatch: Any) -> None:
    session_calls: list[dict[str, Any]] = []

    async def fake_session_builder(
        **kwargs: Any,
    ) -> tuple[_FakeAgent, object, str, None, InferenceTarget]:
        session_calls.append(kwargs)
        return _FakeAgent("response"), object(), "session", None, InferenceTarget()

    monkeypatch.setattr(maf, "_build_agent_session", fake_session_builder)

    result = asyncio.run(runner.run_agent("hello"))

    assert len(session_calls) == 1
    assert session_calls[0]["agent_configuration"] is None
    assert result.session_id == "session"


def test_run_agent_stream_uses_session_builder_with_configuration(monkeypatch: Any) -> None:
    session_calls: list[dict[str, Any]] = []
    subagents = [SimpleNamespace(agent="billing")]
    catalog = object()

    async def fake_session_builder(
        **kwargs: Any,
    ) -> tuple[_FakeAgent, object, str, None, InferenceTarget]:
        session_calls.append(kwargs)

        class _StreamingAgent(_FakeAgent):
            async def run(self, _p: str, *, session: Any, options: Any = None) -> Any:  # type: ignore[override]
                return SimpleNamespace(text="streamed", messages=[])

        return _StreamingAgent(), object(), "stream-harness-session", None, InferenceTarget()

    monkeypatch.setattr(maf, "_build_agent_session", fake_session_builder)

    async def collect() -> list[str]:
        return [
            chunk
            async for chunk in runner.run_agent_stream(
                "hi",
                agent_configuration=AgentConfiguration(),
                subagents=subagents,
                catalog=catalog,
            )
        ]

    asyncio.run(collect())
    assert len(session_calls) == 1
    assert session_calls[0]["agent_configuration"] == AgentConfiguration()
    assert session_calls[0]["subagents"] is subagents
    assert session_calls[0]["catalog"] is catalog
    assert isinstance(session_calls[0]["coordinator_deadline"], float)


def test_run_agent_passes_agent_configuration_to_builder(monkeypatch: Any) -> None:
    captured: list[dict[str, Any]] = []
    config = AgentConfiguration(max_output_tokens=16_000)

    async def fake_harness_builder(
        **kwargs: Any,
    ) -> tuple[_FakeAgent, object, str, None, InferenceTarget]:
        captured.append(kwargs)
        return _FakeAgent(), object(), "s", None, InferenceTarget()

    monkeypatch.setattr(maf, "_build_agent_session", fake_harness_builder)

    asyncio.run(runner.run_agent("prompt", agent_configuration=config))

    assert captured[0]["agent_configuration"] is config
