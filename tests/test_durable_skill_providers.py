from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

import azure_functions_agents.experimental.durable_skill_providers as providers
from azure_functions_agents.discovery.skills import (
    SkillDiscoveryResult,
    clear_skills_cache,
)
from azure_functions_agents.experimental.durable_skill_providers import (
    MAX_DURABLE_SKILL_CONTENT_BYTES,
    MAX_DURABLE_SKILL_METADATA_BYTES,
    MAX_DURABLE_SKILL_REFERENCE_FILES,
    MAX_DURABLE_SKILLS,
    DurableSkillCatalogLimitError,
    DurableSkillCursorError,
    DurableSkillIntegrityError,
    DurableSkillMetadataV1,
    DurableSkillNotFoundError,
    DurableSkillProvider,
    DurableSkillSnapshotUnavailableError,
    DurableSkillValidationError,
    DurableSkillVersionUnavailableError,
    PackagedFilesystemDurableSkillProvider,
)


@pytest.fixture(autouse=True)
def clear_discovery_state() -> None:
    clear_skills_cache()
    yield
    clear_skills_cache()


def _retain_until() -> datetime:
    return datetime.now(UTC) + timedelta(days=1)


def _write_skill(
    app_root: Path,
    skill_id: str,
    *,
    description: str = "Use this skill for deterministic testing.",
    body: str = "# Instructions\n\nFollow the durable instructions.\n",
    display_name: str | None = None,
    tags: tuple[str, ...] = (),
    extra_frontmatter: str = "",
) -> Path:
    skill_dir = app_root / "skills" / skill_id
    skill_dir.mkdir(parents=True)
    display_line = f"display_name: {display_name}\n" if display_name is not None else ""
    tag_lines = "tags:\n" + "".join(f"  - {tag}\n" for tag in tags) if tags else ""
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {skill_id}\n"
        f"description: {description}\n"
        f"{display_line}"
        f"{tag_lines}"
        f"{extra_frontmatter}"
        "---\n\n"
        f"{body}",
        encoding="utf-8",
    )
    return skill_dir


def _write_reference(skill_dir: Path, relative_path: str, content: str) -> Path:
    path = skill_dir / "references" / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_packaged_provider_captures_strict_model_safe_snapshot(tmp_path: Path) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "api-guide",
        display_name="API Guide",
        tags=("api", "reference"),
        body="# API Guide\n\n[details](./references/details.md)\n",
        extra_frontmatter=(
            "provider_id: must-not-be-model-visible\ncredentials: must-not-be-model-visible\n"
        ),
    )
    _write_reference(skill_dir, "details.md", "Use only documented API operations.\n")
    _write_reference(skill_dir, "nested/config.json", '{"mode":"safe"}\n')
    provider = PackagedFilesystemDurableSkillProvider(tmp_path)

    assert isinstance(provider, DurableSkillProvider)
    snapshot = await provider.open_snapshot(
        agent_slug="main-agent",
        retain_until=_retain_until(),
    )

    assert snapshot.provider_id == "packaged-filesystem"
    assert len(snapshot.catalog_revision) == len(snapshot.catalog_hash) == 64
    assert tuple(item.skill_id for item in snapshot.metadata) == ("api-guide",)
    metadata = snapshot.metadata[0]
    assert metadata.display_name == "API Guide"
    assert metadata.tags == ("api", "reference")
    assert metadata.executable is False
    assert set(metadata.model_dump()) == {
        "schema_version",
        "skill_id",
        "display_name",
        "selection_description",
        "version",
        "content_hash",
        "tags",
        "executable",
    }
    assert "provider" not in metadata.model_dump_json()
    assert "credential" not in metadata.model_dump_json()

    content = await provider.get_content(
        snapshot=snapshot,
        skill_id=metadata.skill_id,
        version=metadata.version,
        expected_hash=metadata.content_hash,
    )
    assert "Use only documented API operations." in content.files[0].content
    assert "[details]" not in content.files[0].content
    assert tuple(reference.relative_path for reference in content.files[1:]) == (
        "references/details.md",
        "references/nested/config.json",
    )
    assert content.version == content.content_hash == metadata.content_hash
    assert "must-not-be-model-visible" not in content.files[0].content


