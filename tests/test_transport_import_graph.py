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


def _imports_preview_sdk(tree: ast.AST) -> bool:
    """Detect every import shape that would reach the preview SDK.

    Covers a plain ``import`` of the SDK's fully-qualified module path (with
    or without an alias), a ``from ... import`` naming that same path
    directly, and the aliasing gap where the SDK's parent package is named in
    the ``from`` clause while its final component is only named in the
    ``import`` clause — e.g. ``from azure.containerapps import sandbox``.
    """

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            if _is_preview_sdk_module(node.module):
                return True
            module_parts = tuple(node.module.split("."))
            if any(
                (*module_parts, alias.name)[:3] == _SDK_MODULE_PARTS for alias in node.names
            ):
                return True
        elif isinstance(node, ast.Import):
            if any(_is_preview_sdk_module(alias.name) for alias in node.names):
                return True
    return False


def test_preview_sdk_imports_and_text_are_confined_to_one_production_adapter() -> None:
    root = _repository_root()
    adapter = root / "src" / "azure_functions_agents" / "transport" / "aca_sdk.py"
    module_name = ".".join(_SDK_MODULE_PARTS)

    for path in _python_files(root):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        if _imports_preview_sdk(tree) or module_name in source:
            assert path == adapter


def test_import_guard_catches_the_split_from_import_aliasing_gap() -> None:
    """A regression guard for the guard itself: ``from <parent> import <leaf>`` shapes.

    ``node.module`` alone (the SDK's parent package) does not contain the
    full SDK path, and this import form never spells the full dotted path
    out contiguously in source, so only checking ``node.module`` (or scanning
    source text for that joined string) would silently miss it.
    """

    module_name = ".".join(_SDK_MODULE_PARTS)
    parent_module, leaf_module = _SDK_MODULE_PARTS[:2], _SDK_MODULE_PARTS[2]
    split_import = f"from {'.'.join(parent_module)} import {leaf_module}\n"
    assert module_name not in split_import

    tree = ast.parse(split_import)
    assert _imports_preview_sdk(tree)

    aliased_tree = ast.parse(f"{split_import.rstrip()} as sdk\n")
    assert _imports_preview_sdk(aliased_tree)

    unrelated_tree = ast.parse(f"from {'.'.join(parent_module)} import images\n")
    assert not _imports_preview_sdk(unrelated_tree)


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
