"""FRD 0009 C0: characterization tests locking the *existing* (pre-Durable)
ordinary/workflow app-selection and registration-inventory behavior.

These tests make no product changes. They exist purely as a regression
baseline before the C1 dependency bump (Durable b2->b3, `durabletask`, MAF
trio) and later Durable-specific registration additions (C2c/`registration/
capabilities.py` changes) so any accidental drift in:

* which concrete ``DFApp``/plain ``FunctionApp`` gets chosen, and
* the *exact* set of registered function names/HTTP routes/binding types for
  a given ``builtin_endpoints`` combination, including a mixed
  ordinary + workflow-enabled app,

...is caught immediately. See docs/frds/0009-durable-agent-loop.md §4.14 (C0).
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import azure.durable_functions as df

from azure_functions_agents.app import create_function_app


def _write_agent(tmp_path: Path, filename: str, frontmatter: str, body: str = "Assist.") -> None:
    cleaned_frontmatter = textwrap.dedent(frontmatter).strip()
    cleaned_body = textwrap.dedent(body).strip()
    (tmp_path / filename).write_text(
        f"---\n{cleaned_frontmatter}\n---\n{cleaned_body}\n",
        encoding="utf-8",
    )


def _function_names(functions: list[Any]) -> list[str]:
    return [function.get_function_name() for function in functions]


def _http_routes(functions: list[Any]) -> list[str]:
    routes: list[str] = []
    for function in functions:
        for binding in function.get_bindings():
            route = getattr(binding, "route", None)
            if route is not None:
                routes.append(route)
    return routes


def _binding_types(function: Any) -> list[str]:
    return [binding.get_dict_repr()["type"] for binding in function.get_bindings()]


def _functions_by_name(functions: list[Any]) -> dict[str, Any]:
    return {function.get_function_name(): function for function in functions}


# ---------------------------------------------------------------------------
# Exact registration inventory across builtin_endpoints combinations
# (single ordinary, non-workflow agent).
# ---------------------------------------------------------------------------


def test_chat_api_only_registers_chat_chatstream_history_but_not_page_or_mcp(
    tmp_path: Path,
) -> None:
    _write_agent(
        tmp_path,
        "main.agent.md",
        """
        name: Main Chat
        description: Desc
        builtin_endpoints:
            chat_api: true
        """,
    )

    app = create_function_app(tmp_path)
    functions = app.get_functions()

    assert _function_names(functions) == [
        "agent_main_builtin_chat",
        "agent_main_builtin_chatstream",
        "agent_main_builtin_history",
    ]
    assert _http_routes(functions) == [
        "agents/main/chat",
        "agents/main/chatstream",
        "agents/main/history",
    ]


def test_mcp_only_registers_only_the_mcp_function(tmp_path: Path) -> None:
    _write_agent(
        tmp_path,
        "main.agent.md",
        """
        name: Main Chat
        description: Desc
        builtin_endpoints:
            mcp: true
        """,
    )

    app = create_function_app(tmp_path)
    functions = app.get_functions()

    assert _function_names(functions) == ["agent_main_builtin_mcp"]
    # The MCP endpoint is a tool trigger, not an HTTP route.
    assert _http_routes(functions) == []


def test_debug_chat_ui_alone_forces_chat_api_and_registers_full_http_surface(
    tmp_path: Path,
) -> None:
    """``debug_chat_ui: true`` implies ``chat_api: true`` (schema validator)
    even when the author never wrote ``chat_api`` explicitly."""
    _write_agent(
        tmp_path,
        "main.agent.md",
        """
        name: Main Chat
        description: Desc
        builtin_endpoints:
            debug_chat_ui: true
        """,
    )

    app = create_function_app(tmp_path)
    functions = app.get_functions()

    assert _function_names(functions) == [
        "agent_main_builtin_chat_page",
        "agent_main_builtin_chat",
        "agent_main_builtin_chatstream",
        "agent_main_builtin_history",
    ]
    assert _http_routes(functions) == [
        "agents/main/",
        "agents/main/chat",
        "agents/main/chatstream",
        "agents/main/history",
    ]


def test_all_builtin_endpoints_enabled_registers_the_complete_five_function_inventory(
    tmp_path: Path,
) -> None:
    _write_agent(
        tmp_path,
        "main.agent.md",
        """
        name: Main Chat
        description: Desc
        builtin_endpoints:
            debug_chat_ui: true
            chat_api: true
            mcp: true
        """,
    )

    app = create_function_app(tmp_path)
    functions = app.get_functions()

    assert _function_names(functions) == [
        "agent_main_builtin_chat_page",
        "agent_main_builtin_chat",
        "agent_main_builtin_chatstream",
        "agent_main_builtin_history",
        "agent_main_builtin_mcp",
    ]
    assert _http_routes(functions) == [
        "agents/main/",
        "agents/main/chat",
        "agents/main/chatstream",
        "agents/main/history",
    ]


# ---------------------------------------------------------------------------
# Mixed ordinary + workflow-enabled agents coexisting in one app.
# ---------------------------------------------------------------------------


def test_mixed_ordinary_and_workflow_agents_share_one_durable_app_with_scoped_bindings(
    tmp_path: Path,
) -> None:
    """One ordinary (workflows-disabled) agent and one workflow-enabled agent
    registered together must:

    * select a single ``DFApp`` for the whole app (any workflow agent forces
      this app-wide, per ``app.py:create_function_app``'s existing
      selection rule — ``test_app_routes.py`` proves this for a lone
      workflow agent; this proves it still holds with an ordinary agent
      alongside it),
    * register the ordinary agent's endpoints with NO ``durableClient``
      binding, and
    * register the workflow agent's endpoints WITH a ``durableClient``
      binding,

    while keeping the exact overall function-name inventory stable — the
    regression baseline this C0 slice exists to lock down before FRD 0009's
    dependency bump (C1) or later Durable-specific registration additions.
    """
    _write_agent(
        tmp_path,
        "main.agent.md",
        """
        name: Main
        description: Ordinary main agent.
        builtin_endpoints:
            chat_api: true
        """,
    )
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_agent(
        agents_dir,
        "worker.agent.md",
        """
        name: Worker
        description: Workflow-enabled worker agent.
        builtin_endpoints:
            chat_api: true
        workflows:
            enabled: true
        """,
    )

    app = create_function_app(tmp_path)

    # A DFApp is selected app-wide the moment any agent enables workflows,
    # even though "main" itself never does.
    assert isinstance(app, df.DFApp)

    functions = app.get_functions()
    # Enabling workflows anywhere in the app also registers the shared
    # workflow-engine functions once (the durable-client-bearing orchestrator
    # and its two supporting activities, plus the generic HTTP-poll
    # activity/orchestrator pair) ahead of any per-agent endpoint —
    # unaffected by "main" itself never enabling workflows.
    assert _function_names(functions) == [
        "BuiltIn__HttpActivity",
        "BuiltIn__HttpPollOrchestrator",
        "agents_workflow_run_tool",
        "agents_workflow_run_sub_agent",
        "agents_workflow_orchestrator",
        "agent_worker_builtin_chat",
        "agent_worker_builtin_chatstream",
        "agent_worker_builtin_history",
        "agent_worker_builtin_workflows",
        "agent_worker_builtin_workflow_status",
        "agent_main_builtin_chat",
        "agent_main_builtin_chatstream",
        "agent_main_builtin_history",
    ]

    by_name = _functions_by_name(functions)
    for name in (
        "agent_main_builtin_chat",
        "agent_main_builtin_chatstream",
        "agent_main_builtin_history",
    ):
        assert "durableClient" not in _binding_types(by_name[name]), name
    # "history" reads request-scoped local/Blob transcript storage directly —
    # it never needs a durable-client binding even for a workflow-enabled
    # agent, unlike the other four worker endpoints.
    assert "durableClient" not in _binding_types(by_name["agent_worker_builtin_history"])
    for name in (
        "agent_worker_builtin_chat",
        "agent_worker_builtin_chatstream",
        "agent_worker_builtin_workflows",
        "agent_worker_builtin_workflow_status",
    ):
        assert "durableClient" in _binding_types(by_name[name]), name
