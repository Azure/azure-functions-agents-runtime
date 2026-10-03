from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest
from copilot import CopilotClient, RuntimeConnection, ToolSet
from copilot.rpc import PermissionDecisionApproveOnce, PermissionDecisionDeniedByRules
from copilot.session import MCPHTTPServerConfig, PermissionHandler, PermissionNoResult
from copilot.session_events import (
    PermissionRequestCustomTool,
    PermissionRequestMcp,
    PermissionRequestMemory,
    PermissionRequestRead,
    PermissionRequestShell,
    PermissionRequestUrl,
    PermissionRequestWrite,
)

from azure_functions_agents import _copilot_capabilities
from azure_functions_agents._harness import CopilotPreviewError, HarnessRequest
from azure_functions_agents._skill_policy import SkillPolicy
from azure_functions_agents._tool_descriptor import ToolDescriptor
from azure_functions_agents.discovery.mcp import MCPServerDescriptor
from azure_functions_agents.discovery.skills import SkillDescriptor


def _request(**changes):
    request = HarnessRequest(
        prompt="hello",
        instructions="Author instructions.",
        agent_slug="main",
        session_id="example",
        new_session=True,
        model="fixture-model",
        tools=(),
        max_output_tokens=None,
        deadline=float("inf"),
    )
    return replace(request, **changes)


@pytest.fixture
def scoped_skills(tmp_path):
    approved = tmp_path / "approved"
    excluded = approved / "nested"
    (approved / "scripts").mkdir(parents=True)
    excluded.mkdir()
    (approved / "SKILL.md").write_text(
        "---\nname: approved\ndescription: Approved description.\n---\napproved instructions\n",
        encoding="utf-8",
    )
    (approved / "reference.txt").write_text("approved resource", encoding="utf-8")
    (approved / "scripts" / "run.py").write_text("print('ok')", encoding="utf-8")
    (excluded / "SKILL.md").write_text(
        "---\nname: excluded\ndescription: Excluded description.\n---\nexcluded instructions\n",
        encoding="utf-8",
    )
    (excluded / "reference.txt").write_text("excluded resource", encoding="utf-8")
    descriptors = tuple(
        SkillDescriptor.create(name=name, description=description, path=path)
        for name, description, path in (
            ("approved", "Approved description.", approved),
            ("excluded", "Excluded description.", excluded),
        )
    )
    policy = SkillPolicy.create(
        approved=descriptors[:1], discovered=descriptors, working_directory=tmp_path
    )
    return policy, descriptors


def _shell(command, *, possible_paths=(), **changes):
    return PermissionRequestShell(
        can_offer_session_approval=True,
        commands=[],
        full_command_text=command,
        has_write_file_redirection=False,
        intention="A model claim does not authorize the action.",
        possible_paths=list(possible_paths),
        possible_urls=[],
        **changes,
    )


def _mcp(**changes):
    return PermissionRequestMcp(
        read_only=False, server_name="selected", tool_name="lookup", tool_title="Lookup",
        **changes,
    )


def test_available_tools_uses_the_public_sdk_source_selector_syntax(scoped_skills):
    _policy, skills = scoped_skills
    tool = ToolDescriptor.create(name="host_tool", description="Host tool", func=lambda: "ok")
    server = MCPServerDescriptor.create(
        name="selected", url="https://remote.example/mcp", tools=["lookup"]
    )
    request = _request(tools=(tool,), mcp_servers=(server,), skills=skills[:1], skill_catalog=skills)

    expected = (
        ToolSet().add_custom("host_tool").add_mcp("*")
        .add_builtin(["skill", "view", "bash"]).to_list()
    )
    assert _copilot_capabilities.available_tools(request) == expected
    assert _copilot_capabilities.available_tools(replace(request, skills=())) == [
        "custom:host_tool", "mcp:*",
    ]
    assert _copilot_capabilities.available_tools(_request()) == []


