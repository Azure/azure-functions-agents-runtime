from __future__ import annotations

import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest
from agent_framework import FileSkillsSource, SkillsSourceContext

from azure_functions_agents.discovery.skills import (
    SkillDescriptor,
    clear_skills_cache,
    describe_skill_catalog,
    describe_skill_paths,
    discover_skills,
)

BOUNDARY_FIXTURE = (
    Path(__file__).parent / "fixtures" / "config_scenarios" / "21_skill_root_boundaries"
)


@pytest.fixture(autouse=True)
def clear_discovery_cache():
    clear_skills_cache()
    yield
    clear_skills_cache()


def _write_skill(root: Path, relative: str, name: str, description: str = "Test skill") -> Path:
    directory = root / "skills" / relative
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return directory


async def _native_skills(paths: list[Path]):
    return await FileSkillsSource(paths).get_skills(SkillsSourceContext(agent=Mock()))


@pytest.mark.asyncio
async def test_shared_app_candidates_match_public_maf_root_boundaries() -> None:
    native = await _native_skills([BOUNDARY_FIXTURE / "skills"])
    result = discover_skills(BOUNDARY_FIXTURE)

    assert set(result.skills) == {"parent", "grouped", "excluded"}
    assert [skill.name for skill in result.descriptors] == [
        skill.frontmatter.name for skill in native
    ]
    assert all(not hasattr(skill, "description") for skill in result.descriptors)


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", [0, 1, 2, 3, 4])
async def test_shared_search_matches_public_maf_depth_and_explicit_roots(
    tmp_path: Path, depth: int
) -> None:
    relative = "/".join([*(["group"] * (depth - 1)), "selected"]) if depth else ""
    name = "selected" if depth else "skills"
    skill = _write_skill(tmp_path, relative, name)
    root = tmp_path / "skills"
    native = await _native_skills([root])

    assert [item.name for item in describe_skill_catalog([root])] == [
        item.frontmatter.name for item in native
    ]
    assert len(native) == (1 if depth <= 2 else 0)
    assert [item.name for item in describe_skill_catalog([skill])] == [name]
    assert [item.frontmatter.name for item in await _native_skills([skill])] == [name]


@pytest.mark.asyncio
async def test_explicit_overlapping_roots_preserve_independent_input_order(tmp_path: Path) -> None:
    parent = _write_skill(tmp_path, "parent", "parent")
    child = _write_skill(tmp_path, "parent/child", "child")

    for paths in ([parent], [parent, child], [child, parent]):
        native = await _native_skills(paths)
        assert [item.name for item in describe_skill_catalog(paths)] == [
            item.frontmatter.name for item in native
        ]
    assert describe_skill_catalog([parent, child, parent]) == describe_skill_catalog([parent, child])


@pytest.mark.asyncio
async def test_shared_candidates_skip_missing_files_and_unreadable_groups_like_maf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    good = _write_skill(tmp_path, "good", "good")
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    regular_file = tmp_path / "file.txt"
    regular_file.write_text("not a directory", encoding="utf-8")
    original = Path.iterdir

    def iterdir(path: Path):
        if path == blocked:
            raise PermissionError("test directory is unreadable")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    paths = [tmp_path / "missing", regular_file, blocked, good]
    assert [item.name for item in describe_skill_catalog(paths)] == [
        item.frontmatter.name for item in await _native_skills(paths)
    ]
    assert "Failed to scan skill directory" in caplog.text


