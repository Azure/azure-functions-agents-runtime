from __future__ import annotations

from pathlib import Path

from azure_functions_agents._function_tool import tool


def test_package_imports_resolve_to_real_init() -> None:
    import azure_functions_agents

    package_path = Path(azure_functions_agents.__file__).resolve()
    expected_path = (
        Path(__file__).resolve().parents[1] / "src" / "azure_functions_agents" / "__init__.py"
    )

    assert package_path == expected_path
    for name in azure_functions_agents.__all__:
        assert hasattr(azure_functions_agents, name)


def test_public_exports_include_only_supported_preview_api() -> None:
    import azure_functions_agents

    assert azure_functions_agents.__all__ == [
        "DEFAULT_MODEL",
        "DEFAULT_TIMEOUT",
        "AgentResult",
        "ClientManager",
        "MAFClientManager",
        "create_function_app",
        "create_sandbox_tools",
        "get_client_manager",
        "resolve_config_dir",
        "run_agent",
        "run_agent_stream",
        "set_app_root",
        "set_client_manager",
        "shutdown_client_manager",
        "tool",
    ]
    assert not hasattr(azure_functions_agents, "run_copilot_agent")
    assert not hasattr(azure_functions_agents, "run_copilot_agent_stream")


def test_tool_shim_is_callable() -> None:
    assert callable(tool)
