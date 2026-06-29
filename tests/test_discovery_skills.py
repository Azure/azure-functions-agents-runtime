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
    assert _has_includes("[file](./foo.md)")
    assert _has_includes("  [file](./path/to/file.md)  ")
    assert _has_includes("before\n[ref](./ref.md)\nafter")
    assert not _has_includes("no includes here")
    assert not _has_includes("[link](https://example.com)")  # external link
    assert not _has_includes("[link](other.md)")  # no ./ prefix


def test_resolve_includes_basic(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    refs_dir = skill_dir / "references"
    refs_dir.mkdir()
    (refs_dir / "api.md").write_text("# API Reference\nSome API docs.", encoding="utf-8")

    content = "# Skill\n\n[api.md](./references/api.md)\n\nMore content."
    result = _resolve_includes(content, skill_dir)

    assert "# API Reference" in result
    assert "Some API docs." in result
    assert "[api.md](./references/api.md)" not in result
    assert "More content." in result


def test_resolve_includes_multiple(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "part1.md").write_text("Part 1 content", encoding="utf-8")
    (skill_dir / "part2.md").write_text("Part 2 content", encoding="utf-8")

    content = "Header\n[part1](./part1.md)\nMiddle\n[part2](./part2.md)\nFooter"
    result = _resolve_includes(content, skill_dir)

    assert "Part 1 content" in result
    assert "Part 2 content" in result
    assert "Header" in result
    assert "Middle" in result
    assert "Footer" in result


def test_resolve_includes_nested(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "outer.md").write_text("Outer start\n[inner](./inner.md)\nOuter end", encoding="utf-8")
    (skill_dir / "inner.md").write_text("Inner content", encoding="utf-8")

    content = "Main\n[outer](./outer.md)\nDone"
    result = _resolve_includes(content, skill_dir)

    assert "Main" in result
    assert "Outer start" in result
    assert "Inner content" in result
    assert "Outer end" in result
    assert "Done" in result


def test_resolve_includes_circular_detection(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "a.md").write_text("A includes B\n[b](./b.md)", encoding="utf-8")
    (skill_dir / "b.md").write_text("B includes A\n[a](./a.md)", encoding="utf-8")

    content = "Start\n[a](./a.md)"
    with pytest.raises(ValueError, match="Circular include"):
        _resolve_includes(content, skill_dir)


def test_resolve_includes_self_reference(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "self.md").write_text("Self\n[self](./self.md)", encoding="utf-8")

    content = "[self](./self.md)"
    with pytest.raises(ValueError, match="Circular include"):
        _resolve_includes(content, skill_dir)


def test_resolve_includes_path_escape_rejected(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    # Create a file outside the skill directory
    (tmp_path / "secret.md").write_text("secret content", encoding="utf-8")

    content = "[secret](./../../secret.md)"
    with pytest.raises(ValueError, match="escapes the skill directory"):
        _resolve_includes(content, skill_dir)


def test_resolve_includes_file_not_found(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)

    content = "[missing](./nonexistent.md)"
    with pytest.raises(ValueError, match="Include file not found"):
        _resolve_includes(content, skill_dir)


def test_resolve_includes_empty_path_rejected(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)

    content = "[empty](./)"
    with pytest.raises(ValueError, match="Empty include path"):
        _resolve_includes(content, skill_dir)


def test_resolve_includes_preserves_non_include_content(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)

    # Content without includes should be unchanged
    content = "# Title\n\nSome content with ${ENV_VAR} and other stuff.\n"
    result = _resolve_includes(content, skill_dir)

    assert result == content


def test_resolve_includes_preserves_inline_links(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)

    # Inline links (not on their own line) should not be treated as includes
    content = "Check out [this file](./ref.md) for more info."
    result = _resolve_includes(content, skill_dir)

    # Link should be preserved since it's inline, not on its own line
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
        "---\nname: with-includes\ndescription: Test\n---\n\n# Skill\n[api.md](./references/api.md)\n",
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
    assert "[api.md](./references/api.md)" not in content


def test_prepare_resolved_skills_copies_other_files(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "with-assets"
    skill_dir.mkdir(parents=True)
    assets_dir = skill_dir / "assets"
    assets_dir.mkdir()
    (assets_dir / "example.py").write_text("print('hello')", encoding="utf-8")
    (skill_dir / "refs.md").write_text("Reference doc", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        "---\nname: with-assets\ndescription: Test\n---\n\n[refs](./refs.md)\n",
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
        "---\nname: complex\ndescription: Test\n---\n\n[ref](./ref.md)\n",
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


# ---------------------------------------------------------------------------
# Temp directory lifecycle tests
# ---------------------------------------------------------------------------


def test_temp_dir_created_only_when_needed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Temp directory is only created when a skill has includes."""
    from azure_functions_agents.discovery import skills as skills_module

    # Reset the global temp dir
    monkeypatch.setattr(skills_module, "_RESOLVED_SKILLS_TEMP_DIR", None)

    # Skill without includes
    simple_dir = _write_skill(tmp_path, "no-includes", "no-includes")
    skills = {"no-includes": simple_dir}

    resolved = prepare_resolved_skills(skills)

    # Should return original path and not create temp dir
    assert resolved["no-includes"] == simple_dir
    assert skills_module._RESOLVED_SKILLS_TEMP_DIR is None


def test_temp_dir_created_when_includes_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Temp directory is created when a skill has includes."""
    from azure_functions_agents.discovery import skills as skills_module

    # Reset the global temp dir
    monkeypatch.setattr(skills_module, "_RESOLVED_SKILLS_TEMP_DIR", None)

    # Skill with includes
    skill_dir = tmp_path / "skills" / "with-includes"
    skill_dir.mkdir(parents=True)
    (skill_dir / "ref.md").write_text("Reference content", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        "---\nname: with-includes\ndescription: Test\n---\n\n[ref](./ref.md)\n",
        encoding="utf-8",
    )
    skills = {"with-includes": skill_dir}

    resolved = prepare_resolved_skills(skills)

    # Should create temp dir
    assert resolved["with-includes"] != skill_dir
    assert skills_module._RESOLVED_SKILLS_TEMP_DIR is not None
    assert skills_module._RESOLVED_SKILLS_TEMP_DIR.exists()

    # Clean up
    skills_module._cleanup_resolved_skills_temp_dir()


def test_temp_dir_cleanup_removes_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup function removes the temp directory."""
    from azure_functions_agents.discovery import skills as skills_module

    # Reset and create temp dir
    monkeypatch.setattr(skills_module, "_RESOLVED_SKILLS_TEMP_DIR", None)

    skill_dir = tmp_path / "skills" / "test"
    skill_dir.mkdir(parents=True)
    (skill_dir / "ref.md").write_text("Content", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test\ndescription: Test\n---\n\n[ref](./ref.md)\n",
        encoding="utf-8",
    )

    prepare_resolved_skills({"test": skill_dir})

    temp_dir = skills_module._RESOLVED_SKILLS_TEMP_DIR
    assert temp_dir is not None
    assert temp_dir.exists()

    # Cleanup
    skills_module._cleanup_resolved_skills_temp_dir()

    assert not temp_dir.exists()
    assert skills_module._RESOLVED_SKILLS_TEMP_DIR is None


# ---------------------------------------------------------------------------
# Additional edge case tests for include syntax
# ---------------------------------------------------------------------------


def test_standalone_link_with_prefix_is_resolved(tmp_path: Path) -> None:
    """Markdown links on their own line with ./ prefix are resolved as includes."""
    skill_dir = tmp_path / "skills" / "test"
    skill_dir.mkdir(parents=True)
    (skill_dir / "api.md").write_text("API Documentation", encoding="utf-8")

    # Link on its own line with ./ prefix should be resolved
    content = "[api.md](./api.md)"
    result = _resolve_includes(content, skill_dir)

    assert result == "API Documentation"
    assert "[api.md](./api.md)" not in result


def test_standalone_link_with_whitespace_is_resolved(tmp_path: Path) -> None:
    """Links with leading/trailing whitespace on the line are still resolved."""
    skill_dir = tmp_path / "skills" / "test"
    skill_dir.mkdir(parents=True)
    (skill_dir / "api.md").write_text("API Documentation", encoding="utf-8")

    content = "  [api.md](./api.md)  "
    result = _resolve_includes(content, skill_dir)

    assert "API Documentation" in result


def test_inline_link_preserved_not_included(tmp_path: Path) -> None:
    """Inline links (within text) are preserved and not treated as includes."""
    skill_dir = tmp_path / "skills" / "test"
    skill_dir.mkdir(parents=True)
    # Create the file - but it should NOT be included since link is inline
    (skill_dir / "ref.md").write_text("REF CONTENT SHOULD NOT APPEAR", encoding="utf-8")

    # Link inline with other text - should NOT be resolved
    content = "See [this documentation](./ref.md) for more details."
    result = _resolve_includes(content, skill_dir)

    # Original link should remain unchanged
    assert result == content
    assert "REF CONTENT SHOULD NOT APPEAR" not in result


def test_link_without_dot_slash_prefix_preserved(tmp_path: Path) -> None:
    """Links without ./ prefix are not treated as includes."""
    skill_dir = tmp_path / "skills" / "test"
    skill_dir.mkdir(parents=True)
    (skill_dir / "ref.md").write_text("SHOULD NOT BE INCLUDED", encoding="utf-8")

    # Link without ./ prefix on its own line - should NOT be resolved
    content = "[ref](ref.md)"
    result = _resolve_includes(content, skill_dir)

    assert result == content
    assert "SHOULD NOT BE INCLUDED" not in result


def test_external_link_preserved(tmp_path: Path) -> None:
    """External URLs are never treated as includes."""
    skill_dir = tmp_path / "skills" / "test"
    skill_dir.mkdir(parents=True)

    content = "[docs](https://example.com/docs.md)"
    result = _resolve_includes(content, skill_dir)

    assert result == content


def test_deeply_nested_includes(tmp_path: Path) -> None:
    """Three levels of nesting: A includes B, B includes C."""
    skill_dir = tmp_path / "skills" / "test"
    skill_dir.mkdir(parents=True)

    (skill_dir / "level1.md").write_text(
        "LEVEL 1 START\n[level2](./level2.md)\nLEVEL 1 END", encoding="utf-8"
    )
    (skill_dir / "level2.md").write_text(
        "LEVEL 2 START\n[level3](./level3.md)\nLEVEL 2 END", encoding="utf-8"
    )
    (skill_dir / "level3.md").write_text("LEVEL 3 CONTENT", encoding="utf-8")

    content = "MAIN START\n[level1](./level1.md)\nMAIN END"
    result = _resolve_includes(content, skill_dir)

    assert "MAIN START" in result
    assert "LEVEL 1 START" in result
    assert "LEVEL 2 START" in result
    assert "LEVEL 3 CONTENT" in result
    assert "LEVEL 2 END" in result
    assert "LEVEL 1 END" in result
    assert "MAIN END" in result


def test_path_traversal_with_dots_rejected(tmp_path: Path) -> None:
    """Path traversal attempts using .. are rejected."""
    skill_dir = tmp_path / "skills" / "test"
    skill_dir.mkdir(parents=True)
    # Create a file outside skill directory
    (tmp_path / "outside.md").write_text("OUTSIDE CONTENT", encoding="utf-8")

    # Try various traversal patterns
    for path in [
        "./../outside.md",
        "./../../outside.md",
        "./sub/../../outside.md",
        "./sub/../../../outside.md",
    ]:
        content = f"[file]({path})"
        with pytest.raises(ValueError, match="escapes the skill directory"):
            _resolve_includes(content, skill_dir)


def test_missing_file_error_includes_filename(tmp_path: Path) -> None:
    """Error for missing file includes the filename for debugging."""
    skill_dir = tmp_path / "skills" / "test"
    skill_dir.mkdir(parents=True)

    content = "[missing](./nonexistent-file.md)"
    with pytest.raises(ValueError, match=r"nonexistent-file\.md"):
        _resolve_includes(content, skill_dir)