def test_replace_prompt_projects_only_approved_names_and_descriptions(scoped_skills):
    _policy, skills = scoped_skills
    prompt = _copilot_capabilities.skill_instructions("Author instructions.", skills[:1])

    assert prompt.count("Author instructions.") == 1
    assert "- approved: Approved description." in prompt
    assert "excluded" not in prompt
    assert "approved instructions" not in prompt
    assert _copilot_capabilities.skill_instructions("Author instructions.", ()) == "Author instructions."
    assert _copilot_capabilities.skill_instructions(None, ()) == ""


@pytest.mark.asyncio
async def test_mcp_configuration_maps_all_existing_filters_and_remote_transports(monkeypatch):
    descriptors = tuple(
        MCPServerDescriptor.create(name=name, url=f"https://{name}.example/mcp", **options)
        for name, options in (
            ("all", {}),
            ("empty", {"tools": []}),
            ("selected", {"tools": ["lookup", "search"], "transport": "streamable-http"}),
            ("wildcard", {"tools": ["ignored", "*"]}),
        )
    )
    materialize = Mock(side_effect=lambda server: {"X-Server": server.name})
    protect = Mock()
    monkeypatch.setattr(_copilot_capabilities, "materialize_mcp_headers", materialize)

    configured = await _copilot_capabilities.mcp_configuration(
        descriptors, protect_headers=protect
    )

    assert set(configured) == {"all", "empty", "selected", "wildcard"}
    assert [configured[name]["tools"] for name in configured] == [
        ["*"], [], ["lookup", "search"], ["*"],
    ]
    assert all(server["type"] == "http" for server in configured.values())
    assert configured["selected"]["url"] == "https://selected.example/mcp"
    assert configured["selected"]["headers"] == {"X-Server": "selected"}
    assert materialize.call_count == 4
    assert protect.call_count == 4


