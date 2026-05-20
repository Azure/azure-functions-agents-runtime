from __future__ import annotations

from pathlib import Path

import pytest

from azure_functions_agents.discovery.skills import (
    clear_skills_cache,
    discover_skills,
)


@pytest.fixture(autouse=True)
def clear_discovery_cache() -> None:
    clear_skills_cache()
    yield
    clear_skills_cache()


def _write_skill(app_root: Path, dir_name: str, name: str, description: str = "Test skill") -> Path:
    skill_dir = app_root / "skills" / dir_name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return skill_dir


def test_discover_skills_returns_name_to_directory_map(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "alpha", "alpha")

    discovered = discover_skills(tmp_path)

    assert discovered == {"alpha": skill_dir.resolve()} or discovered == {"alpha": skill_dir}


def test_discover_skills_returns_empty_when_no_skills_dir(tmp_path: Path) -> None:
    assert discover_skills(tmp_path) == {}


def test_discover_skills_returns_empty_when_no_skill_files(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "README.md").write_text("not a skill", encoding="utf-8")

    assert discover_skills(tmp_path) == {}


def test_discover_skills_caches_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_skill(tmp_path, "alpha", "alpha")

    import frontmatter

    parse_count = 0
    original_load = frontmatter.load

    def counting_load(*args: object, **kwargs: object) -> object:
        nonlocal parse_count
        parse_count += 1
        return original_load(*args, **kwargs)

    monkeypatch.setattr(frontmatter, "load", counting_load)

    discover_skills(tmp_path)
    discover_skills(tmp_path / ".")

    assert parse_count == 1


def test_clear_skills_cache_reruns_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_skill(tmp_path, "alpha", "alpha")

    import frontmatter

    parse_count = 0
    original_load = frontmatter.load

    def counting_load(*args: object, **kwargs: object) -> object:
        nonlocal parse_count
        parse_count += 1
        return original_load(*args, **kwargs)

    monkeypatch.setattr(frontmatter, "load", counting_load)

    discover_skills(tmp_path)
    clear_skills_cache()
    discover_skills(tmp_path)

    assert parse_count == 2


def test_discover_skills_rejects_missing_name(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills" / "broken"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        "---\ndescription: missing name\n---\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing a 'name' field"):
        discover_skills(tmp_path)


def test_discover_skills_rejects_invalid_name(tmp_path: Path) -> None:
    _write_skill(tmp_path, "bad", "Bad_Name")

    with pytest.raises(ValueError, match="is invalid"):
        discover_skills(tmp_path)


def test_discover_skills_rejects_duplicate_names(tmp_path: Path) -> None:
    _write_skill(tmp_path, "first", "shared")
    _write_skill(tmp_path, "second", "shared")

    with pytest.raises(ValueError, match="Duplicate skill name"):
        discover_skills(tmp_path)


def test_discover_skills_skips_unparseable_frontmatter(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bad_dir = tmp_path / "skills" / "broken"
    bad_dir.mkdir(parents=True)
    # Intentionally malformed YAML in the frontmatter block.
    (bad_dir / "SKILL.md").write_text(
        "---\nname: [unclosed\n---\nbody\n",
        encoding="utf-8",
    )
    _write_skill(tmp_path, "good", "good")

    discovered = discover_skills(tmp_path)

    assert "good" in discovered
    assert "broken" not in discovered
