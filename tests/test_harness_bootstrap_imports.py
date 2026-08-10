from __future__ import annotations

import ast
from pathlib import Path

_STDLIB_ROOTS = frozenset(
    {
        "argparse",
        "asyncio",
        "collections",
        "dataclasses",
        "hashlib",
        "importlib",
        "inspect",
        "io",
        "json",
        "os",
        "pathlib",
        "re",
        "shutil",
        "site",
        "stat",
        "sys",
        "sysconfig",
        "typing",
        "zipfile",
    }
)


def test_bootstrap_imports_only_stdlib_modules() -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / "src" / "azure_functions_agents" / "harness" / "bootstrap.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    imports = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        (node.module or "").split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.level == 0
        and node.module is not None
        and node.module != "__future__"
    )

    assert imports <= _STDLIB_ROOTS