@pytest.mark.asyncio
async def test_header_materialization_is_bounded_by_the_existing_async_deadline(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def waiting_materialization(_callback, *_args):
        started.set()
        await release.wait()

    monkeypatch.setattr(_copilot_capabilities.asyncio, "to_thread", waiting_materialization)
    server = MCPServerDescriptor.create(name="selected", url="https://remote.example/mcp")
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await _copilot_capabilities.mcp_configuration((server,), protect_headers=Mock())
    assert started.is_set()


@pytest.mark.asyncio
async def test_authentication_failure_is_explicit_sanitized_and_not_retried(monkeypatch):
    acquire = Mock(side_effect=RuntimeError("private credential-sentinel"))
    monkeypatch.setattr(_copilot_capabilities, "materialize_mcp_headers", acquire)
    server = MCPServerDescriptor.create(name="selected", url="https://remote.example/mcp")

    with pytest.raises(CopilotPreviewError, match="MCP authentication") as caught:
        await _copilot_capabilities.mcp_configuration((server,), protect_headers=Mock())

    assert "credential-sentinel" not in str(caught.value)
    acquire.assert_called_once_with(server)


def test_only_mcp_delegates_to_the_sdk_approve_once_helper(scoped_skills, monkeypatch):
    policy, _skills = scoped_skills
    decide = _copilot_capabilities.permission_handler(policy)
    approve = Mock(wraps=PermissionHandler.approve_all)
    monkeypatch.setattr(PermissionHandler, "approve_all", approve)
    request, invocation = _mcp(tool_call_id="mcp"), {"session_id": "main.example"}

    result = decide(request, invocation)

    approve.assert_called_once_with(request, invocation)
    assert result.kind == PermissionDecisionApproveOnce.kind
    approve.reset_mock()
    denied = decide(_shell("pwd", possible_paths=[str(policy.approved[0].path)]), invocation)
    assert denied.kind == PermissionDecisionDeniedByRules.kind
    approve.assert_not_called()


def test_mcp_retains_the_sdk_managed_approval_behavior(scoped_skills):
    policy, _skills = scoped_skills
    decide = _copilot_capabilities.permission_handler(policy)

    with pytest.raises(RuntimeError, match="managed settings"):
        decide(_mcp(), {"session_id": "main.example", "managed_settings_enabled": True})
    result = decide(_mcp(managed_approval_required=True), {"session_id": "main.example"})
    assert result.kind == PermissionNoResult().kind


def test_skill_resource_and_literal_script_actions_get_only_approve_once(scoped_skills):
    policy, skills = scoped_skills
    decide = _copilot_capabilities.permission_handler(policy)
    resource = skills[0].path / "reference.txt"
    script = skills[0].path / "scripts" / "run.py"
    invocation = {"session_id": "main.example"}

    read = decide(PermissionRequestRead(intention="resource", path=str(resource)), invocation)
    shell = decide(_shell(f"python '{script}' --format json"), invocation)

    assert read.kind == PermissionDecisionApproveOnce.kind
    assert shell.kind == PermissionDecisionApproveOnce.kind


def test_hints_intent_and_skill_identity_do_not_grant_general_shell(scoped_skills):
    policy, skills = scoped_skills
    decide = _copilot_capabilities.permission_handler(policy)
    script = skills[0].path / "scripts" / "run.py"
    for command in ("pwd", "echo permitted", f"python '{script}'; pwd", "python -c 'print(1)'"):
        request = _shell(
            command,
            possible_paths=[str(script)],
            tool_call_id="previous-approved-skill",
            resolved_working_directory=str(skills[0].path),
        )
        result = decide(request, {"session_id": "main.example"})
        assert result.kind == PermissionDecisionDeniedByRules.kind


def test_excluded_child_resource_and_unrelated_request_kinds_are_denied(scoped_skills):
    policy, skills = scoped_skills
    decide = _copilot_capabilities.permission_handler(policy)
    excluded_resource = skills[1].path / "reference.txt"
    requests = [
        PermissionRequestRead(intention="approved parent", path=str(excluded_resource)),
        PermissionRequestWrite(
            can_offer_session_approval=True,
            diff="write",
            file_name=str(skills[0].path / "reference.txt"),
            intention="write approved resource",
        ),
        PermissionRequestUrl(intention="approved skill", url="https://remote.example/"),
        PermissionRequestMemory(fact="approved skill is loaded"),
        PermissionRequestCustomTool(
            tool_description="claims to be a skill", tool_name="bash", skip_permission=True
        ),
    ]
    for request in requests:
        result = decide(request, {"session_id": "main.example"})
        assert result.kind == PermissionDecisionDeniedByRules.kind


def test_denial_diagnostics_include_only_permission_kind(scoped_skills, monkeypatch):
    policy, skills = scoped_skills
    diagnostic = Mock()
    monkeypatch.setattr(_copilot_capabilities, "logger", diagnostic)
    decide = _copilot_capabilities.permission_handler(policy)
    resource = str(skills[1].path / "reference.txt")
    command = "echo private-command-sentinel"
    for request in (
        PermissionRequestRead(intention="private intention", path=resource),
        _shell(command),
    ):
        decide(request, {"session_id": "main.example"})

    assert diagnostic.debug.call_count == 2
    diagnostic.debug.assert_any_call("Copilot permission denied: kind=%s", PermissionRequestRead.kind)
    diagnostic.debug.assert_any_call("Copilot permission denied: kind=%s", PermissionRequestShell.kind)
    assert resource not in repr(diagnostic.debug.call_args_list)
    assert command not in repr(diagnostic.debug.call_args_list)
    assert "private intention" not in repr(diagnostic.debug.call_args_list)


@pytest.mark.parametrize("managed", ["settings", "approval", "bypass"])
def test_scoped_skill_approval_does_not_bypass_managed_or_sandbox_requirements(
    scoped_skills, managed
):
    policy, skills = scoped_skills
    decide = _copilot_capabilities.permission_handler(policy)
    request = PermissionRequestRead(
        intention="resource",
        path=str(skills[0].path / "reference.txt"),
        managed_approval_required=managed == "approval",
        request_sandbox_bypass=managed == "bypass",
    )
    result = decide(request, {
        "session_id": "main.example", "managed_settings_enabled": managed == "settings",
    })
    assert result.kind == PermissionDecisionDeniedByRules.kind


def test_skills_disabled_grants_no_resource_or_script_access(scoped_skills):
    policy, skills = scoped_skills
    disabled = SkillPolicy.create(
        approved=(), discovered=skills, working_directory=policy.working_directory
    )
    decide = _copilot_capabilities.permission_handler(disabled)
    requests = [
        PermissionRequestRead(intention="resource", path=str(skills[0].path / "reference.txt")),
        _shell(f"python '{skills[0].path / 'scripts' / 'run.py'}'"),
    ]
    for request in requests:
        assert decide(request, {"session_id": "main.example"}).kind == PermissionDecisionDeniedByRules.kind


@pytest.mark.asyncio
@pytest.mark.parametrize("enable_skills", [False, True])
@pytest.mark.parametrize("include_mcp", [False, True])
async def test_pinned_sdk_public_create_resume_wire_does_not_treat_empty_arrays_as_resets(
    tmp_path, monkeypatch, enable_skills, include_mcp
):
    sent = []

    async def request(method, payload, **_options):
        sent.append((method, payload))
        if method in ("session.create", "session.resume"):
            return {"sessionId": "main.example"}
        return {"success": True}

    client = CopilotClient(
        connection=RuntimeConnection.for_stdio(),
        mode="empty",
        base_directory=str(tmp_path / "wire-only"),
        use_logged_in_user=False,
        telemetry=None,
    )
    # Only the transport is stubbed; public SDK methods perform their real serialization.
    monkeypatch.setattr(client, "_client", Mock(request=AsyncMock(side_effect=request)))
    monkeypatch.setattr(client, "start", AsyncMock(side_effect=AssertionError("no process")))
    selected = []
    if include_mcp:
        selected.append("mcp:*")
    if enable_skills:
        selected.extend(["builtin:skill", "builtin:view", "builtin:bash"])
    options = {
        "available_tools": selected,
        "enable_config_discovery": False,
        "enable_skills": enable_skills,
        "included_builtin_skills": [],
        "skill_directories": ["approved-individual-directory"] if enable_skills else [],
        "disabled_skills": ["excluded"] if enable_skills else [],
        "mcp_servers": {
            "selected": MCPHTTPServerConfig(
                type="http", url="https://remote.example/mcp", tools=[],
                headers={"Authorization": "Bearer first-token-sentinel"},
            ),
        } if include_mcp else {},
    }
    created = await client.create_session(session_id="main.example", **options)
    await created.disconnect()
    if include_mcp:
        options["mcp_servers"] = {
            "selected": MCPHTTPServerConfig(
                type="http", url="https://remote.example/mcp", tools=[],
                headers={"Authorization": "Bearer second-token-sentinel"},
            ),
        }
    resumed = await client.resume_session("main.example", **options)
    await resumed.disconnect()

    payloads = [
        payload for method, payload in sent if method in ("session.create", "session.resume")
    ]
    assert len(payloads) == 2
    for turn, payload in enumerate(payloads):
        assert payload["sessionId"] == "main.example"
        assert payload["enableSkills"] is enable_skills
        assert payload["enableConfigDiscovery"] is False
        assert payload["availableTools"] == selected
        if include_mcp:
            server = payload["mcpServers"]["selected"]
            assert server["tools"] == []
            assert server["type"] == "http"
            assert server["url"] == "https://remote.example/mcp"
            assert server["headers"] == {
                "Authorization": f"Bearer {'first' if turn == 0 else 'second'}-token-sentinel",
            }
        else:
            assert "mcpServers" not in payload
        if enable_skills:
            assert payload["skillDirectories"] == ["approved-individual-directory"]
            assert payload["disabledSkills"] == ["excluded"]
        else:
            assert "skillDirectories" not in payload
            assert "disabledSkills" not in payload
    patches = [payload for method, payload in sent if method == "session.options.update"]
    assert len(patches) == 2
    assert all(payload["includedBuiltinSkills"] == [] for payload in patches)
    assert all(payload["installedPlugins"] == [] for payload in patches)
    client.start.assert_not_awaited()
