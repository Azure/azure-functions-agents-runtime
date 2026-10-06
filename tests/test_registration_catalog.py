from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from azure_functions_agents._function_tool import tool
from azure_functions_agents.config.schema import BuiltinEndpointsConfig, ResolvedAgent, ToolsFilter
from azure_functions_agents.discovery.mcp import MCPServerDescriptor
from azure_functions_agents.discovery.skills import SkillDescriptor
from azure_functions_agents.registration.capabilities import (
    AgentCapabilities,
    with_runtime_skill_paths,
)
from azure_functions_agents.registration.catalog import CatalogEntry, build_catalog


def _resolved() -> ResolvedAgent:
    return ResolvedAgent(
        name="Leaf", slug="leaf", description="Leaf", trigger=None, instructions="Leaf",
        is_main=False, builtin_endpoints=BuiltinEndpointsConfig(), model=None, timeout=1.0,
        enabled_mcp_names=[], enabled_skills_names=[], tool_filter=ToolsFilter(),
        sandbox_config=None, input_schema=None, response_schema=None, response_example=None,
    )


def test_catalog_snapshots_capability_lists_without_sdk_objects(tmp_path: Path) -> None:
    user = tool(lambda: "user", name="local")
    server = MCPServerDescriptor(
        name="remote", url="https://fixture.invalid/mcp", transport="http",
        headers=(), tools=None, auth_scope=None, client_id=None,
    )
    skill = SkillDescriptor(name="selected", path=tmp_path / "selected")
    users, servers, skills = [user], [server], [skill]
    capabilities = AgentCapabilities(
        filtered_user_tools=users, filtered_mcp_tools=servers, skills=skills, skill_catalog=skills,
    )
    resolved = _resolved()
    catalog = build_catalog({"leaf": CatalogEntry(resolved, capabilities)})
    frozen = catalog["leaf"].capabilities
    users.clear()
    servers.clear()
    skills.clear()

    assert frozen.filtered_user_tools == (user,)
    assert frozen.filtered_mcp_tools == (server,)
    assert frozen.skills == frozen.skill_catalog == (skill,)
    assert frozen.enabled_skill_paths == (skill.path,)
    with pytest.raises(FrozenInstanceError):
        frozen.skills = ()
    with pytest.raises(TypeError):
        catalog["other"] = catalog["leaf"]


def test_runtime_guidance_copy_never_changes_catalog_leaf_inventory(tmp_path: Path) -> None:
    project = SkillDescriptor(name="project", path=tmp_path / "project")
    excluded = SkillDescriptor(
        name="excluded-child", path=project.path / "excluded-child",
    )
    runtime = tmp_path / "data-driven-workflows"
    runtime.mkdir()
    (runtime / "SKILL.md").write_text(
        "---\nname: data-driven-workflows\ndescription: Workflow guidance\n---\n",
        encoding="utf-8",
    )
    child = runtime / "nested"
    child.mkdir()
    (child / "SKILL.md").write_text(
        "---\nname: runtime-child\ndescription: Nested runtime guidance\n---\n",
        encoding="utf-8",
    )
    capabilities = AgentCapabilities.create(skills=(project,), skill_catalog=(project, excluded))
    resolved = _resolved()
    catalog = build_catalog({"leaf": CatalogEntry(resolved, capabilities)})
    direct = with_runtime_skill_paths(catalog["leaf"].capabilities, (runtime,))

    assert [skill.name for skill in direct.skills] == ["project", "data-driven-workflows"]
    assert {skill.name for skill in direct.skill_catalog} == {
        "project", "excluded-child", "data-driven-workflows",
    }
    assert catalog["leaf"].capabilities.skills == (project,)
    assert catalog["leaf"].capabilities.skill_catalog == (project, excluded)
