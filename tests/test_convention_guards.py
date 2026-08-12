"""Executable guards for repository-wide Python conventions."""

from __future__ import annotations

import ast
import io
import re
import tokenize
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

_PROVENANCE_PATTERNS = (
    ("decision citation", re.compile(r"\bDecisions?\s*#?\s*\d+\b", re.IGNORECASE)),
    ("pull request citation", re.compile(r"\b(?:PR|pull request)\s*#?\s*\d+\b", re.IGNORECASE)),
    (
        "phase label",
        re.compile(
            r"\bP\d+[a-z]\b|\bFRD\s*\d{4}\b[^\n]{0,80}\bP\d+\b|"
            r"\bP\d+\b(?=\s*(?:extension|implementation|phase|:))",
        ),
    ),
)
_CONCRETE_RUNTIME_TYPES = frozenset(
    {
        "AcaSandboxAdapter",
        "AcaSandboxExecutionBackend",
        "AcaSandboxHandle",
    }
)
_CANONICAL_PATH_MODULES = frozenset(
    {
        "src/azure_functions_agents/journal_paths.py",
        "src/azure_functions_agents/transport/manifest.py",
    }
)


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _frozen_dataclass_has_post_init(tree: ast.AST) -> list[int]:
    findings: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not _is_explicitly_frozen_dataclass(node):
            continue
        findings.extend(
            member.lineno
            for member in node.body
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            and member.name == "__post_init__"
        )
    return findings


def _is_explicitly_frozen_dataclass(node: ast.ClassDef) -> bool:
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call) or not _is_dataclass_decorator(decorator.func):
            continue
        for keyword in decorator.keywords:
            if (
                keyword.arg == "frozen"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
            ):
                return True
    return False


def _is_dataclass_decorator(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "dataclass"
    if not isinstance(node, ast.Attribute) or node.attr != "dataclass":
        return False
    return isinstance(node.value, ast.Name) and node.value.id == "dataclasses"


def _module_name_collisions(paths: Iterable[Path], root: Path) -> dict[str, list[str]]:
    by_name: defaultdict[str, list[str]] = defaultdict(list)
    for path in paths:
        if path.name != "__init__.py":
            by_name[path.name].append(str(path.relative_to(root)))
    return {name: entries for name, entries in by_name.items() if len(entries) > 1}


def _provenance_violations(source: str, tree: ast.AST) -> list[str]:
    violations = _comment_violations(source)
    violations.extend(_docstring_violations(tree))
    violations.extend(_assert_message_violations(tree))
    return violations


def _comment_violations(source: str) -> list[str]:
    violations: list[str] = []
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    for token in tokens:
        if token.type == tokenize.COMMENT:
            violations.extend(_matches(token.string, f"comment:{token.start[0]}"))
    return violations


def _docstring_violations(tree: ast.AST) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        docstring = ast.get_docstring(node, clean=False)
        if docstring is not None:
            violations.extend(_matches(docstring, f"docstring:{node.body[0].lineno}"))
    return violations


def _assert_message_violations(tree: ast.AST) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert) and node.msg is not None:
            for text in _literal_texts(node.msg):
                violations.extend(_matches(text, f"assertion:{node.lineno}"))
    return violations


