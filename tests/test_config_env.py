from __future__ import annotations

import pytest

from azure_functions_agents.config.env import (
    _to_bool,
    resolve_env_var,
    substitute_env_vars_in_text,
)


def test_resolve_env_var_dollar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOO", "value")
    assert resolve_env_var("$FOO") == "value"
    monkeypatch.delenv("FOO", raising=False)
    assert resolve_env_var("$FOO") == "$FOO"


def test_resolve_env_var_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOO", "value")
    assert resolve_env_var("%FOO%") == "value"
    monkeypatch.delenv("FOO", raising=False)
    assert resolve_env_var("%FOO%") == "%FOO%"


def test_full_string_percent_rejects_invalid_var_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("1 BAD-NAME", "value")
    assert resolve_env_var("%1 BAD-NAME%") == "%1 BAD-NAME%"


def test_full_string_percent_accepts_valid_var_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOO", "bar")
    assert resolve_env_var("%FOO%") == "bar"


def test_resolve_env_var_plain() -> None:
    assert resolve_env_var("plain") == "plain"


def test_resolve_env_var_brace_syntax_not_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOO", "value")
    assert resolve_env_var("${FOO}") == "${FOO}"


def test_substitute_env_vars_in_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOO", "value")
    text = "hello $FOO and %FOO%"
    assert substitute_env_vars_in_text(text) == "hello value and value"


def test_substitute_env_vars_in_text_preserves_code_fences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOO", "value")
    text = "before $FOO\n```bash\necho $FOO\n```\nafter %FOO%"
    assert substitute_env_vars_in_text(text) == "before value\n```bash\necho $FOO\n```\nafter value"


def test_to_bool() -> None:
    assert _to_bool(True) is True
    assert _to_bool(False) is False
    assert _to_bool("true") is True
    assert _to_bool("false") is False
    assert _to_bool("1") is True
    assert _to_bool("0") is False
    assert _to_bool("yes") is True
    assert _to_bool("no") is False
    assert _to_bool("garbage", default=False) is False
