"""Durable instruction-only skill provider contracts and packaged-file adapter."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import stat
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Protocol, runtime_checkable

import frontmatter
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

from ..discovery.skills import discover_skills
from ..strict_json import canonical_json_bytes, decode_json_object
from .durable_loop_protocol import (
    MAX_SKILL_CATALOG_METADATA_BYTES,
    MAX_SKILL_CATALOG_RECORDS,
    MAX_SKILL_CONTENT_BYTES,
    MAX_SKILL_REFERENCE_FILES,
    DurableSkillCatalogSnapshotV1,
    DurableSkillContentFileV1,
    DurableSkillContentV1,
    DurableSkillMetadataPageV1,
    DurableSkillMetadataV1,
)

MAX_DURABLE_SKILLS = MAX_SKILL_CATALOG_RECORDS
MAX_DURABLE_SKILL_METADATA_BYTES = MAX_SKILL_CATALOG_METADATA_BYTES
MAX_DURABLE_SKILL_REFERENCE_FILES = MAX_SKILL_REFERENCE_FILES
MAX_DURABLE_SKILL_CONTENT_BYTES = MAX_SKILL_CONTENT_BYTES
DEFAULT_DURABLE_SKILL_PAGE_SIZE = 32
MAX_DURABLE_SKILL_PAGE_SIZE = 100

_ALLOWED_REFERENCE_EXTENSIONS = frozenset({".json", ".md", ".txt", ".yaml", ".yml"})
_ENV_PLACEHOLDER = re.compile(
    r"(?:\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})"
    r"|%[A-Za-z_][A-Za-z0-9_]*%)"
)
_INCLUDE_LINE = re.compile(r"^[ \t]*\[[^\]\r\n]+\]\((\./[^)\r\n]+)\)[ \t]*$")
_SKILL_ID_PATTERN = r"^[a-z0-9](?:[a-z0-9]*-[a-z0-9])*[a-z0-9]*$"
_HASH_PATTERN = r"^[0-9a-f]{64}$"
_PROVIDER_ID_PATTERN = r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$"
_SNAPSHOT_TOKEN_PATTERN = r"^dss1_[A-Za-z0-9_-]{43}$"

SkillId = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=64, pattern=_SKILL_ID_PATTERN),
]
HashValue = Annotated[
    str,
    StringConstraints(strict=True, min_length=64, max_length=64, pattern=_HASH_PATTERN),
]
SnapshotToken = Annotated[
    str,
    StringConstraints(strict=True, min_length=48, max_length=48, pattern=_SNAPSHOT_TOKEN_PATTERN),
]


class DurableSkillProviderError(RuntimeError):
    """Base class for explicit durable skill provider failures."""


class DurableSkillValidationError(DurableSkillProviderError):
    """Raised when an authored skill is not safe for durable instruction-only use."""


class DurableSkillCatalogLimitError(DurableSkillProviderError):
    """Raised when a durable skill catalog exceeds a hard admission limit."""


class DurableSkillSnapshotUnavailableError(DurableSkillProviderError):
    """Raised when an immutable provider snapshot is unknown or expired."""


class DurableSkillCursorError(DurableSkillProviderError):
    """Raised when a search cursor is malformed or bound to another search."""


class DurableSkillNotFoundError(DurableSkillProviderError):
    """Raised when a skill is absent from the admitted snapshot."""


class DurableSkillVersionUnavailableError(DurableSkillProviderError):
    """Raised when an exact catalog or skill version cannot be served."""


class DurableSkillIntegrityError(DurableSkillProviderError):
    """Raised when an expected immutable hash does not match admitted content."""


class _DurableSkillModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


@runtime_checkable
class DurableSkillProvider(Protocol):
    """Asynchronous exact-version boundary for durable skill providers."""

    async def open_snapshot(
        self,
        *,
        agent_slug: str,
        requested_revision: str | None = None,
        retain_until: datetime,
    ) -> DurableSkillCatalogSnapshotV1: ...

    async def search_metadata(
        self,
        *,
        snapshot: DurableSkillCatalogSnapshotV1,
        cursor: str | None = None,
        query: str | None = None,
    ) -> DurableSkillMetadataPageV1: ...

    async def get_content(
        self,
        *,
        snapshot: DurableSkillCatalogSnapshotV1,
        skill_id: str,
        version: str,
        expected_hash: str,
    ) -> DurableSkillContentV1: ...


class _SkillDeclaration(_DurableSkillModel):
    name: SkillId
    description: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=320)]
    display_name: (
        Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)] | None
    ) = None
    tags: Annotated[
        list[Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)]],
        Field(max_length=32),
    ] = Field(default_factory=list)

    @field_validator("description", "display_name")
    @classmethod
    def reject_blank_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("skill metadata must not be blank")
        return value

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[str]) -> list[str]:
        normalized = sorted(value, key=lambda tag: (tag.casefold(), tag))
        if any(not tag.strip() for tag in normalized):
            raise ValueError("skill tags must not be blank")
        if len(set(normalized)) != len(normalized):
            raise ValueError("skill tags must be unique")
        return normalized


class _CursorPayload(_DurableSkillModel):
    cursor_version: Literal["dsc1"] = "dsc1"
    snapshot_token: SnapshotToken
    catalog_hash: HashValue
    query: Annotated[str, StringConstraints(strict=True, max_length=512)]
    offset: Annotated[int, Field(ge=1, le=MAX_DURABLE_SKILLS)]
    page_size: Annotated[int, Field(ge=1, le=MAX_DURABLE_SKILL_PAGE_SIZE)]


class _CapturedSnapshot:
    __slots__ = ("content", "snapshot")

    def __init__(
        self,
        *,
        snapshot: DurableSkillCatalogSnapshotV1,
        content: Mapping[str, DurableSkillContentV1],
    ) -> None:
        self.snapshot = snapshot
        self.content = dict(content)


class PackagedFilesystemDurableSkillProvider:
    """Capture packaged ``skills/*/SKILL.md`` files as immutable snapshots."""

    def __init__(
        self,
        app_root: Path,
        *,
        provider_id: str = "packaged-filesystem",
        page_size: int = DEFAULT_DURABLE_SKILL_PAGE_SIZE,
    ) -> None:
        if not re.fullmatch(_PROVIDER_ID_PATTERN, provider_id) or len(provider_id) > 128:
            raise ValueError("provider_id is invalid")
        if not 1 <= page_size <= MAX_DURABLE_SKILL_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_DURABLE_SKILL_PAGE_SIZE}")
        self._app_root = Path(app_root).resolve()
        self._provider_id = provider_id
        self._page_size = page_size
        self._snapshots: dict[str, _CapturedSnapshot] = {}

    async def open_snapshot(
        self,
        *,
        agent_slug: str,
        requested_revision: str | None = None,
        retain_until: datetime,
    ) -> DurableSkillCatalogSnapshotV1:
        """Capture the current packaged catalog without substituting old revisions."""
        normalized_agent_slug = _validate_agent_slug(agent_slug)
        normalized_retain_until = _validate_retain_until(retain_until)
        self._purge_expired_snapshots()

        metadata, content = self._capture_catalog()
        metadata_document = [item.model_dump(mode="json") for item in metadata]
        metadata_bytes = canonical_json_bytes(metadata_document)
        if len(metadata_bytes) > MAX_DURABLE_SKILL_METADATA_BYTES:
            raise DurableSkillCatalogLimitError("durable skill catalog metadata exceeds 8 MiB")
        catalog_hash = hashlib.sha256(metadata_bytes).hexdigest()
        catalog_revision = catalog_hash
        if requested_revision is not None and not re.fullmatch(_HASH_PATTERN, requested_revision):
            raise DurableSkillValidationError("requested_revision is invalid")
        if requested_revision is not None and requested_revision != catalog_revision:
            raise DurableSkillVersionUnavailableError("requested catalog revision is unavailable")

        snapshot_token = _snapshot_token(
            provider_id=self._provider_id,
            agent_slug=normalized_agent_slug,
            catalog_hash=catalog_hash,
            retain_until=normalized_retain_until,
        )
        try:
            snapshot = DurableSkillCatalogSnapshotV1.create(
                provider_id=self._provider_id,
                catalog_revision=catalog_revision,
                metadata=metadata,
                snapshot_token=snapshot_token,
                retain_until=normalized_retain_until,
            )
        except ValidationError as exc:
            raise DurableSkillValidationError("captured durable skill snapshot is invalid") from exc
        existing = self._snapshots.get(snapshot_token)
        if existing is not None and existing.snapshot.catalog_hash != snapshot.catalog_hash:
            raise DurableSkillIntegrityError("snapshot token collision")
        self._snapshots[snapshot_token] = _CapturedSnapshot(
            snapshot=snapshot,
            content=content,
        )
        return snapshot

    async def search_metadata(
        self,
        *,
        snapshot: DurableSkillCatalogSnapshotV1,
        cursor: str | None = None,
        query: str | None = None,
    ) -> DurableSkillMetadataPageV1:
        """Search one immutable catalog with snapshot-bound deterministic pagination."""
        captured = self._require_snapshot(snapshot)
        normalized_query = _normalize_query(query)
        offset = 0
        if cursor is not None:
            payload = _decode_cursor(cursor, snapshot.snapshot_token)
            if (
                payload.snapshot_token != snapshot.snapshot_token
                or payload.catalog_hash != snapshot.catalog_hash
                or payload.query != normalized_query
                or payload.page_size != self._page_size
            ):
                raise DurableSkillCursorError(
                    "cursor is bound to another snapshot, query, ordering, or page size"
                )
            offset = payload.offset

        matches = tuple(
            item for item in captured.snapshot.metadata if _metadata_matches(item, normalized_query)
        )
        if offset > len(matches):
            raise DurableSkillCursorError("cursor offset is outside the search result")
        return _build_metadata_page(
            snapshot=snapshot,
            matches=matches,
            offset=offset,
            page_size=self._page_size,
            query=normalized_query,
        )

    async def get_content(
        self,
        *,
        snapshot: DurableSkillCatalogSnapshotV1,
        skill_id: str,
        version: str,
        expected_hash: str,
    ) -> DurableSkillContentV1:
        """Return only the exact content admitted by the supplied snapshot."""
        captured = self._require_snapshot(snapshot)
        metadata = next(
            (item for item in captured.snapshot.metadata if item.skill_id == skill_id),
            None,
        )
        if metadata is None:
            raise DurableSkillNotFoundError(
                f"skill {skill_id!r} is absent from the admitted snapshot"
            )
        if version != metadata.version:
            raise DurableSkillVersionUnavailableError(
                f"skill {skill_id!r} version {version!r} is unavailable"
            )
        if expected_hash != metadata.content_hash:
            raise DurableSkillIntegrityError(
                f"skill {skill_id!r} expected hash does not match the admitted catalog"
            )
        content = captured.content.get(skill_id)
        if content is None:
            raise DurableSkillSnapshotUnavailableError(
                f"skill {skill_id!r} content is unavailable for the admitted snapshot"
            )
        actual_hash = _hash_files(content.files)
        if actual_hash != expected_hash or content.content_hash != expected_hash:
            raise DurableSkillIntegrityError(
                f"skill {skill_id!r} content failed immutable hash verification"
            )
        return content

    def _capture_catalog(
        self,
    ) -> tuple[tuple[DurableSkillMetadataV1, ...], dict[str, DurableSkillContentV1]]:
        skills_root = _resolve_packaged_skills_root(self._app_root)
        if skills_root is not None:
            _reject_packaged_links(skills_root)
        try:
            discovery = discover_skills(self._app_root)
        except DurableSkillProviderError:
            raise
        except Exception as exc:
            raise DurableSkillValidationError(
                f"packaged skill discovery failed: {type(exc).__name__}"
            ) from exc
        if discovery.failed_loads:
            failed_paths = ", ".join(path for path, _ in discovery.failed_loads)
            raise DurableSkillValidationError(
                f"packaged skill discovery failed for: {failed_paths}"
            )
        if len(discovery.skills) > MAX_DURABLE_SKILLS:
            raise DurableSkillCatalogLimitError(
                f"durable skill catalog exceeds {MAX_DURABLE_SKILLS} records"
            )
        entries: list[tuple[DurableSkillMetadataV1, DurableSkillContentV1]] = []
        for discovered_id, skill_dir in discovery.skills.items():
            try:
                skill_metadata, skill_content = _capture_skill(
                    discovered_id,
                    skill_dir,
                    skills_root,
                )
            except DurableSkillProviderError:
                raise
            except OSError as exc:
                raise DurableSkillValidationError(
                    f"durable skill {discovered_id!r} filesystem access failed"
                ) from exc
            entries.append((skill_metadata, skill_content))
        entries.sort(key=lambda entry: (entry[0].skill_id.casefold(), entry[0].skill_id))
        ordered_metadata = tuple(item[0] for item in entries)
        if len({item.skill_id for item in ordered_metadata}) != len(ordered_metadata):
            raise DurableSkillValidationError("durable skill catalog contains duplicate skill IDs")
        return ordered_metadata, {item.skill_id: item_content for item, item_content in entries}

    def _require_snapshot(
        self,
        snapshot: DurableSkillCatalogSnapshotV1,
    ) -> _CapturedSnapshot:
        if snapshot.provider_id != self._provider_id:
            raise DurableSkillSnapshotUnavailableError(
                "snapshot belongs to a different durable skill provider"
            )
        captured = self._snapshots.get(snapshot.snapshot_token)
        if captured is None or captured.snapshot != snapshot:
            raise DurableSkillSnapshotUnavailableError(
                "durable skill snapshot is unknown or no longer retained"
            )
        if snapshot.retain_until < datetime.now(UTC):
            self._snapshots.pop(snapshot.snapshot_token, None)
            raise DurableSkillSnapshotUnavailableError(
                "durable skill snapshot retention has expired"
            )
        return captured

    def _purge_expired_snapshots(self) -> None:
        now = datetime.now(UTC)
        expired = [
            token
            for token, captured in self._snapshots.items()
            if captured.snapshot.retain_until < now
        ]
        for token in expired:
            self._snapshots.pop(token, None)


def _capture_skill(
    discovered_id: str,
    skill_dir: Path,
    skills_root: Path | None,
) -> tuple[DurableSkillMetadataV1, DurableSkillContentV1]:
    if skills_root is None:
        raise DurableSkillValidationError(
            f"durable skill {discovered_id!r} is outside the packaged skills root"
        )
    _validate_skill_location(discovered_id, skill_dir, skills_root)
    normalized_dir = skill_dir.resolve()
    files = _admitted_skill_files(discovered_id, normalized_dir)
    source_by_path: dict[str, str] = {}
    total_source_bytes = 0
    for relative_path, path in files:
        source, source_bytes = _read_utf8_file(discovered_id, path)
        total_source_bytes += source_bytes
        if total_source_bytes > MAX_DURABLE_SKILL_CONTENT_BYTES:
            raise DurableSkillCatalogLimitError(f"durable skill {discovered_id!r} exceeds 2 MiB")
        _reject_environment_placeholders(discovered_id, relative_path, source)
        source_by_path[relative_path] = source

    skill_source = source_by_path["SKILL.md"]
    try:
        post = frontmatter.loads(skill_source)
        declaration = _SkillDeclaration.model_validate(
            {
                "name": post.metadata.get("name"),
                "description": post.metadata.get("description"),
                "display_name": post.metadata.get("display_name"),
                "tags": post.metadata.get("tags", []),
            }
        )
    except Exception as exc:
        raise DurableSkillValidationError(
            f"durable skill {discovered_id!r} has invalid strict frontmatter"
        ) from exc
    if declaration.name != discovered_id:
        raise DurableSkillValidationError(
            f"durable skill ID {discovered_id!r} changed while being captured"
        )

    reference_sources = {
        path: source for path, source in source_by_path.items() if path != "SKILL.md"
    }
    try:
        return _build_skill_documents(
            discovered_id,
            declaration,
            post.content,
            reference_sources,
        )
    except DurableSkillProviderError:
        raise
    except Exception as exc:
        raise DurableSkillValidationError(
            f"durable skill {discovered_id!r} canonical content is invalid"
        ) from exc


def _build_skill_documents(
    discovered_id: str,
    declaration: _SkillDeclaration,
    instruction_source: str,
    reference_sources: Mapping[str, str],
) -> tuple[DurableSkillMetadataV1, DurableSkillContentV1]:
    resolver = _IncludeResolver(discovered_id, reference_sources)
    instructions = resolver.resolve_text(
        instruction_source,
        source_path="SKILL.md",
        stack=(),
    )
    resolved_size = len(instructions.encode("utf-8"))
    resolved_references: list[DurableSkillContentFileV1] = []
    for path in sorted(reference_sources, key=lambda value: (value.casefold(), value)):
        resolved_reference = resolver.resolve_reference(path, stack=())
        resolved_size += len(path.encode("utf-8")) + len(resolved_reference.encode("utf-8"))
        if resolved_size > MAX_DURABLE_SKILL_CONTENT_BYTES:
            raise DurableSkillCatalogLimitError(
                f"durable skill {discovered_id!r} resolved content exceeds 2 MiB"
            )
        resolved_references.append(
            DurableSkillContentFileV1(
                relative_path=path,
                content=resolved_reference,
            )
        )
    files = (
        DurableSkillContentFileV1(relative_path="SKILL.md", content=instructions),
        *resolved_references,
    )
    content_hash = _hash_files(files)
    content = DurableSkillContentV1.create(
        skill_id=declaration.name,
        version=content_hash,
        files=files,
    )
    metadata = DurableSkillMetadataV1(
        skill_id=declaration.name,
        display_name=declaration.display_name or declaration.name,
        selection_description=declaration.description,
        version=content.version,
        content_hash=content.content_hash,
        tags=tuple(declaration.tags),
        executable=False,
    )
    return metadata, content


def _resolve_packaged_skills_root(app_root: Path) -> Path | None:
    for name in ("skills", "Skills"):
        candidate = app_root / name
        if candidate.is_dir():
            if _is_link(candidate):
                raise DurableSkillValidationError(
                    "packaged skills root must not be a symlink or junction"
                )
            return candidate
    return None


def _reject_packaged_links(skills_root: Path) -> None:
    for root, directory_names, file_names in os.walk(
        skills_root,
        followlinks=False,
        onerror=_raise_walk_error,
    ):
        root_path = Path(root)
        for name in (*directory_names, *file_names):
            if _is_link(root_path / name):
                relative = (root_path / name).relative_to(skills_root).as_posix()
                raise DurableSkillValidationError(
                    f"packaged skills must not contain symlink or junction {relative!r}"
                )


def _validate_skill_location(skill_id: str, skill_dir: Path, skills_root: Path) -> None:
    try:
        relative = skill_dir.relative_to(skills_root)
    except ValueError as exc:
        raise DurableSkillValidationError(
            f"durable skill {skill_id!r} is outside the packaged skills root"
        ) from exc
    current = skills_root
    for part in relative.parts:
        current /= part
        if _is_link(current):
            raise DurableSkillValidationError(
                f"durable skill {skill_id!r} path must not contain a symlink or junction"
            )
    if not skill_dir.resolve().is_relative_to(skills_root.resolve()):
        raise DurableSkillValidationError(
            f"durable skill {skill_id!r} resolves outside the packaged skills root"
        )


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _admitted_skill_files(skill_id: str, skill_dir: Path) -> list[tuple[str, Path]]:
    if not skill_dir.is_dir():
        raise DurableSkillValidationError(f"durable skill {skill_id!r} directory is unavailable")
    admitted: list[tuple[str, Path]] = []
    reference_count = 0
    for root, directory_names, file_names in os.walk(
        skill_dir,
        followlinks=False,
        onerror=_raise_walk_error,
    ):
        root_path = Path(root)
        relative_root = root_path.relative_to(skill_dir)
        for directory_name in directory_names:
            _validate_skill_directory(
                skill_id,
                root_path,
                relative_root,
                directory_name,
            )
        for file_name in file_names:
            path = root_path / file_name
            relative = (relative_root / file_name).as_posix()
            is_reference = _validate_admitted_file(skill_id, path, relative)
            reference_count += int(is_reference)
            if reference_count > MAX_DURABLE_SKILL_REFERENCE_FILES:
                raise DurableSkillCatalogLimitError(
                    f"durable skill {skill_id!r} exceeds "
                    f"{MAX_DURABLE_SKILL_REFERENCE_FILES} reference files"
                )
            admitted.append((relative, path))
    if not any(relative == "SKILL.md" for relative, _ in admitted):
        raise DurableSkillValidationError(f"durable skill {skill_id!r} is missing SKILL.md")
    admitted.sort(key=lambda item: (item[0].casefold(), item[0]))
    return admitted


def _validate_skill_directory(
    skill_id: str,
    root_path: Path,
    relative_root: Path,
    directory_name: str,
) -> None:
    directory = root_path / directory_name
    relative = (relative_root / directory_name).as_posix()
    if _is_link(directory):
        raise DurableSkillValidationError(
            f"durable skill {skill_id!r} contains symlink {relative!r}"
        )
    if directory_name.casefold() in {"assets", "scripts"}:
        raise DurableSkillValidationError(
            f"durable skill {skill_id!r} contains forbidden directory {relative!r}"
        )
    if relative_root != Path("."):
        return
    if directory_name != "references":
        raise DurableSkillValidationError(
            f"durable skill {skill_id!r} contains directory outside references/: {relative!r}"
        )


def _validate_admitted_file(skill_id: str, path: Path, relative: str) -> bool:
    if _is_link(path):
        raise DurableSkillValidationError(
            f"durable skill {skill_id!r} contains symlink {relative!r}"
        )
    mode = path.stat(follow_symlinks=False).st_mode
    if not stat.S_ISREG(mode):
        raise DurableSkillValidationError(
            f"durable skill {skill_id!r} contains non-regular file {relative!r}"
        )
    if mode & 0o111:
        raise DurableSkillValidationError(
            f"durable skill {skill_id!r} contains executable file {relative!r}"
        )
    if relative == "SKILL.md":
        return False
    if (
        not relative.startswith("references/")
        or path.suffix.lower() not in _ALLOWED_REFERENCE_EXTENSIONS
    ):
        raise DurableSkillValidationError(
            f"durable skill {skill_id!r} contains forbidden file {relative!r}"
        )
    return True


def _raise_walk_error(exc: OSError) -> None:
    raise exc


def _read_utf8_file(skill_id: str, path: Path) -> tuple[str, int]:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise DurableSkillValidationError(
            f"durable skill {skill_id!r} file {path.name!r} is not readable UTF-8"
        ) from exc
    if any(ord(character) < 32 and character not in "\t\n\r" for character in text):
        raise DurableSkillValidationError(
            f"durable skill {skill_id!r} file {path.name!r} contains binary control bytes"
        )
    return text.replace("\r\n", "\n").replace("\r", "\n"), len(raw)


def _reject_environment_placeholders(skill_id: str, relative_path: str, text: str) -> None:
    if _ENV_PLACEHOLDER.search(text):
        raise DurableSkillValidationError(
            f"durable skill {skill_id!r} file {relative_path!r} contains an environment placeholder"
        )


class _IncludeResolver:
    def __init__(self, skill_id: str, references: Mapping[str, str]) -> None:
        self._skill_id = skill_id
        self._references = dict(references)
        self._resolved: dict[str, str] = {}

    def resolve_reference(self, path: str, *, stack: tuple[str, ...]) -> str:
        cached = self._resolved.get(path)
        if cached is not None:
            return cached
        if path in stack:
            cycle = " -> ".join((*stack, path))
            raise DurableSkillValidationError(
                f"durable skill {self._skill_id!r} contains circular include {cycle}"
            )
        source = self._references.get(path)
        if source is None:
            raise DurableSkillValidationError(
                f"durable skill {self._skill_id!r} includes missing reference {path!r}"
            )
        resolved = self.resolve_text(
            source,
            source_path=path,
            stack=(*stack, path),
        )
        self._resolved[path] = resolved
        return resolved

    def resolve_text(
        self,
        text: str,
        *,
        source_path: str,
        stack: tuple[str, ...],
    ) -> str:
        parts: list[str] = []
        size = 0
        for line in text.splitlines(keepends=True):
            has_newline = line.endswith("\n")
            bare_line = line[:-1] if has_newline else line
            match = _INCLUDE_LINE.fullmatch(bare_line)
            if match is None:
                replacement = line
            else:
                target = _normalize_include_target(
                    self._skill_id,
                    source_path,
                    match.group(1),
                )
                replacement = self.resolve_reference(target, stack=stack)
                if has_newline and not replacement.endswith("\n"):
                    replacement += "\n"
            size += len(replacement.encode("utf-8"))
            if size > MAX_DURABLE_SKILL_CONTENT_BYTES:
                raise DurableSkillCatalogLimitError(
                    f"durable skill {self._skill_id!r} resolved content exceeds 2 MiB"
                )
            parts.append(replacement)
        return "".join(parts)


def _normalize_include_target(skill_id: str, source_path: str, target: str) -> str:
    if "\\" in target or "?" in target or "#" in target:
        raise DurableSkillValidationError(
            f"durable skill {skill_id!r} has invalid include {target!r} in {source_path!r}"
        )
    path = PurePosixPath(target.removeprefix("./"))
    if (
        path.is_absolute()
        or not path.parts
        or path.parts[0] != "references"
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.suffix.lower() not in _ALLOWED_REFERENCE_EXTENSIONS
    ):
        raise DurableSkillValidationError(
            f"durable skill {skill_id!r} include {target!r} escapes references/"
        )
    return path.as_posix()


def _hash_files(files: tuple[DurableSkillContentFileV1, ...]) -> str:
    return hashlib.sha256(
        canonical_json_bytes([file.model_dump(mode="json") for file in files])
    ).hexdigest()


def _snapshot_token(
    *,
    provider_id: str,
    agent_slug: str,
    catalog_hash: str,
    retain_until: datetime,
) -> str:
    digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "agent_slug": agent_slug,
                "catalog_hash": catalog_hash,
                "provider_id": provider_id,
                "retain_until": retain_until.isoformat(),
            }
        )
    ).digest()
    return "dss1_" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _validate_agent_slug(agent_slug: str) -> str:
    if (
        not isinstance(agent_slug, str)
        or not 1 <= len(agent_slug) <= 128
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", agent_slug)
    ):
        raise DurableSkillValidationError("agent_slug is invalid")
    return agent_slug


def _validate_retain_until(retain_until: datetime) -> datetime:
    if retain_until.tzinfo is None or retain_until.utcoffset() is None:
        raise DurableSkillValidationError("retain_until must include a timezone offset")
    normalized = retain_until.astimezone(UTC)
    if normalized <= datetime.now(UTC):
        raise DurableSkillValidationError("retain_until must be in the future")
    return normalized


def _normalize_query(query: str | None) -> str:
    if query is None:
        return ""
    if not isinstance(query, str):
        raise DurableSkillValidationError("skill search query must be a string")
    normalized = " ".join(query.split()).casefold()
    if len(normalized) > 512:
        raise DurableSkillValidationError("skill search query exceeds 512 characters")
    return normalized


def _metadata_matches(metadata: DurableSkillMetadataV1, query: str) -> bool:
    if not query:
        return True
    searchable = " ".join(
        (
            metadata.skill_id,
            metadata.display_name,
            metadata.selection_description,
            *metadata.tags,
        )
    ).casefold()
    return all(term in searchable for term in query.split())


def _build_metadata_page(
    *,
    snapshot: DurableSkillCatalogSnapshotV1,
    matches: tuple[DurableSkillMetadataV1, ...],
    offset: int,
    page_size: int,
    query: str,
) -> DurableSkillMetadataPageV1:
    item_count = min(page_size, len(matches) - offset)
    query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
    while True:
        items = matches[offset : offset + item_count]
        next_offset = offset + item_count
        next_cursor = None
        if next_offset < len(matches):
            next_cursor = _encode_cursor(
                _CursorPayload(
                    snapshot_token=snapshot.snapshot_token,
                    catalog_hash=snapshot.catalog_hash,
                    query=query,
                    offset=next_offset,
                    page_size=page_size,
                )
            )
        try:
            return DurableSkillMetadataPageV1(
                catalog_hash=snapshot.catalog_hash,
                query_hash=query_hash,
                page_size=page_size,
                metadata=items,
                next_cursor=next_cursor,
            )
        except ValidationError as exc:
            if item_count <= 1:
                raise DurableSkillCatalogLimitError(
                    "one durable skill metadata record exceeds the search result limit"
                ) from exc
            item_count -= 1


def _encode_cursor(payload: _CursorPayload) -> str:
    document = canonical_json_bytes(payload)
    signature = hashlib.sha256(payload.snapshot_token.encode("ascii") + b"\0" + document).digest()
    return base64.urlsafe_b64encode(signature + document).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, snapshot_token: str) -> _CursorPayload:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
        signature, document = decoded[:32], decoded[32:]
        expected_signature = hashlib.sha256(
            snapshot_token.encode("ascii") + b"\0" + document
        ).digest()
        _validate_cursor_signature(signature, expected_signature)
        decoded_document = decode_json_object(document)
        return _CursorPayload.model_validate(decoded_document)
    except Exception as exc:
        raise DurableSkillCursorError("skill search cursor is invalid") from exc


def _validate_cursor_signature(signature: bytes, expected_signature: bytes) -> None:
    if len(signature) != 32 or not hmac.compare_digest(signature, expected_signature):
        raise ValueError("cursor signature mismatch")
