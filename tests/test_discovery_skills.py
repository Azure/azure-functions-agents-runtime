from __future__ import annotations

from pathlib import Path

import pytest

from azure_functions_agents.discovery.skills import (
    clear_skills_cache,
    discover_skill_texts,
    discover_skills,
)


@pytest.fixture(autouse=True)
def clear_discovery_cache() -> None:
    clear_skills_cache()
    yield
    clear_skills_cache()


def _write_skill(app_root: Path, name: str, content: str) -> None:
    skills_dir = app_root / "skills"
    skills_dir.mkdir()
    (skills_dir / name).write_text(content, encoding="utf-8")


def test_discover_skills_caches_by_resolved_app_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_skill(tmp_path, "alpha.md", "# Alpha\n")

    target_path = (tmp_path / "skills" / "alpha.md").resolve()
    read_count = 0
    original_read_text = Path.read_text

    def counting_read_text(self: Path, *args: object, **kwargs: object) -> str:
        nonlocal read_count
        if self.resolve() == target_path:
            read_count += 1
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    first = discover_skills(tmp_path)
    second = discover_skills(tmp_path / ".")

    assert first == second
    assert "## skill: alpha.md" in first
    assert read_count == 1


def test_discover_skill_texts_returns_independent_dicts(tmp_path: Path) -> None:
    _write_skill(tmp_path, "alpha.md", "# Alpha\n")

    discovered_skills = discover_skill_texts(tmp_path)
    discovered_skills["extra.md"] = "ignored"

    subsequent_skills = discover_skill_texts(tmp_path)

    assert subsequent_skills == {"alpha.md": "# Alpha\n"}


def test_clear_skills_cache_reruns_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_skill(tmp_path, "alpha.md", "# Alpha\n")

    target_path = (tmp_path / "skills" / "alpha.md").resolve()
    read_count = 0
    original_read_text = Path.read_text

    def counting_read_text(self: Path, *args: object, **kwargs: object) -> str:
        nonlocal read_count
        if self.resolve() == target_path:
            read_count += 1
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    discover_skills(tmp_path)
    clear_skills_cache()
    discover_skills(tmp_path)

    assert read_count == 2