@pytest.mark.parametrize(
    "content",
    [
        "",
        "---\ndescription: Missing name\n---\n",
        "---\nname: Bad_Name\ndescription: Invalid name\n---\n",
        "---\nname: different\ndescription: Wrong directory\n---\n",
        "---\nname: alpha\n---\n",
        "---\nname: alpha\ndescription: null\n---\n",
        "---\nNAME: alpha\nDESCRIPTION: Case-insensitive keys\n---\n",
        "---\nname: alpha\ndescription: [unclosed\n---\n",
        "---\nname: alpha\ndescription: " + "x" * 1025 + "\n---\n",
    ],
)
def test_discovery_never_parses_sdk_owned_metadata(
    tmp_path: Path, content: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _write_skill(tmp_path, "alpha", "alpha")
    (directory / "SKILL.md").write_text(content, encoding="utf-8")
    no_read = Mock(side_effect=AssertionError("Discovery must not read skill content"))
    monkeypatch.setattr(Path, "read_text", no_read)
    monkeypatch.setattr(Path, "open", no_read)
    monkeypatch.setattr("builtins.open", no_read)
    monkeypatch.setitem(sys.modules, "agent_framework", ModuleType("agent_framework"))

    expected = (SkillDescriptor(name="alpha", path=directory.resolve()),)
    assert discover_skills(tmp_path).descriptors == expected
    assert describe_skill_paths([directory]) == expected
    assert describe_skill_catalog([directory]) == expected
    no_read.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_root_remains_a_boundary_but_only_sdk_rejects_its_content(tmp_path: Path) -> None:
    parent = _write_skill(tmp_path, "parent", "parent")
    _write_skill(tmp_path, "parent/child", "child")
    (parent / "SKILL.md").write_text("---\nname: different\ndescription: Invalid\n---\n", encoding="utf-8")

    assert discover_skills(tmp_path).skills == {"parent": parent.resolve()}
    assert await _native_skills([parent]) == []
    assert discover_skills(tmp_path).failed_loads == []


@pytest.mark.asyncio
async def test_same_name_candidates_reach_sdk_for_first_valid_selection(tmp_path: Path) -> None:
    first = _write_skill(tmp_path, "first/shared", "shared", "First")
    second = _write_skill(tmp_path, "second/shared", "shared", "Second")
    (first / "SKILL.md").write_text("---\nname: wrong\ndescription: Invalid\n---\n", encoding="utf-8")
    candidates = describe_skill_catalog([first, second])

    assert [item.path for item in candidates] == [first.resolve(), second.resolve()]
    assert len(describe_skill_paths([first, second])) == 2
    native = await _native_skills([item.path for item in candidates])
    assert [item.frontmatter.description for item in native] == ["Second"]


@pytest.mark.parametrize("directory_name", ["Bad_Name", "invalid--name", "a" * 65])
def test_candidate_directory_names_are_not_host_validated(tmp_path: Path, directory_name: str) -> None:
    directory = _write_skill(tmp_path, directory_name, directory_name)

    assert discover_skills(tmp_path).skills == {directory_name: directory.resolve()}


@pytest.mark.parametrize("skills_directory", ["skills", "Skills"])
def test_discovery_maps_canonical_paths_and_is_case_compatible(
    tmp_path: Path, skills_directory: str
) -> None:
    directory = tmp_path / skills_directory / "alpha"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("SDK-owned content", encoding="utf-8")
    result = discover_skills(tmp_path)

    assert result.skills == {"alpha": directory.resolve()}
    assert result.failed_loads == []
    assert result.descriptors == (SkillDescriptor("alpha", directory.resolve()),)


def test_discovery_returns_empty_without_skill_roots(tmp_path: Path) -> None:
    assert discover_skills(tmp_path).descriptors == ()
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "README.md").write_text("not a skill", encoding="utf-8")
    clear_skills_cache()
    assert discover_skills(tmp_path).skills == {}
    assert describe_skill_paths([tmp_path / "missing"]) == ()


def test_cache_preserves_inventory_and_returns_independent_maps(tmp_path: Path) -> None:
    alpha = _write_skill(tmp_path, "alpha", "alpha")
    first = discover_skills(tmp_path)
    first.skills.clear()
    beta = _write_skill(tmp_path, "beta", "beta")

    assert discover_skills(tmp_path / ".").skills == {"alpha": alpha.resolve()}
    clear_skills_cache()
    assert discover_skills(tmp_path).skills == {"alpha": alpha.resolve(), "beta": beta.resolve()}


def test_cache_preserves_same_name_candidates_and_first_map_entry(tmp_path: Path) -> None:
    _write_skill(tmp_path, "first/shared", "shared")
    _write_skill(tmp_path, "second/shared", "shared")
    first = discover_skills(tmp_path)
    cached = discover_skills(tmp_path)

    assert len(first.descriptors) == 2
    assert cached.descriptors == first.descriptors
    assert first.skills == cached.skills == {"shared": first.descriptors[0].path}


def test_individual_paths_do_not_expand_collections_but_catalog_does(tmp_path: Path) -> None:
    directory = _write_skill(tmp_path, "group/alpha", "alpha")
    group = directory.parent

    assert describe_skill_paths([group]) == ()
    assert describe_skill_catalog([group]) == (SkillDescriptor("alpha", directory.resolve()),)


def test_skill_descriptor_factory_is_canonical_and_frozen(tmp_path: Path) -> None:
    descriptor = SkillDescriptor.create(name="alpha", path=tmp_path / ".")

    assert descriptor.path == tmp_path.resolve()
    with pytest.raises(FrozenInstanceError):
        descriptor.name = "changed"
