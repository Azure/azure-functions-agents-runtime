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


def test_tool_shim_is_callable() -> None:
    assert callable(tool)
