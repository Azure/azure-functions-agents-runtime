from __future__ import annotations

import builtins
import warnings

import pytest

from azure_functions_agents.harness.agent_framework._maf_warnings import (
    suppress_experimental_warnings,
)


def test_suppression_is_limited_to_maf_experimental_category() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with suppress_experimental_warnings():
            from agent_framework._feature_stage import ExperimentalWarning

            warnings.warn("MAF preview feature", ExperimentalWarning, stacklevel=2)
            warnings.warn("unrelated experimental warning", UserWarning, stacklevel=2)

    assert [str(warning.message) for warning in caught] == [
        "unrelated experimental warning"
    ]


def test_experimental_warning_behavior_is_unchanged_outside_maf_operation() -> None:
    with suppress_experimental_warnings():
        from agent_framework._feature_stage import ExperimentalWarning

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warnings.warn("MAF preview feature", ExperimentalWarning, stacklevel=2)

    assert [str(warning.message) for warning in caught] == ["MAF preview feature"]


def test_importerror_fallback_restores_recorded_warnings_before_yield(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def failing_import(name, globals=None, locals=None, fromlist=(), level=0):  # type: ignore[no-untyped-def]
        if name == "agent_framework._feature_stage":
            warnings.warn("import warning", UserWarning, stacklevel=2)
            raise ImportError("simulated missing warning category")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", failing_import)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with suppress_experimental_warnings():
            warnings.warn("operation warning", UserWarning, stacklevel=2)

    assert [str(warning.message) for warning in caught] == [
        "import warning",
        "operation warning",
    ]
