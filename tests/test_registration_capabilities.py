"""Tests for capability filtering — wiring user-tools, MCP, and skill paths."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from azure_functions_agents.registration.capabilities import (
    AgentCapabilities,
    build_capabilities,
)


def _resolved(
    *,
    enabled_skills_names: list[str] | None = None,
    skills_disabled: bool = False,
    enabled_mcp_names: list[str] | None = None,
    mcp_disabled: bool = False,
    tools_disabled: bool = False,
    exclude: list[str] | None = None,
) -> Any:
    return SimpleNamespace(
        enabled_skills_names=enabled_skills_names or [],
        skills_disabled=skills_disabled,
        enabled_mcp_names=enabled_mcp_names or [],
        mcp_disabled=mcp_disabled,
        tools_disabled=tools_disabled,
        tool_filter=SimpleNamespace(exclude=exclude or []),
    )


def _named_tool(name: str) -> Any:
    return SimpleNamespace(name=name)


def test_build_capabilities_maps_enabled_skills_to_paths(tmp_path: Path) -> None:
    skill_dir_a = tmp_path / "alpha"
    skill_dir_b = tmp_path / "beta"
    skill_dir_a.mkdir()
    skill_dir_b.mkdir()
    capabilities = build_capabilities(
        _resolved(enabled_skills_names=["alpha", "beta"]),
        discovered_user_tools=[],
        discovered_mcp_tools={},
        discovered_skills={"alpha": skill_dir_a, "beta": skill_dir_b},
    )
    assert capabilities.enabled_skill_paths == [skill_dir_a, skill_dir_b]


def test_build_capabilities_skips_unknown_enabled_skill_names(tmp_path: Path) -> None:
    skill_dir_a = tmp_path / "alpha"
    skill_dir_a.mkdir()
    capabilities = build_capabilities(
        _resolved(enabled_skills_names=["alpha", "missing"]),
        discovered_user_tools=[],
        discovered_mcp_tools={},
        discovered_skills={"alpha": skill_dir_a},
    )
    assert capabilities.enabled_skill_paths == [skill_dir_a]


def test_build_capabilities_skills_disabled_returns_empty(tmp_path: Path) -> None:
    skill_dir_a = tmp_path / "alpha"
    skill_dir_a.mkdir()
    capabilities = build_capabilities(
        _resolved(enabled_skills_names=["alpha"], skills_disabled=True),
        discovered_user_tools=[],
        discovered_mcp_tools={},
        discovered_skills={"alpha": skill_dir_a},
    )
    assert capabilities.enabled_skill_paths == []


def test_build_capabilities_filters_user_tools_by_exclude_name() -> None:
    capabilities = build_capabilities(
        _resolved(exclude=["keep_out"]),
        discovered_user_tools=[_named_tool("keep"), _named_tool("keep_out")],
        discovered_mcp_tools={},
        discovered_skills={},
    )
    assert capabilities.filtered_user_tools is not None
    assert [t.name for t in capabilities.filtered_user_tools] == ["keep"]


def test_build_capabilities_tools_disabled_returns_empty_user_tools() -> None:
    capabilities = build_capabilities(
        _resolved(tools_disabled=True),
        discovered_user_tools=[_named_tool("anything")],
        discovered_mcp_tools={},
        discovered_skills={},
    )
    assert capabilities.filtered_user_tools == []


def test_build_capabilities_mcp_disabled_returns_empty_mcp_tools() -> None:
    capabilities = build_capabilities(
        _resolved(enabled_mcp_names=["srv"], mcp_disabled=True),
        discovered_user_tools=[],
        discovered_mcp_tools={"srv": SimpleNamespace(name="srv")},  # type: ignore[dict-item]
        discovered_skills={},
    )
    assert capabilities.filtered_mcp_tools == []


def test_agent_capabilities_defaults_are_independent_lists() -> None:
    a = AgentCapabilities()
    b = AgentCapabilities()
    a.enabled_skill_paths.append(Path("x"))
    assert b.enabled_skill_paths == []


def test_build_skills_provider_returns_none_for_empty() -> None:
    from azure_functions_agents.runner import _build_skills_provider

    assert _build_skills_provider(None) is None
    assert _build_skills_provider([]) is None


def test_build_skills_provider_returns_provider_for_skill_paths(tmp_path: Path) -> None:
    pytest.importorskip("agent_framework")
    skill_dir = tmp_path / "alpha"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: A test skill.\n---\n\n# Alpha\n",
        encoding="utf-8",
    )

    from azure_functions_agents.runner import _build_skills_provider

    provider = _build_skills_provider([skill_dir])

    # We don't depend on a specific attribute layout — just that the helper
    # returns a non-None ``ContextProvider`` from MAF when given paths.
    assert provider is not None
    from agent_framework import ContextProvider

    assert isinstance(provider, ContextProvider)
