from __future__ import annotations

from pathlib import Path

import pytest

from azure_functions_agents.discovery.skills import (
    _has_includes,
    _resolve_includes,
    clear_skills_cache,
    discover_skills,
    prepare_resolved_skills,
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

    discovered = discover_skills(tmp_path)

    assert "good" in discovered
    assert "broken" not in discovered


# ---------------------------------------------------------------------------
# Include resolution tests
# ---------------------------------------------------------------------------


def test_has_includes_detects_include_directive() -> None:
    assert _has_includes("{{include:foo.md}}")
    assert _has_includes("  {{include:path/to/file.md}}  ")
    assert _has_includes("before\n{{include:ref.md}}\nafter")
    assert not _has_includes("no includes here")
    assert not _has_includes("{{ include:foo.md }}")  # space after opening braces


def test_resolve_includes_basic(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    refs_dir = skill_dir / "references"
    refs_dir.mkdir()
    (refs_dir / "api.md").write_text("# API Reference\nSome API docs.", encoding="utf-8")

    content = "# Skill\n\n{{include:references/api.md}}\n\nMore content."
    result = _resolve_includes(content, skill_dir)

    assert "# API Reference" in result
    assert "Some API docs." in result
    assert "{{include:" not in result
    assert "More content." in result


def test_resolve_includes_multiple(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "part1.md").write_text("Part 1 content", encoding="utf-8")
    (skill_dir / "part2.md").write_text("Part 2 content", encoding="utf-8")

    content = "Header\n{{include:part1.md}}\nMiddle\n{{include:part2.md}}\nFooter"
    result = _resolve_includes(content, skill_dir)

    assert "Part 1 content" in result
    assert "Part 2 content" in result
    assert "Header" in result
    assert "Middle" in result
    assert "Footer" in result


def test_resolve_includes_nested(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "outer.md").write_text("Outer start\n{{include:inner.md}}\nOuter end", encoding="utf-8")
    (skill_dir / "inner.md").write_text("Inner content", encoding="utf-8")

    content = "Main\n{{include:outer.md}}\nDone"
    result = _resolve_includes(content, skill_dir)

    assert "Main" in result
    assert "Outer start" in result
    assert "Inner content" in result
    assert "Outer end" in result
    assert "Done" in result


def test_resolve_includes_circular_detection(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "a.md").write_text("A includes B\n{{include:b.md}}", encoding="utf-8")
    (skill_dir / "b.md").write_text("B includes A\n{{include:a.md}}", encoding="utf-8")

    content = "Start\n{{include:a.md}}"
    with pytest.raises(ValueError, match="Circular include"):
        _resolve_includes(content, skill_dir)


def test_resolve_includes_self_reference(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "self.md").write_text("Self\n{{include:self.md}}", encoding="utf-8")

    content = "{{include:self.md}}"
    with pytest.raises(ValueError, match="Circular include"):
        _resolve_includes(content, skill_dir)


def test_resolve_includes_path_escape_rejected(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    # Create a file outside the skill directory
    (tmp_path / "secret.md").write_text("secret content", encoding="utf-8")

    content = "{{include:../../secret.md}}"
    with pytest.raises(ValueError, match="escapes the skill directory"):
        _resolve_includes(content, skill_dir)


def test_resolve_includes_absolute_path_rejected(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)

    content = "{{include:/etc/passwd}}"
    with pytest.raises(ValueError, match="must be relative"):
        _resolve_includes(content, skill_dir)


def test_resolve_includes_file_not_found(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)

    content = "{{include:nonexistent.md}}"
    with pytest.raises(ValueError, match="Include file not found"):
        _resolve_includes(content, skill_dir)


def test_resolve_includes_empty_path_rejected(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)

    content = "{{include:}}"
    with pytest.raises(ValueError, match="Empty include path"):
        _resolve_includes(content, skill_dir)


def test_resolve_includes_preserves_non_include_content(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)

    # Content without includes should be unchanged
    content = "# Title\n\nSome content with ${ENV_VAR} and other stuff.\n"
    result = _resolve_includes(content, skill_dir)

    assert result == content


# ---------------------------------------------------------------------------
# prepare_resolved_skills tests
# ---------------------------------------------------------------------------


def _write_skill_with_content(
    app_root: Path, dir_name: str, name: str, body: str
) -> Path:
    skill_dir = app_root / "skills" / dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test skill\n---\n\n{body}",
        encoding="utf-8",
    )
    return skill_dir


def test_prepare_resolved_skills_no_includes_returns_original_path(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "simple", "simple")
    skills = {"simple": skill_dir}

    resolved = prepare_resolved_skills(skills)

    # Without includes, should return original path
    assert resolved["simple"] == skill_dir


def test_prepare_resolved_skills_with_includes_returns_temp_path(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "with-includes"
    skill_dir.mkdir(parents=True)
    refs_dir = skill_dir / "references"
    refs_dir.mkdir()
    (refs_dir / "api.md").write_text("API content", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        "---\nname: with-includes\ndescription: Test\n---\n\n# Skill\n{{include:references/api.md}}\n",
        encoding="utf-8",
    )
    skills = {"with-includes": skill_dir}

    resolved = prepare_resolved_skills(skills)

    # With includes, should return a different (temp) path
    assert resolved["with-includes"] != skill_dir
    # The temp path should contain the resolved content
    resolved_skill_file = resolved["with-includes"] / "SKILL.md"
    content = resolved_skill_file.read_text(encoding="utf-8")
    assert "API content" in content
    assert "{{include:" not in content


def test_prepare_resolved_skills_copies_other_files(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "with-assets"
    skill_dir.mkdir(parents=True)
    assets_dir = skill_dir / "assets"
    assets_dir.mkdir()
    (assets_dir / "example.py").write_text("print('hello')", encoding="utf-8")
    (skill_dir / "refs.md").write_text("Reference doc", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        "---\nname: with-assets\ndescription: Test\n---\n\n{{include:refs.md}}\n",
        encoding="utf-8",
    )
    skills = {"with-assets": skill_dir}

    resolved = prepare_resolved_skills(skills)

    # Check that other files were copied
    resolved_assets = resolved["with-assets"] / "assets" / "example.py"
    assert resolved_assets.exists()
    assert resolved_assets.read_text(encoding="utf-8") == "print('hello')"


def test_prepare_resolved_skills_empty_input_returns_empty(tmp_path: Path) -> None:
    assert prepare_resolved_skills({}) == {}


def test_prepare_resolved_skills_mixed_skills(tmp_path: Path) -> None:
    # One skill with includes, one without
    simple_dir = _write_skill(tmp_path, "simple", "simple")

    complex_dir = tmp_path / "skills" / "complex"
    complex_dir.mkdir(parents=True)
    (complex_dir / "ref.md").write_text("Referenced content", encoding="utf-8")
    (complex_dir / "SKILL.md").write_text(
        "---\nname: complex\ndescription: Test\n---\n\n{{include:ref.md}}\n",
        encoding="utf-8",
    )

    skills = {"simple": simple_dir, "complex": complex_dir}
    resolved = prepare_resolved_skills(skills)

    # Simple skill should keep original path
    assert resolved["simple"] == simple_dir
    # Complex skill should get temp path
    assert resolved["complex"] != complex_dir
    content = (resolved["complex"] / "SKILL.md").read_text(encoding="utf-8")
    assert "Referenced content" in content
