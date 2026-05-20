from __future__ import annotations

import pytest

from azure_functions_agents.config.env import (
    _to_bool,
    has_unresolved_placeholders,
    resolve_env_vars_in_data,
    substitute_env_vars_in_text,
    substitute_env_vars_in_value,
)


def test_substitute_env_vars_in_value_dollar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOO", "value")
    assert substitute_env_vars_in_value("$FOO") == "value"
    monkeypatch.setenv("FOO", "$FOO")
    assert substitute_env_vars_in_value("$FOO") == "$FOO"


def test_substitute_env_vars_in_value_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOO", "value")
    assert substitute_env_vars_in_value("%FOO%") == "value"
    monkeypatch.setenv("FOO", "%FOO%")
    assert substitute_env_vars_in_value("%FOO%") == "%FOO%"


def test_substitute_env_vars_in_value_rejects_invalid_var_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("1 BAD-NAME", "value")
    assert substitute_env_vars_in_value("%1 BAD-NAME%") == "%1 BAD-NAME%"


def test_substitute_env_vars_in_value_accepts_valid_var_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOO", "bar")
    assert substitute_env_vars_in_value("%FOO%") == "bar"


def test_substitute_env_vars_in_value_plain() -> None:
    assert substitute_env_vars_in_value("plain") == "plain"


def test_substitute_env_vars_in_value_brace_syntax_not_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOO", "value")
    assert substitute_env_vars_in_value("${FOO}") == "${FOO}"


def test_substitute_env_vars_in_value_inline_dollar_with_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOKEN", "secret")
    assert substitute_env_vars_in_value("Bearer $TOKEN") == "Bearer secret"


def test_substitute_env_vars_in_value_inline_percent_with_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOO", "value")
    assert substitute_env_vars_in_value("%FOO%-suffix") == "value-suffix"


def test_substitute_env_vars_in_value_url_with_inline_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOST", "example.com")
    assert substitute_env_vars_in_value("https://$HOST/api") == "https://example.com/api"


def test_has_unresolved_placeholders_url_with_inline_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HOST", raising=False)
    assert has_unresolved_placeholders("https://$HOST/api") is True

    monkeypatch.setenv("HOST", "example.com")
    resolved = substitute_env_vars_in_value("https://$HOST/api")
    assert has_unresolved_placeholders(resolved) is False


def test_has_unresolved_placeholders_plain_url() -> None:
    assert has_unresolved_placeholders("https://example.com") is False


def test_substitute_env_vars_in_value_same_var_multiple_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOST", "example.com")
    assert (
        substitute_env_vars_in_value("$HOST and $HOST")
        == "example.com and example.com"
    )


def test_substitute_env_vars_in_value_mixed_dollar_and_percent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A", "x")
    monkeypatch.setenv("B", "y")
    assert substitute_env_vars_in_value("$A and %B%") == "x and y"


def test_substitute_env_vars_in_value_trailing_literal_dollar_stays_literal() -> None:
    assert substitute_env_vars_in_value("trailing $") == "trailing $"


def test_substitute_env_vars_in_value_inline_digit_start_identifier_stays_literal() -> None:
    assert substitute_env_vars_in_value("port $1FOO") == "port $1FOO"


def test_substitute_env_vars_in_value_empty_env_var_resolves_to_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMPTY", "")
    assert substitute_env_vars_in_value("$EMPTY") == ""


def test_substitute_env_vars_in_value_undefined_inline_stays_literal() -> None:
    assert substitute_env_vars_in_value("Bearer $MISSING") == "Bearer $MISSING"


def test_substitute_env_vars_in_value_partial_identifier_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAR", "hello")
    assert substitute_env_vars_in_value("$VAR-NAME") == "hello-NAME"

    monkeypatch.delenv("VAR", raising=False)
    assert substitute_env_vars_in_value("$VAR-NAME") == "$VAR-NAME"


def test_substitute_env_vars_in_value_percent_partial_identifier_stays_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAR", "hello")
    assert substitute_env_vars_in_value("%VAR-NAME%") == "%VAR-NAME%"

    monkeypatch.delenv("VAR", raising=False)
    assert substitute_env_vars_in_value("%VAR-NAME%") == "%VAR-NAME%"


def test_resolve_env_vars_in_data_nested_dict_and_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOO", "resolved")
    value = {"a": "$FOO", "b": ["literal", "%FOO%-x"], "c": {"d": "$FOO/path"}}
    assert resolve_env_vars_in_data(value) == {
        "a": "resolved",
        "b": ["literal", "resolved-x"],
        "c": {"d": "resolved/path"},
    }


def test_resolve_env_vars_in_data_does_not_substitute_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYNAME", "substituted")
    assert resolve_env_vars_in_data({"$KEYNAME": "value"}) == {"$KEYNAME": "value"}


def test_resolve_env_vars_in_data_passes_through_non_string_scalars() -> None:
    value = {"int": 42, "bool": True, "none": None, "float": 3.14}
    assert resolve_env_vars_in_data(value) == value


def test_resolve_env_vars_in_data_string_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOO", "value")
    assert resolve_env_vars_in_data("$FOO") == "value"


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