@pytest.mark.asyncio
async def test_snapshot_content_is_immutable_after_packaged_files_change(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "stable")
    provider = PackagedFilesystemDurableSkillProvider(tmp_path)
    first = await provider.open_snapshot(agent_slug="agent", retain_until=_retain_until())
    first_metadata = first.metadata[0]

    (skill_dir / "SKILL.md").write_text(
        "---\nname: stable\ndescription: Updated selection text.\n---\n\nUpdated instructions.\n",
        encoding="utf-8",
    )
    second = await provider.open_snapshot(agent_slug="agent", retain_until=_retain_until())
    old_content = await provider.get_content(
        snapshot=first,
        skill_id="stable",
        version=first_metadata.version,
        expected_hash=first_metadata.content_hash,
    )

    assert old_content.files[0].content.endswith("Follow the durable instructions.")
    assert second.catalog_hash != first.catalog_hash
    assert second.metadata[0].version != first_metadata.version


@pytest.mark.asyncio
async def test_reopening_same_catalog_does_not_replace_an_earlier_lease(
    tmp_path: Path,
) -> None:
    _write_skill(tmp_path, "stable")
    provider = PackagedFilesystemDurableSkillProvider(tmp_path)
    first = await provider.open_snapshot(
        agent_slug="agent",
        retain_until=datetime.now(UTC) + timedelta(hours=1),
    )
    second = await provider.open_snapshot(
        agent_slug="agent",
        retain_until=datetime.now(UTC) + timedelta(hours=2),
    )

    assert first.snapshot_token != second.snapshot_token
    metadata = first.metadata[0]
    first_content = await provider.get_content(
        snapshot=first,
        skill_id=metadata.skill_id,
        version=metadata.version,
        expected_hash=metadata.content_hash,
    )
    assert first_content.content_hash == metadata.content_hash


