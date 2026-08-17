"""Structural guards for sandbox-local history boundaries.

These checks inspect production source because doubles can prove controller
ordering and typed outcomes, but cannot prove deployment import or credential
boundaries.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = _ROOT / "src" / "azure_functions_agents"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _durable_session_create_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "DurableSessionRecord"
    ]


def _class_method_names(tree: ast.AST, class_name: str) -> set[str]:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                member.name
                for member in node.body
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    raise AssertionError(f"{class_name} was not found")


def test_every_production_session_rewrite_carries_checkpoint_expectation() -> None:
    missing: list[str] = []
    for path in _SOURCE_ROOT.rglob("*.py"):
        for call in _durable_session_create_calls(_tree(path)):
            if "checkpoint_expectation" not in {keyword.arg for keyword in call.keywords}:
                missing.append(f"{path.relative_to(_ROOT)}:{call.lineno}")

    assert not missing, "\n".join(missing)


def test_execution_backend_remains_a_four_method_seam() -> None:
    methods = _class_method_names(
        _tree(_SOURCE_ROOT / "execution" / "backend.py"),
        "AgentExecutionBackend",
    )

    assert methods == {"start_run", "get_run", "read_events", "cancel_run"}


def test_preview_sdk_stays_in_the_transport_adapter() -> None:
    preview_sdk_module = ".".join(("azure", "containerapps", "sandbox"))
    findings = [
        str(path.relative_to(_ROOT))
        for path in _SOURCE_ROOT.rglob("*.py")
        if preview_sdk_module in path.read_text(encoding="utf-8")
        and path != _SOURCE_ROOT / "transport" / "aca_sdk.py"
    ]

    assert not findings, "\n".join(findings)


def test_guest_execution_never_receives_state_storage_configuration() -> None:
    guest_paths = (
        _SOURCE_ROOT / "execution",
        _SOURCE_ROOT / "harness",
    )
    forbidden = ("AzureWebJobsStorage", "blobServiceUri", "DefaultAzureCredential")
    findings = [
        f"{path.relative_to(_ROOT)}: {token}"
        for directory in guest_paths
        for path in directory.rglob("*.py")
        for token in forbidden
        if token in path.read_text(encoding="utf-8")
    ]

    assert not findings, "\n".join(findings)


def test_checkpoint_reader_never_projects_or_writes_history_externally() -> None:
    source = (_SOURCE_ROOT / "controller" / "history_reader.py").read_text(encoding="utf-8")
    forbidden = (
        "BlobHistoryProvider",
        "build_blob_provider_from_environment",
        "append_block",
        "create_append_blob",
        "write_file(",
    )

    assert not [token for token in forbidden if token in source]
