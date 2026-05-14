from __future__ import annotations

from pathlib import Path

import pytest

from azure_functions_agents.config import paths


def test_get_app_root_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    paths._app_root = None
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_APP_ROOT", str(tmp_path))
    monkeypatch.delenv("AzureWebJobsScriptRoot", raising=False)
    assert paths.get_app_root() == tmp_path.resolve()


def test_set_app_root_wins_over_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    explicit = tmp_path / "explicit"
    env_root = tmp_path / "env"
    explicit.mkdir()
    env_root.mkdir()
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_APP_ROOT", str(env_root))
    paths._app_root = None
    paths.set_app_root(explicit)
    assert paths.get_app_root() == explicit.resolve()
    paths._app_root = None


def test_get_app_root_azure_webjobs_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths._app_root = None
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_APP_ROOT", raising=False)
    monkeypatch.setenv("AzureWebJobsScriptRoot", str(tmp_path))
    assert paths.get_app_root() == tmp_path.resolve()


def test_get_app_root_cwd_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    paths._app_root = None
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_APP_ROOT", raising=False)
    monkeypatch.delenv("AzureWebJobsScriptRoot", raising=False)
    monkeypatch.chdir(tmp_path)
    assert paths.get_app_root() == tmp_path.resolve()


def test_resolve_config_dir_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_CONFIG_DIR", r"C:\config\preferred")
    monkeypatch.setenv("CODE_ASSISTANT_CONFIG_PATH", r"C:\config\legacy")
    monkeypatch.setenv("CONTAINER_NAME", "container")
    assert paths.resolve_config_dir() == r"C:\config\preferred"

    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_CONFIG_DIR", raising=False)
    assert paths.resolve_config_dir() == r"C:\config\legacy"

    monkeypatch.delenv("CODE_ASSISTANT_CONFIG_PATH", raising=False)
    assert paths.resolve_config_dir() == paths._REMOTE_CONFIG_DIR

    monkeypatch.delenv("CONTAINER_NAME", raising=False)
    assert paths.resolve_config_dir() is None
