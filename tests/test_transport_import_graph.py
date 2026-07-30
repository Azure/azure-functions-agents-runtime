"""Architecture guards for P4a's preview-SDK containment and safe adapter shape."""

from __future__ import annotations

import ast
from pathlib import Path

_SDK_MODULE_PARTS = ("azure", "containerapps", "sandbox")


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _python_files(root: Path) -> list[Path]:
    return sorted(
        [
            *root.joinpath("src").rglob("*.py"),
            *root.joinpath("tests").rglob("*.py"),
        ]
    )


def _is_preview_sdk_module(module: str | None) -> bool:
    if module is None:
        return False
    return tuple(module.split(".")[:3]) == _SDK_MODULE_PARTS


def test_preview_sdk_imports_and_text_are_confined_to_one_production_adapter() -> None:
    root = _repository_root()
    adapter = root / "src" / "azure_functions_agents" / "transport" / "aca_sdk.py"
    module_name = ".".join(_SDK_MODULE_PARTS)

    for path in _python_files(root):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        imports_preview_sdk = any(
            (
                isinstance(node, ast.ImportFrom) and _is_preview_sdk_module(node.module)
            )
            or (
                isinstance(node, ast.Import)
                and any(_is_preview_sdk_module(alias.name) for alias in node.names)
            )
            for node in ast.walk(tree)
        )
        if imports_preview_sdk or module_name in source:
            assert path == adapter


def test_production_transport_does_not_import_test_doubles() -> None:
    root = _repository_root()
    for path in root.joinpath("src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module is None or not node.module.startswith("tests")
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith("tests") for alias in node.names)


def test_adapter_cannot_open_ports_or_bypass_the_egress_proxy() -> None:
    adapter = (
        _repository_root()
        / "src"
        / "azure_functions_agents"
        / "transport"
        / "aca_sdk.py"
    )
    source = adapter.read_text(encoding="utf-8")
    prohibited_port_operation = "add" + "_port"
    unsafe_proxy_flag = "skip_egress_proxy=" + "True"

    assert prohibited_port_operation not in source
    assert unsafe_proxy_flag not in source
    assert "ports=[]" in source
    assert "skip_egress_proxy=False" in source
    assert 'default_action="Deny"' in source