@pytest.mark.asyncio
async def test_versions_ignore_paths_and_normalize_newlines(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    _write_skill(first_root, "stable")
    second_skill = _write_skill(second_root, "stable")
    second_source = (second_skill / "SKILL.md").read_text(encoding="utf-8")
    (second_skill / "SKILL.md").write_bytes(second_source.replace("\n", "\r\n").encode())
    retain_until = _retain_until()

    first = await PackagedFilesystemDurableSkillProvider(first_root).open_snapshot(
        agent_slug="agent",
        retain_until=retain_until,
    )
    second = await PackagedFilesystemDurableSkillProvider(second_root).open_snapshot(
        agent_slug="agent",
        retain_until=retain_until,
    )

    assert first.catalog_hash == second.catalog_hash
    assert first.metadata[0].version == second.metadata[0].version
    assert first.snapshot_token == second.snapshot_token


@pytest.mark.asyncio
async def test_requested_revision_is_exact_and_never_substituted(tmp_path: Path) -> None:
    _write_skill(tmp_path, "stable")
    provider = PackagedFilesystemDurableSkillProvider(tmp_path)
    snapshot = await provider.open_snapshot(agent_slug="agent", retain_until=_retain_until())

    exact = await provider.open_snapshot(
        agent_slug="agent",
        requested_revision=snapshot.catalog_revision,
        retain_until=_retain_until(),
    )
    assert exact.catalog_revision == snapshot.catalog_revision

    with pytest.raises(DurableSkillVersionUnavailableError):
        await provider.open_snapshot(
            agent_slug="agent",
            requested_revision="0" * 64,
            retain_until=_retain_until(),
        )
    with pytest.raises(DurableSkillValidationError, match="requested_revision"):
        await provider.open_snapshot(
            agent_slug="agent",
            requested_revision="latest",
            retain_until=_retain_until(),
        )


@pytest.mark.asyncio
async def test_search_is_deterministic_paginated_and_query_bound(tmp_path: Path) -> None:
    _write_skill(tmp_path, "zulu", description="Shared reference")
    _write_skill(tmp_path, "alpha", description="Shared API", tags=("network",))
    _write_skill(tmp_path, "bravo", description="Shared API", tags=("storage",))
    provider = PackagedFilesystemDurableSkillProvider(tmp_path, page_size=2)
    snapshot = await provider.open_snapshot(agent_slug="agent", retain_until=_retain_until())

    first = await provider.search_metadata(snapshot=snapshot, query="  SHARED   api ")
    assert tuple(item.skill_id for item in first.metadata) == ("alpha", "bravo")
    assert first.next_cursor is None

    unfiltered = await provider.search_metadata(snapshot=snapshot)
    assert tuple(item.skill_id for item in unfiltered.metadata) == ("alpha", "bravo")
    assert unfiltered.next_cursor is not None
    second = await provider.search_metadata(
        snapshot=snapshot,
        cursor=unfiltered.next_cursor,
    )
    assert tuple(item.skill_id for item in second.metadata) == ("zulu",)
    assert second.next_cursor is None

    with pytest.raises(DurableSkillCursorError):
        await provider.search_metadata(
            snapshot=snapshot,
            cursor=unfiltered.next_cursor,
            query="shared",
        )
    with pytest.raises(DurableSkillCursorError):
        await provider.search_metadata(
            snapshot=snapshot,
            cursor=unfiltered.next_cursor[:-1] + "x",
        )


@pytest.mark.asyncio
async def test_cursor_and_snapshot_are_bound_to_exact_snapshot(tmp_path: Path) -> None:
    _write_skill(tmp_path, "alpha")
    _write_skill(tmp_path, "bravo")
    provider = PackagedFilesystemDurableSkillProvider(tmp_path, page_size=1)
    first = await provider.open_snapshot(agent_slug="first", retain_until=_retain_until())
    second = await provider.open_snapshot(agent_slug="second", retain_until=_retain_until())
    page = await provider.search_metadata(snapshot=first)
    assert page.next_cursor is not None

    with pytest.raises(DurableSkillCursorError):
        await provider.search_metadata(snapshot=second, cursor=page.next_cursor)

    foreign_provider = PackagedFilesystemDurableSkillProvider(
        tmp_path,
        provider_id="another-provider",
    )
    with pytest.raises(DurableSkillSnapshotUnavailableError):
        await foreign_provider.search_metadata(snapshot=first)


@pytest.mark.asyncio
async def test_get_content_fails_explicitly_for_unknown_version_and_hash(tmp_path: Path) -> None:
    _write_skill(tmp_path, "stable")
    provider = PackagedFilesystemDurableSkillProvider(tmp_path)
    snapshot = await provider.open_snapshot(agent_slug="agent", retain_until=_retain_until())
    metadata = snapshot.metadata[0]

    with pytest.raises(DurableSkillNotFoundError):
        await provider.get_content(
            snapshot=snapshot,
            skill_id="unknown",
            version=metadata.version,
            expected_hash=metadata.content_hash,
        )
    with pytest.raises(DurableSkillVersionUnavailableError):
        await provider.get_content(
            snapshot=snapshot,
            skill_id=metadata.skill_id,
            version="old-version",
            expected_hash=metadata.content_hash,
        )
    with pytest.raises(DurableSkillIntegrityError):
        await provider.get_content(
            snapshot=snapshot,
            skill_id=metadata.skill_id,
            version=metadata.version,
            expected_hash="0" * 64,
        )


@pytest.mark.asyncio
async def test_empty_catalog_is_valid_but_parse_failure_is_explicit(tmp_path: Path) -> None:
    provider = PackagedFilesystemDurableSkillProvider(tmp_path)
    snapshot = await provider.open_snapshot(agent_slug="agent", retain_until=_retain_until())
    page = await provider.search_metadata(snapshot=snapshot)
    assert snapshot.metadata == ()
    assert page.metadata == ()

    broken = tmp_path / "skills" / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text(
        "---\nname: [unclosed\n---\nbody\n",
        encoding="utf-8",
    )
    clear_skills_cache()
    with pytest.raises(DurableSkillValidationError, match="discovery failed"):
        await provider.open_snapshot(agent_slug="agent", retain_until=_retain_until())


@pytest.mark.asyncio
async def test_duplicate_skill_ids_fail_explicitly(tmp_path: Path) -> None:
    first = tmp_path / "skills" / "one"
    second = tmp_path / "skills" / "two"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    source = "---\nname: duplicate\ndescription: duplicate\n---\nbody\n"
    (first / "SKILL.md").write_text(source, encoding="utf-8")
    (second / "SKILL.md").write_text(source, encoding="utf-8")

    with pytest.raises(DurableSkillValidationError, match="discovery failed"):
        await PackagedFilesystemDurableSkillProvider(tmp_path).open_snapshot(
            agent_slug="agent",
            retain_until=_retain_until(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("placeholder", ["$API_TOKEN", "${API_TOKEN}", "%API_TOKEN%"])
async def test_environment_placeholders_are_rejected(
    tmp_path: Path,
    placeholder: str,
) -> None:
    _write_skill(tmp_path, "unsafe", body=f"Use {placeholder}.\n")

    with pytest.raises(DurableSkillValidationError, match="environment placeholder"):
        await PackagedFilesystemDurableSkillProvider(tmp_path).open_snapshot(
            agent_slug="agent",
            retain_until=_retain_until(),
        )


@pytest.mark.asyncio
async def test_environment_placeholders_in_references_are_rejected(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "unsafe")
    _write_reference(skill_dir, "secret.txt", "Bearer $API_TOKEN\n")

    with pytest.raises(DurableSkillValidationError, match="environment placeholder"):
        await PackagedFilesystemDurableSkillProvider(tmp_path).open_snapshot(
            agent_slug="agent",
            retain_until=_retain_until(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("relative_path", "is_directory"),
    [
        ("assets", True),
        ("scripts", True),
        ("other", True),
        ("references/assets", True),
        ("README.md", False),
        ("references/tool.py", False),
    ],
)
async def test_non_instruction_files_and_directories_are_rejected(
    tmp_path: Path,
    relative_path: str,
    is_directory: bool,
) -> None:
    skill_dir = _write_skill(tmp_path, "unsafe")
    path = skill_dir / relative_path
    if is_directory:
        path.mkdir(parents=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("unsafe", encoding="utf-8")

    with pytest.raises(DurableSkillValidationError):
        await PackagedFilesystemDurableSkillProvider(tmp_path).open_snapshot(
            agent_slug="agent",
            retain_until=_retain_until(),
        )


@pytest.mark.asyncio
async def test_binary_reference_is_rejected(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "unsafe")
    binary = skill_dir / "references" / "binary.txt"
    binary.parent.mkdir()
    binary.write_bytes(b"text\x00binary")

    with pytest.raises(DurableSkillValidationError, match="binary"):
        await PackagedFilesystemDurableSkillProvider(tmp_path).open_snapshot(
            agent_slug="agent",
            retain_until=_retain_until(),
        )


@pytest.mark.asyncio
async def test_non_utf8_reference_is_rejected(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "unsafe")
    binary = skill_dir / "references" / "binary.txt"
    binary.parent.mkdir()
    binary.write_bytes(b"\xff")

    with pytest.raises(DurableSkillValidationError, match="UTF-8"):
        await PackagedFilesystemDurableSkillProvider(tmp_path).open_snapshot(
            agent_slug="agent",
            retain_until=_retain_until(),
        )


@pytest.mark.asyncio
async def test_symlink_is_rejected_without_following_it(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "unsafe")
    reference_dir = skill_dir / "references"
    reference_dir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = reference_dir / "linked.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(DurableSkillValidationError, match="symlink"):
        await PackagedFilesystemDurableSkillProvider(tmp_path).open_snapshot(
            agent_slug="agent",
            retain_until=_retain_until(),
        )


@pytest.mark.asyncio
async def test_symlinked_skills_root_is_rejected(tmp_path: Path) -> None:
    outside_root = tmp_path / "outside"
    _write_skill(outside_root, "unsafe")
    app_root = tmp_path / "app"
    app_root.mkdir()
    try:
        (app_root / "skills").symlink_to(outside_root / "skills", target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")

    clear_skills_cache()
    with pytest.raises(DurableSkillValidationError, match="skills root"):
        await PackagedFilesystemDurableSkillProvider(app_root).open_snapshot(
            agent_slug="agent",
            retain_until=_retain_until(),
        )


@pytest.mark.asyncio
async def test_executable_reference_is_rejected_when_mode_is_supported(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "unsafe")
    executable = _write_reference(skill_dir, "executable.txt", "instructions")
    executable.chmod(executable.stat().st_mode | 0o100)
    if not executable.stat().st_mode & 0o111:
        pytest.skip("executable mode bits are unavailable")

    with pytest.raises(DurableSkillValidationError, match="executable"):
        await PackagedFilesystemDurableSkillProvider(tmp_path).open_snapshot(
            agent_slug="agent",
            retain_until=_retain_until(),
        )


@pytest.mark.asyncio
async def test_reference_count_limit_allows_64_and_rejects_65(tmp_path: Path) -> None:
    allowed_dir = _write_skill(tmp_path / "allowed", "bounded")
    for index in range(MAX_DURABLE_SKILL_REFERENCE_FILES):
        _write_reference(allowed_dir, f"{index:02}.txt", "reference")
    allowed = await PackagedFilesystemDurableSkillProvider(tmp_path / "allowed").open_snapshot(
        agent_slug="agent", retain_until=_retain_until()
    )
    assert len(allowed.metadata) == 1

    rejected_dir = _write_skill(tmp_path / "rejected", "overflow")
    for index in range(MAX_DURABLE_SKILL_REFERENCE_FILES + 1):
        _write_reference(rejected_dir, f"{index:02}.txt", "reference")
    with pytest.raises(DurableSkillCatalogLimitError, match="reference files"):
        await PackagedFilesystemDurableSkillProvider(tmp_path / "rejected").open_snapshot(
            agent_slug="agent", retain_until=_retain_until()
        )


@pytest.mark.asyncio
async def test_content_size_limit_is_enforced(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "overflow",
        body="x" * MAX_DURABLE_SKILL_CONTENT_BYTES,
    )

    with pytest.raises(DurableSkillCatalogLimitError, match="2 MiB"):
        await PackagedFilesystemDurableSkillProvider(tmp_path).open_snapshot(
            agent_slug="agent",
            retain_until=_retain_until(),
        )


@pytest.mark.asyncio
async def test_record_and_metadata_catalog_limits_are_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = PackagedFilesystemDurableSkillProvider(tmp_path)
    too_many = {
        f"skill-{index:05}": tmp_path / str(index) for index in range(MAX_DURABLE_SKILLS + 1)
    }
    monkeypatch.setattr(
        providers,
        "discover_skills",
        lambda _root: SkillDiscoveryResult(skills=too_many, failed_loads=[]),
    )
    with pytest.raises(DurableSkillCatalogLimitError, match="records"):
        await provider.open_snapshot(agent_slug="agent", retain_until=_retain_until())

    _write_skill(tmp_path, "bounded")
    clear_skills_cache()
    monkeypatch.undo()
    monkeypatch.setattr(providers, "MAX_DURABLE_SKILL_METADATA_BYTES", 1)
    with pytest.raises(DurableSkillCatalogLimitError, match="8 MiB"):
        await provider.open_snapshot(agent_slug="agent", retain_until=_retain_until())
    assert MAX_DURABLE_SKILL_METADATA_BYTES == 8 * 1024 * 1024


@pytest.mark.asyncio
async def test_nested_includes_resolve_deterministically(tmp_path: Path) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "includes",
        body="[first](./references/first.md)\n",
    )
    _write_reference(
        skill_dir,
        "first.md",
        "First.\n[second](./references/nested/second.txt)\n",
    )
    _write_reference(skill_dir, "nested/second.txt", "Second.\n")

    provider = PackagedFilesystemDurableSkillProvider(tmp_path)
    snapshot = await provider.open_snapshot(agent_slug="agent", retain_until=_retain_until())
    metadata = snapshot.metadata[0]
    content = await provider.get_content(
        snapshot=snapshot,
        skill_id=metadata.skill_id,
        version=metadata.version,
        expected_hash=metadata.content_hash,
    )

    assert content.files[0].content == "First.\nSecond.\n"
    assert content.files[1].content == "First.\nSecond.\n"
    assert content.files[2].content == "Second.\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("[outside](./references/../SKILL.md)\n", "escapes references"),
        ("[missing](./references/missing.md)\n", "missing reference"),
    ],
)
async def test_invalid_include_targets_fail_closed(
    tmp_path: Path,
    body: str,
    expected: str,
) -> None:
    _write_skill(tmp_path, "includes", body=body)

    with pytest.raises(DurableSkillValidationError, match=expected):
        await PackagedFilesystemDurableSkillProvider(tmp_path).open_snapshot(
            agent_slug="agent",
            retain_until=_retain_until(),
        )


@pytest.mark.asyncio
async def test_circular_includes_fail_closed(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "includes")
    _write_reference(skill_dir, "first.md", "[second](./references/second.md)\n")
    _write_reference(skill_dir, "second.md", "[first](./references/first.md)\n")

    with pytest.raises(DurableSkillValidationError, match="circular include"):
        await PackagedFilesystemDurableSkillProvider(tmp_path).open_snapshot(
            agent_slug="agent",
            retain_until=_retain_until(),
        )


@pytest.mark.asyncio
async def test_inline_markdown_links_are_not_treated_as_includes(tmp_path: Path) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "links",
        body="Read [details](./references/details.md) before proceeding.\n",
    )
    _write_reference(skill_dir, "details.md", "Details.\n")
    provider = PackagedFilesystemDurableSkillProvider(tmp_path)
    snapshot = await provider.open_snapshot(agent_slug="agent", retain_until=_retain_until())
    metadata = snapshot.metadata[0]
    content = await provider.get_content(
        snapshot=snapshot,
        skill_id=metadata.skill_id,
        version=metadata.version,
        expected_hash=metadata.content_hash,
    )
    assert content.files[0].content == (
        "Read [details](./references/details.md) before proceeding."
    )


def test_models_are_strict_extra_forbidden_and_frozen() -> None:
    valid = {
        "skill_id": "strict",
        "display_name": "Strict",
        "selection_description": "Strict metadata",
        "version": "1",
        "content_hash": "0" * 64,
        "tags": (),
        "executable": False,
    }
    with pytest.raises(ValidationError):
        DurableSkillMetadataV1.model_validate({**valid, "unexpected": "field"})
    with pytest.raises(ValidationError):
        DurableSkillMetadataV1.model_validate({**valid, "display_name": 0})

    metadata = DurableSkillMetadataV1.model_validate(valid)
    with pytest.raises(ValidationError):
        metadata.selection_description = "changed"


def test_constructor_and_retention_validation_are_explicit(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="provider_id"):
        PackagedFilesystemDurableSkillProvider(tmp_path, provider_id="Provider Secret")
    with pytest.raises(ValueError, match="page_size"):
        PackagedFilesystemDurableSkillProvider(tmp_path, page_size=0)


@pytest.mark.asyncio
async def test_naive_or_expired_retention_is_rejected(tmp_path: Path) -> None:
    provider = PackagedFilesystemDurableSkillProvider(tmp_path)
    with pytest.raises(DurableSkillValidationError, match="timezone"):
        await provider.open_snapshot(
            agent_slug="agent",
            retain_until=datetime.now(),
        )
    with pytest.raises(DurableSkillValidationError, match="future"):
        await provider.open_snapshot(
            agent_slug="agent",
            retain_until=datetime.now(UTC) - timedelta(seconds=1),
        )


def test_published_hard_limits_match_the_frd() -> None:
    assert MAX_DURABLE_SKILLS == 10_000
    assert MAX_DURABLE_SKILL_METADATA_BYTES == 8 * 1024 * 1024
    assert MAX_DURABLE_SKILL_REFERENCE_FILES == 64
    assert MAX_DURABLE_SKILL_CONTENT_BYTES == 2 * 1024 * 1024
