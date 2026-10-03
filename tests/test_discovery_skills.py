from __future__ import annotations

import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from azure_functions_agents.discovery.skills import (
    SkillDescriptor,
    clear_skills_cache,
    describe_skill_catalog,
    describe_skill_paths,
    discover_skills,
)

SCOPED_SKILLS_FIXTURE = (
    Path(__file__).parent / "fixtures" / "config_scenarios" / "20_scoped_skills"
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

    result = discover_skills(tmp_path)

    assert result.skills == {"alpha": skill_dir.resolve()} or result.skills == {"alpha": skill_dir}
    assert result.failed_loads == []
    assert result.descriptors == (
        SkillDescriptor(name="alpha", description="Test skill", path=skill_dir.resolve()),
    )


def test_discover_skills_returns_empty_when_no_skills_dir(tmp_path: Path) -> None:
    result = discover_skills(tmp_path)
    assert result.skills == {}
    assert result.failed_loads == []
    assert result.descriptors == ()


def test_discover_skills_returns_empty_when_no_skill_files(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "README.md").write_text("not a skill", encoding="utf-8")

    result = discover_skills(tmp_path)
    assert result.skills == {}
    assert result.failed_loads == []
    assert result.descriptors == ()


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


def test_discover_skills_rejects_name_over_max_length(tmp_path: Path) -> None:
    # 65 lowercase letters — over MAF's 64-char cap. The regex alone would
    # accept this, so this test locks in the length check that mirrors
    # agent_framework._skills.MAX_NAME_LENGTH (and prevents MAF from silently
    # dropping the skill at runtime).
    long_name = "a" * 65
    _write_skill(tmp_path, "too-long", long_name)

    with pytest.raises(ValueError, match="at most 64 characters"):
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

    result = discover_skills(tmp_path)

    assert "good" in result.skills
    assert "broken" not in result.skills
    assert len(result.failed_loads) == 1
    assert "broken" in result.failed_loads[0][0]
    assert [skill.name for skill in result.descriptors] == ["good"]
    assert "Failed to parse skill frontmatter" in caplog.text


def test_skill_descriptor_factory_is_canonical_and_frozen(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "alpha", "alpha")

    descriptor = SkillDescriptor.create(
        name="alpha", description="Test skill", path=skill_dir / "."
    )

    assert descriptor.path == skill_dir.resolve()
    with pytest.raises(FrozenInstanceError):
        descriptor.description = "changed"


def test_discovery_cache_preserves_metadata_and_returns_mapping_copies(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "alpha", "alpha", "Original description")
    first = discover_skills(tmp_path)
    first.skills.clear()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Changed description\n---\n", encoding="utf-8"
    )

    cached = discover_skills(tmp_path)

    assert cached.skills == {"alpha": skill_dir.resolve()}
    assert cached.descriptors[0].description == "Original description"
    clear_skills_cache()
    assert discover_skills(tmp_path).descriptors[0].description == "Changed description"


@pytest.mark.parametrize(
    ("metadata", "description"),
    [
        ("name: alpha\n", ""),
        ("name: alpha\ndescription: null\n", ""),
        ("name: alpha\ndescription: false\n", ""),
        ("name: alpha\ndescription: 123\n", "123"),
        ('name: alpha\ndescription: "  Useful description  "\n', "Useful description"),
    ],
)
def test_discovery_does_not_tighten_description_validation(
    tmp_path: Path, metadata: str, description: str
) -> None:
    skill_dir = tmp_path / "skills" / "alpha"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"---\n{metadata}---\n", encoding="utf-8")

    result = discover_skills(tmp_path)

    assert result.skills == {"alpha": skill_dir.resolve()}
    assert result.descriptors[0].description == description
    assert describe_skill_paths([skill_dir]) == result.descriptors


def test_discovery_retains_nested_roots_as_ownership_metadata() -> None:
    result = discover_skills(SCOPED_SKILLS_FIXTURE)
    parent = SCOPED_SKILLS_FIXTURE / "skills" / "guide-parent"
    child = parent / "guide-child"

    assert result.skills == {"guide-parent": parent.resolve(), "guide-child": child.resolve()}
    assert result.descriptors == (
        SkillDescriptor(
            name="guide-child", description="Nested child skill guidance.", path=child.resolve()
        ),
        SkillDescriptor(
            name="guide-parent", description="Parent skill guidance.", path=parent.resolve()
        ),
    )