def _literal_texts(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return [_joined_string_text(node)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_texts(node.left)
        right = _literal_texts(node.right)
        if len(left) == 1 and len(right) == 1:
            return [left[0] + right[0]]
    return []


def _joined_string_text(node: ast.JoinedStr) -> str:
    return "".join(
        value.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
        else _formatted_value_text(value)
        for value in node.values
    )


def _formatted_value_text(node: ast.AST) -> str:
    if not isinstance(node, ast.FormattedValue):
        return "0"
    texts = _literal_texts(node.value)
    return texts[0] if len(texts) == 1 else "0"


def _matches(text: str, location: str) -> list[str]:
    return [
        f"{location}: {name}"
        for name, pattern in _PROVENANCE_PATTERNS
        if pattern.search(text)
    ]


def _repository_provenance_findings(paths: Iterable[Path], root: Path) -> list[str]:
    findings: list[str] = []
    for path in paths:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        findings.extend(
            f"{path.relative_to(root)} {violation}"
            for violation in _provenance_violations(source, tree)
        )
    return findings


def _imports_aca_sdk_adapter(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.endswith("transport.aca_sdk"):
                return True
            if module.endswith("transport") and any(alias.name == "aca_sdk" for alias in node.names):
                return True
        if isinstance(node, ast.Import) and any(
            alias.name.endswith("transport.aca_sdk") for alias in node.names
        ):
            return True
    return False


def _concrete_runtime_cast_lines(tree: ast.AST) -> list[int]:
    findings: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "cast" or not node.args:
            continue
        rendered_target = ast.unparse(node.args[0])
        if any(runtime_type in rendered_target for runtime_type in _CONCRETE_RUNTIME_TYPES):
            findings.append(node.lineno)
    return findings


def _hardcoded_var_lib_lines(tree: ast.AST) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "/var/lib" in node.value
    ]


def _harness_journal_error_messages(tree: ast.AST) -> tuple[list[str], list[int]]:
    """Return literal HarnessJournalError messages and the line of any non-literal argument."""
    literals: list[str] = []
    dynamic: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "HarnessJournalError" or not node.args:
            continue
        argument = node.args[0]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            literals.append(argument.value)
        else:
            dynamic.append(node.lineno)
    return literals, dynamic


def test_frozen_dataclass_guard_detects_direct_post_init_only() -> None:
    frozen_tree = ast.parse(
        """
from dataclasses import dataclass

@dataclass(frozen=True)
class Frozen:
    def __post_init__(self) -> None:
        pass
"""
    )
    mutable_tree = ast.parse(
        """
from dataclasses import dataclass

@dataclass
class Mutable:
    def __post_init__(self) -> None:
        pass
"""
    )

    assert _frozen_dataclass_has_post_init(frozen_tree) == [6]
    assert _frozen_dataclass_has_post_init(mutable_tree) == []


def test_hardcoded_var_lib_guard_detects_literal_paths() -> None:
    tree = ast.parse('ROOT = "/var/lib/example"\n')

    assert _hardcoded_var_lib_lines(tree) == [1]


def test_provenance_guard_detects_only_governed_text_locations() -> None:
    source = '''
# PR #42
def example() -> None:
    """FRD 0008 Decision #107."""
    assert False, "P4a"

message = "Decision #12"
'''
    tree = ast.parse(source)

    assert _provenance_violations(source, tree) == [
        "comment:2: pull request citation",
        "docstring:4: decision citation",
        "assertion:5: phase label",
    ]


def test_provenance_guard_detects_dynamic_assertion_messages() -> None:
    source = '''
def example(number: int) -> None:
    assert False, f"FRD {number:04d} Decision #{number}"
'''
    tree = ast.parse(source)

    assert _provenance_violations(source, tree) == ["assertion:3: decision citation"]


def test_provenance_guard_detects_static_f_string_expressions() -> None:
    source = '''
def example() -> None:
    assert False, f"{'Decision #12'}"
'''
    tree = ast.parse(source)

    assert _provenance_violations(source, tree) == ["assertion:3: decision citation"]


def test_source_has_no_frozen_dataclass_post_init() -> None:
    root = _repository_root()
    findings = [
        f"{path.relative_to(root)}:{line}"
        for path in _python_files(root / "src")
        for line in _frozen_dataclass_has_post_init(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
    ]

    assert not findings, "\n".join(findings)


def test_source_module_basenames_are_unique() -> None:
    root = _repository_root()
    collisions = _module_name_collisions(_python_files(root / "src"), root / "src")

    assert not collisions, "\n".join(
        f"{name}: {', '.join(paths)}" for name, paths in sorted(collisions.items())
    )


def test_source_paths_are_centralized_in_canonical_path_modules() -> None:
    root = _repository_root()
    excluded = Path(__file__).resolve()
    paths = [
        path
        for directory in (root / "src", root / "tests")
        for path in _python_files(directory)
        if path != excluded
    ]
    findings = [
        f"{path.relative_to(root)}:{line}"
        for path in paths
        if str(path.relative_to(root)).replace("\\", "/") not in _CANONICAL_PATH_MODULES
        for line in _hardcoded_var_lib_lines(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
    ]

    assert not findings, "\n".join(findings)


def test_execution_and_controller_code_stay_behind_protocols() -> None:
    root = _repository_root()
    module_paths = [
        *sorted((root / "src" / "azure_functions_agents" / "execution").glob("*.py")),
        *sorted((root / "src" / "azure_functions_agents" / "controller").glob("*.py")),
    ]
    sdk_imports: list[str] = []
    concrete_casts: list[str] = []
    for path in module_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _imports_aca_sdk_adapter(tree):
            sdk_imports.append(str(path.relative_to(root)))
        concrete_casts.extend(
            f"{path.relative_to(root)}:{line}"
            for line in _concrete_runtime_cast_lines(tree)
        )

    assert not sdk_imports, "\n".join(sdk_imports)
    assert not concrete_casts, "\n".join(concrete_casts)


def test_source_and_tests_have_no_feature_bookkeeping() -> None:
    root = _repository_root()
    excluded = Path(__file__).resolve()
    paths = [
        path
        for directory in (root / "src", root / "tests")
        for path in _python_files(directory)
        if path != excluded
    ]
    findings = _repository_provenance_findings(paths, root)

    assert not findings, "\n".join(findings)


def test_harness_launch_diagnostics_are_allow_listed() -> None:
    from azure_functions_agents.journal_paths import ALLOWED_LAUNCH_DIAGNOSTICS

    root = _repository_root()
    harness = root / "src" / "azure_functions_agents" / "harness"
    missing: list[str] = []
    dynamic: list[str] = []
    for path in (harness / "__main__.py", harness / "journal_writer.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        literals, dynamic_lines = _harness_journal_error_messages(tree)
        missing.extend(
            f"{path.relative_to(root)}: {message!r}"
            for message in literals
            if message not in ALLOWED_LAUNCH_DIAGNOSTICS
        )
        dynamic.extend(f"{path.relative_to(root)}:{line}" for line in dynamic_lines)

    assert not dynamic, "non-literal HarnessJournalError argument(s): " + ", ".join(dynamic)
    assert not missing, "HarnessJournalError message(s) absent from the allow-list:\n" + "\n".join(
        missing
    )
