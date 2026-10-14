from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "azure_functions_agents"
_CONCRETE_IMPORT_MARKERS = ("copilot_sdk", "agent_framework")
_CONCRETE_PROSE = re.compile(
    r"\b(MAF|Copilot|agent_framework|copilot|Microsoft Agent Framework|Agent Framework)\b"
)


def _relative(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


def _source(relative: str) -> str:
    return (SRC / relative).read_text(encoding="utf-8")


def _tree(relative: str) -> ast.Module:
    return ast.parse(_source(relative), filename=relative)


def _is_adapter_path(relative: str) -> bool:
    return relative.startswith(("harness/agent_framework/", "harness/copilot_sdk/"))


def _common_modules() -> list[Path]:
    return sorted(path for path in SRC.rglob("*.py") if not _is_adapter_path(_relative(path)))


def _import_modules(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(("." * node.level) + node.module)
    return modules


def _concrete_import_violations(relative: str, source: str) -> list[str]:
    allowed = {
        "harness/_harness_binding.py": {
            ".agent_framework._maf_runner",
            ".copilot_sdk._copilot_preview",
        }
    }
    tree = ast.parse(source, filename=relative)
    violations: list[str] = []
    for module in _import_modules(tree):
        if any(marker in module for marker in _CONCRETE_IMPORT_MARKERS) and module not in allowed.get(
            relative, set()
        ):
            violations.append(f"{relative}: {module}")
    return violations


def _concrete_name_violations(relative: str, source: str) -> list[str]:
    allowed_names = {
        "harness/_harness_binding.py": {"HarnessKind", "MafHarnessAdapter", "MAF", "COPILOT"},
    }
    tree = ast.parse(source, filename=relative)
    violations: list[str] = []
    for node in ast.walk(tree):
        names: list[tuple[str, int]] = []
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            names.append((node.name, node.lineno))
        elif isinstance(node, ast.Name):
            names.append((node.id, node.lineno))
        elif isinstance(node, ast.Attribute):
            names.append((node.attr, node.lineno))
        for name, line in names:
            if name in allowed_names.get(relative, set()):
                continue
            if "Copilot" in name or name.startswith("Maf") or name in {"MAF", "COPILOT"}:
                violations.append(f"{relative}:{line}: {name}")
    return violations


def _allowed_concrete_string(relative: str, value: str) -> bool:
    if relative == "harness/_harness_binding.py":
        return value in {
            "maf",
            "copilot",
            "Agent harness selected: harness=%s",
        }
    if relative == "config/loader.py":
        return value == "agent_framework" or (
            "agent_configuration.agent_framework.compaction.max_context_window_tokens" in value
        )
    return False


def _concrete_string_violations(relative: str, source: str) -> list[str]:
    tree = ast.parse(source, filename=relative)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if _allowed_concrete_string(relative, value):
                continue
            if "agent_framework" in value or re.search(r"\b(copilot|MAF|Copilot)\b", value):
                violations.append(f"{relative}:{node.lineno}: {value!r}")
    return violations


def _docstring_ranges(tree: ast.AST) -> set[tuple[int, int]]:
    ranges: set[tuple[int, int]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ranges.add((first.lineno, first.end_lineno or first.lineno))
    return ranges


def _concrete_prose_violations(relative: str, source: str) -> list[str]:
    tree = ast.parse(source, filename=relative)
    docstring_ranges = _docstring_ranges(tree)
    violations: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT or any(
            start <= token.start[0] <= end for start, end in docstring_ranges
        ):
            line = token.start[0]
            text = token.string
        else:
            continue
        if relative == "config/loader.py" and "agent_framework.compaction" in text:
            continue
        if _CONCRETE_PROSE.search(text):
            violations.append(f"{relative}:{line}: {text.strip()}")
    return violations


def _harness_policy_branch_violations(relative: str, source: str) -> list[str]:
    if relative == "harness/_harness_binding.py":
        return []
    tree = ast.parse(source, filename=relative)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If | ast.Match):
            continue
        names = {
            child.id
            for child in ast.walk(node)
            if isinstance(child, ast.Name)
        }
        if "HarnessKind" in names:
            violations.append(f"{relative}:{node.lineno}: HarnessKind branch")
    return violations


def test_shared_harness_binding_exposes_neutral_state_only() -> None:
    tree = _tree("harness/_harness_binding.py")
    class_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
    }
    assert {"AppHarness", "HarnessAdapter", "HarnessRequest"} <= class_names
    assert {
        "CopilotProvider",
        "CopilotRuntime",
        "StorageRoute",
    }.isdisjoint(class_names)

    resources = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "_HarnessResources"
    )
    fields = [
        target.id
        for statement in resources.body
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
        for target in [statement.target]
    ]
    assert fields == ["guard", "runner"]


def test_shared_registration_has_no_harness_kind_policy_branches() -> None:
    for relative in ("registration/_handlers.py", "registration/endpoints.py"):
        assert _harness_policy_branch_violations(relative, _source(relative)) == []


def test_backend_imports_in_shared_harness_are_limited_to_composition_points() -> None:
    for relative in ("harness/_harness_binding.py", "harness/_agent_runner.py"):
        assert _concrete_import_violations(relative, _source(relative)) == []


def test_common_modules_keep_concrete_sdk_knowledge_at_allowed_boundaries() -> None:
    violations: list[str] = []
    for path in _common_modules():
        relative = _relative(path)
        source = path.read_text(encoding="utf-8")
        violations.extend(_concrete_import_violations(relative, source))
        violations.extend(_concrete_name_violations(relative, source))
        violations.extend(_concrete_string_violations(relative, source))
        violations.extend(_harness_policy_branch_violations(relative, source))
    assert violations == []


def test_common_module_comments_and_docstrings_stay_runtime_neutral() -> None:
    violations: list[str] = []
    for path in _common_modules():
        relative = _relative(path)
        violations.extend(_concrete_prose_violations(relative, path.read_text(encoding="utf-8")))
    assert violations == []


def test_neutrality_scanners_catch_review_examples() -> None:
    assert _concrete_import_violations(
        "runner.py",
        "from .harness.agent_framework._maf_tools import build_maf_tools\n",
    )
    assert _concrete_name_violations("runner.py", "if kind is HarnessKind.COPILOT:\n    pass\n")
    assert _concrete_string_violations("runner.py", 'ROOT = "agent_framework"\n')
    assert _concrete_prose_violations("runner.py", '"""MAF-specific behavior."""\n')


def test_neutrality_scanners_allow_sanctioned_boundaries() -> None:
    assert _concrete_string_violations(
        "config/loader.py",
        'FIELD = "agent_configuration.agent_framework.compaction.max_context_window_tokens"\n',
    ) == []
    assert _concrete_string_violations("harness/_harness_binding.py", 'VALUE = "copilot"\n') == []
    assert _concrete_import_violations(
        "harness/_harness_binding.py",
        "from .copilot_sdk._copilot_preview import select_copilot_harness\n",
    ) == []