def test_describe_explicit_paths_reads_only_selected_skill_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = _write_skill(tmp_path, "parent", "parent", "Parent description")
    _write_skill(tmp_path, "parent/child", "child")
    (parent / "references").mkdir()
    (parent / "references" / "unparsed.md").write_text(
        "---\nname: [unclosed\n---\n", encoding="utf-8"
    )
    (parent / "scripts").mkdir()
    (parent / "scripts" / "not_imported.py").write_text(
        "raise RuntimeError('must not execute')\n", encoding="utf-8"
    )

    import frontmatter

    parsed: list[Path] = []
    original_load = frontmatter.load

    def capture_load(path: Path) -> object:
        parsed.append(path)
        return original_load(path)

    monkeypatch.setattr(frontmatter, "load", capture_load)

    descriptors = describe_skill_paths([parent])

    assert parsed == [parent / "SKILL.md"]
    assert descriptors == (
        SkillDescriptor(
            name="parent", description="Parent description", path=parent.resolve()
        ),
    )


def test_describe_explicit_paths_skips_malformed_and_missing_metadata(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    malformed = tmp_path / "bad"
    malformed.mkdir()
    (malformed / "SKILL.md").write_text("---\nname: [unclosed\n---\n", encoding="utf-8")
    good = _write_skill(tmp_path, "good", "good")

    descriptors = describe_skill_paths([malformed, tmp_path / "missing", good])

    assert [descriptor.name for descriptor in descriptors] == ["good"]
    assert caplog.text.count("Failed to parse skill frontmatter") == 2


def test_explicit_catalog_keeps_nested_ownership_outside_the_app_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir()
    parent = _write_skill(tmp_path / "external", "parent", "parent")
    child = _write_skill(tmp_path / "external", "parent/child", "child")

    import frontmatter

    parsed: list[Path] = []
    original_load = frontmatter.load

    def capture_load(path: Path) -> object:
        parsed.append(path)
        return original_load(path)

    monkeypatch.setattr(frontmatter, "load", capture_load)

    catalog = describe_skill_catalog([parent, child, parent / "."])

    assert discover_skills(app_root).descriptors == ()
    assert {descriptor.name: descriptor.path for descriptor in catalog} == {
        "parent": parent.resolve(),
        "child": child.resolve(),
    }
    assert sorted(parsed) == sorted([parent / "SKILL.md", child / "SKILL.md"])


def test_explicit_catalog_preserves_malformed_skips_and_duplicate_errors(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    parent = _write_skill(tmp_path, "parent", "parent")
    broken = parent / "broken"
    broken.mkdir()
    (broken / "SKILL.md").write_text("---\nname: [unclosed\n---\n", encoding="utf-8")

    assert [descriptor.name for descriptor in describe_skill_catalog([parent])] == ["parent"]
    assert "Failed to parse skill frontmatter" in caplog.text

    _write_skill(tmp_path, "parent/duplicate", "parent")
    with pytest.raises(ValueError, match="Duplicate skill name"):
        describe_skill_catalog([parent])


def test_explicit_catalog_does_not_hide_distinct_roots_sharing_a_canonical_file(
    tmp_path: Path,
) -> None:
    first = _write_skill(tmp_path, "first", "shared")
    second = tmp_path / "skills" / "second"
    second.mkdir()
    try:
        (second / "SKILL.md").symlink_to(first / "SKILL.md")
    except OSError as exc:
        if os.name == "nt" and exc.winerror in {5, 1314}:
            pytest.skip("Windows account cannot create symlinks")
        raise

    with pytest.raises(ValueError, match="Duplicate skill name"):
        describe_skill_catalog([first, second])


@pytest.mark.parametrize(
    ("name", "error"),
    [
        ("", "missing a 'name' field"),
        ("Bad_Name", "is invalid"),
        ("a" * 65, "at most 64 characters"),
    ],
)
def test_describe_explicit_paths_preserves_name_validation(
    tmp_path: Path, name: str, error: str
) -> None:
    skill_dir = _write_skill(tmp_path, "invalid", name)

    with pytest.raises(ValueError, match=error):
        describe_skill_paths([skill_dir])


def test_describe_explicit_paths_preserves_duplicate_name_errors(tmp_path: Path) -> None:
    first = _write_skill(tmp_path, "first", "shared")
    second = _write_skill(tmp_path, "second", "shared")

    with pytest.raises(ValueError, match="Duplicate skill name"):
        describe_skill_paths([first, second])
