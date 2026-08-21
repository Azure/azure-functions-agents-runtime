"""Canonical manifest and digest contract for Foundry-hosted application content."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import stat as stat_module
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from ..strict_json import DuplicateJsonKeyError, decode_json_object

APPLICATION_CONTENT_MANIFEST_VERSION = "fha_application_content_v2"
APPLICATION_CONTENT_ENTRY_KIND: Literal["file"] = "file"
APPLICATION_CONTENT_DIGEST_PREFIX = "sha256:"
MAX_APPLICATION_CONTENT_MANIFEST_BYTES = 32 * 1024
MAX_APPLICATION_CONTENT_ENTRY_COUNT = 1_024
MAX_APPLICATION_CONTENT_PATH_BYTES = 1_024
MAX_APPLICATION_CONTENT_FILE_BYTES = 256 * 1024 * 1024

_SHA256_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_WINDOWS_DRIVE_PATH_PATTERN = re.compile(r"^[A-Za-z]:")
_READ_CHUNK_SIZE = 1024 * 1024
_REPARSE_POINT_ATTRIBUTE = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)

_EXCLUDED_PATH_COMPONENTS = frozenset(
    {
        ".azure",
        ".azure-functions-agents",
        ".bzr",
        ".cache",
        ".hypothesis",
        ".git",
        ".hg",
        ".history",
        ".mypy_cache",
        ".nox",
        ".pytype",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        ".virtualenv",
        "__pycache__",
        "cache",
        "env",
        "history",
        "state",
        "venv",
    }
)
_EXCLUDED_FILE_NAMES = frozenset(
    {
        "credentials",
        "credentials.json",
        "id_ed25519",
        "id_rsa",
        "local.settings.json",
        "secrets",
        "secrets.json",
    }
)
_EXCLUDED_FILE_SUFFIXES = (".cer", ".crt", ".key", ".pem", ".p12", ".pfx")
_SEMANTIC_FILE_NAMES = frozenset(
    {
        "agents.config.yaml",
        "mcp.json",
        "requirements.txt",
    }
)
_SEMANTIC_DIRECTORY_IGNORES = _EXCLUDED_PATH_COMPONENTS | frozenset(
    {
        ".python_packages",
        "node_modules",
        "site-packages",
        "wheels",
    }
)
_DEPLOYMENT_ONLY_FILE_NAMES = frozenset(
    {
        ".funcignore",
        "host.json",
        "local.settings.template.json",
    }
)
_AGENT_DIRECTORY_NAMES = ("agents", "Agents")
_SKILL_DIRECTORY_NAMES = ("skills", "Skills")
_TOOL_DIRECTORY_NAME = "tools"

type ApplicationContentEntryKind = Literal["file"]
type _ManifestPath = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_APPLICATION_CONTENT_PATH_BYTES)
]
type _ManifestLength = Annotated[int, Field(ge=0, le=MAX_APPLICATION_CONTENT_FILE_BYTES)]


class _HashWriter(Protocol):
    """The narrow SHA-256 writer surface used by canonical framing."""

    def update(self, data: bytes) -> None: ...


class ApplicationContentManifestError(ValueError):
    """The application-content manifest or its selected filesystem tree is unsafe."""


class ApplicationContentManifestSerializationError(ApplicationContentManifestError):
    """The application-content manifest cannot be represented as a bounded canonical value."""


class ApplicationContentManifestValidationError(ApplicationContentManifestError):
    """The application-content manifest does not describe the current application root."""


class _ApplicationContentManifestEntryPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    path: _ManifestPath
    kind: ApplicationContentEntryKind
    length: _ManifestLength


class _ApplicationContentManifestPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    version: Literal["fha_application_content_v2"]
    entries: list[_ApplicationContentManifestEntryPayload]
    runtime_projection: str | None = None


@dataclass(frozen=True, slots=True)
class ApplicationContentManifestEntry:
    """One regular application file selected for Foundry-hosted staging."""

    path: str
    kind: ApplicationContentEntryKind
    length: int

    @classmethod
    def create(
        cls,
        *,
        path: str,
        kind: ApplicationContentEntryKind = APPLICATION_CONTENT_ENTRY_KIND,
        length: int,
    ) -> ApplicationContentManifestEntry:
        normalized_path = _normalize_relative_path(path)
        if kind != APPLICATION_CONTENT_ENTRY_KIND:
            raise ApplicationContentManifestValidationError(
                "Application-content manifest entry kind is unsupported."
            )
        if isinstance(length, bool) or not isinstance(length, int):
            raise ApplicationContentManifestValidationError(
                "Application-content manifest entry length must be an integer."
            )
        if length < 0 or length > MAX_APPLICATION_CONTENT_FILE_BYTES:
            raise ApplicationContentManifestValidationError(
                "Application-content manifest entry length exceeds the supported bound."
            )
        return cls(path=normalized_path, kind=kind, length=length)


@dataclass(frozen=True, slots=True)
class ApplicationContentManifest:
    """A bounded, canonical selection of application files for Foundry staging."""

    version: str
    entries: tuple[ApplicationContentManifestEntry, ...]
    runtime_projection: str | None = None

    @classmethod
    def create(
        cls,
        *,
        entries: Sequence[ApplicationContentManifestEntry],
        runtime_projection: str | None = None,
        version: str = APPLICATION_CONTENT_MANIFEST_VERSION,
    ) -> ApplicationContentManifest:
        if version != APPLICATION_CONTENT_MANIFEST_VERSION:
            raise ApplicationContentManifestValidationError(
                "Application-content manifest version is unsupported."
            )
        if len(entries) > MAX_APPLICATION_CONTENT_ENTRY_COUNT:
            raise ApplicationContentManifestSerializationError(
                "Application-content manifest has too many entries."
            )

        normalized_entries: list[ApplicationContentManifestEntry] = []
        paths: set[str] = set()
        casefolded_paths: dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, ApplicationContentManifestEntry):
                raise ApplicationContentManifestValidationError(
                    "Application-content manifest entries must be typed."
                )
            normalized_entry = ApplicationContentManifestEntry.create(
                path=entry.path,
                kind=entry.kind,
                length=entry.length,
            )
            if normalized_entry.path in paths:
                raise ApplicationContentManifestValidationError(
                    "Application-content manifest contains duplicate paths."
                )
            paths.add(normalized_entry.path)
            _register_casefolded_path(normalized_entry.path, casefolded_paths)
            normalized_entries.append(normalized_entry)

        manifest = cls(
            version=version,
            entries=tuple(sorted(normalized_entries, key=lambda entry: entry.path)),
            runtime_projection=_validate_runtime_projection(runtime_projection),
        )
        _ensure_manifest_size(_render_manifest(manifest))
        return manifest


def build_application_content_manifest(application_root: Path) -> ApplicationContentManifest:
    """Build the bounded canonical manifest for a resolved Functions application root."""
    root = _validated_application_root(application_root)
    entries = [
        ApplicationContentManifestEntry.create(
            path=relative_path,
            length=entry_stat.st_size,
        )
        for relative_path, entry_stat in _scan_application_tree(root)
    ]
    return ApplicationContentManifest.create(entries=entries)


def serialize_application_content_manifest(manifest: ApplicationContentManifest) -> str:
    """Serialize one manifest as its only accepted compact UTF-8 JSON form."""
    checked_manifest = ApplicationContentManifest.create(
        entries=manifest.entries,
        runtime_projection=manifest.runtime_projection,
        version=manifest.version,
    )
    serialized = _render_manifest(checked_manifest)
    _ensure_manifest_size(serialized)
    return serialized


def parse_application_content_manifest(payload: bytes | str) -> ApplicationContentManifest:
    """Parse and require the exact canonical serialization of a staged manifest."""
    try:
        text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        _ensure_manifest_size(text)
        document = _ApplicationContentManifestPayload.model_validate(decode_json_object(text))
        manifest = ApplicationContentManifest.create(
            version=document.version,
            entries=tuple(
                ApplicationContentManifestEntry.create(
                    path=entry.path,
                    kind=entry.kind,
                    length=entry.length,
                )
                for entry in document.entries
            ),
            runtime_projection=document.runtime_projection,
        )
        if text != serialize_application_content_manifest(manifest):
            raise ApplicationContentManifestSerializationError(
                "Application-content manifest is not canonically serialized."
            )
        return manifest
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        DuplicateJsonKeyError,
        ValidationError,
        TypeError,
        ValueError,
    ):
        raise ApplicationContentManifestValidationError(
            "Application-content manifest is invalid."
        ) from None


def validate_application_content_manifest(
    application_root: Path,
    manifest: ApplicationContentManifest | bytes | str,
) -> ApplicationContentManifest:
    """Require a staged manifest to match every selected regular file under the root."""
    checked_manifest = _coerce_manifest(manifest)
    current_manifest = build_application_content_manifest(application_root)
    if (
        current_manifest.version != checked_manifest.version
        or current_manifest.entries != checked_manifest.entries
    ):
        raise ApplicationContentManifestValidationError(
            "Application-content manifest does not match the application root."
        )
    return checked_manifest


def compute_application_content_digest(
    application_root: Path,
    manifest: ApplicationContentManifest | bytes | str | None = None,
) -> str:
    """Hash one validated manifest with its selected file bytes and no filesystem metadata."""
    root = _validated_application_root(application_root)
    checked_manifest = (
        build_application_content_manifest(root)
        if manifest is None
        else validate_application_content_manifest(root, manifest)
    )
    hasher = hashlib.sha256()
    _update_length_prefixed(hasher, APPLICATION_CONTENT_MANIFEST_VERSION.encode("utf-8"))
    _update_u32(hasher, len(checked_manifest.entries))
    for entry in checked_manifest.entries:
        _update_length_prefixed(hasher, entry.path.encode("utf-8"))
        _update_length_prefixed(hasher, entry.kind.encode("ascii"))
        _update_u64(hasher, entry.length)
        _update_file_bytes(hasher, root, entry)
    if checked_manifest.runtime_projection is not None:
        _update_length_prefixed(hasher, b"fha_runtime_projection")
        _update_length_prefixed(
            hasher,
            checked_manifest.runtime_projection.encode("utf-8"),
        )
    final_manifest = build_application_content_manifest(root)
    if (
        final_manifest.version != checked_manifest.version
        or final_manifest.entries != checked_manifest.entries
    ):
        raise ApplicationContentManifestValidationError(
            "Application-content tree changed while it was hashed."
        )
    return f"{APPLICATION_CONTENT_DIGEST_PREFIX}{hasher.hexdigest()}"


def validate_sha256_digest(value: str) -> str:
    """Validate the canonical SHA-256 digest form used by FHA binding fields."""
    if not isinstance(value, str) or _SHA256_DIGEST_PATTERN.fullmatch(value) is None:
        raise ApplicationContentManifestValidationError(
            "Application-content digest is not a canonical SHA-256 digest."
        )
    return value


def _coerce_manifest(
    manifest: ApplicationContentManifest | bytes | str,
) -> ApplicationContentManifest:
    if isinstance(manifest, ApplicationContentManifest):
        return ApplicationContentManifest.create(
            entries=manifest.entries,
            runtime_projection=manifest.runtime_projection,
            version=manifest.version,
        )
    return parse_application_content_manifest(manifest)


def _render_manifest(manifest: ApplicationContentManifest) -> str:
    payload: Mapping[str, object] = {
        "version": manifest.version,
        "entries": [
            {
                "path": entry.path,
                "kind": entry.kind,
                "length": entry.length,
            }
            for entry in manifest.entries
        ],
    }
    if manifest.runtime_projection is not None:
        payload = {
            **payload,
            "runtime_projection": manifest.runtime_projection,
        }
    return json.dumps(
        payload, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def _ensure_manifest_size(serialized: str) -> None:
    try:
        size = len(serialized.encode("utf-8"))
    except UnicodeEncodeError:
        raise ApplicationContentManifestSerializationError(
            "Application-content manifest must be valid UTF-8."
        ) from None
    if size > MAX_APPLICATION_CONTENT_MANIFEST_BYTES:
        raise ApplicationContentManifestSerializationError(
            "Application-content manifest exceeds the binding size bound."
        )


def _validate_runtime_projection(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise ApplicationContentManifestValidationError(
            "Application-content runtime projection is invalid."
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ApplicationContentManifestValidationError(
            "Application-content runtime projection must be valid UTF-8."
        ) from None
    return value


def _validated_application_root(application_root: Path) -> Path:
    root = Path(os.path.abspath(application_root))
    try:
        root_stat = os.lstat(root)
    except OSError:
        raise ApplicationContentManifestValidationError(
            "Application-content root is unavailable."
        ) from None
    _reject_link_or_reparse(root_stat)
    if not stat_module.S_ISDIR(root_stat.st_mode):
        raise ApplicationContentManifestValidationError(
            "Application-content root must be a directory."
        )
    return root


def _scan_application_tree(root: Path) -> list[tuple[str, os.stat_result]]:
    selected_paths = _select_semantic_application_paths(root)
    scanned_files: list[tuple[str, os.stat_result]] = []
    casefolded_paths: dict[str, str] = {}
    for relative_path in sorted(selected_paths, key=str):
        normalized_path = _normalize_relative_path(relative_path.as_posix())
        _register_casefolded_path(normalized_path, casefolded_paths)
        _, entry_stat = _validated_file_path(root, normalized_path)
        if entry_stat.st_size > MAX_APPLICATION_CONTENT_FILE_BYTES:
            raise ApplicationContentManifestValidationError(
                "Application-content file exceeds the supported size bound."
            )
        scanned_files.append((normalized_path, entry_stat))
    return scanned_files


def _select_semantic_application_paths(root: Path) -> set[PurePosixPath]:
    _validate_unselected_tree_safety(root)
    selected_paths: set[PurePosixPath] = set()
    _select_root_agent_files(root, selected_paths)
    for filename in _SEMANTIC_FILE_NAMES:
        _select_optional_file(root, root / filename, selected_paths)

    agents_directory = _select_first_directory(root, _AGENT_DIRECTORY_NAMES)
    if agents_directory is not None:
        _select_root_agent_files(agents_directory, selected_paths, root=root)

    tools_directory = _select_optional_directory(root, root / _TOOL_DIRECTORY_NAME)
    tool_python_paths: set[PurePosixPath] = set()
    if tools_directory is not None:
        tool_paths = _select_directory_tree(root, tools_directory, selected_paths)
        tool_python_paths.update(path for path in tool_paths if path.suffix == ".py")

    skills_directory = _select_first_directory(root, _SKILL_DIRECTORY_NAMES)
    if skills_directory is not None:
        _select_directory_tree(root, skills_directory, selected_paths)

    _select_tool_import_dependencies(root, tool_python_paths, selected_paths)
    return selected_paths


def _select_root_agent_files(
    directory: Path,
    selected_paths: set[PurePosixPath],
    *,
    root: Path | None = None,
) -> None:
    application_root = directory if root is None else root
    for child in _directory_children(directory):
        if child.name.startswith(".") or not _is_recognized_agent_filename(child.name):
            continue
        _select_optional_file(application_root, Path(child.path), selected_paths)


def _is_recognized_agent_filename(filename: str) -> bool:
    lowered = filename.casefold()
    return lowered in {"agent.md", "claude.md"} or lowered.endswith(
        (".agent.md", ".claude.md")
    )


def _select_first_directory(root: Path, names: Sequence[str]) -> Path | None:
    for name in names:
        directory = _select_optional_directory(root, root / name)
        if directory is not None:
            return directory
    return None


def _select_optional_directory(root: Path, path: Path) -> Path | None:
    try:
        entry_stat = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise ApplicationContentManifestValidationError(
            "Application-content entry could not be inspected."
        ) from None
    _reject_link_or_reparse(entry_stat)
    if not stat_module.S_ISDIR(entry_stat.st_mode):
        raise ApplicationContentManifestValidationError(
            "Application-content semantic directory is not a directory."
        )
    try:
        path.relative_to(root)
    except ValueError:
        raise ApplicationContentManifestValidationError(
            "Application-content path escapes the application root."
        ) from None
    return path


def _select_optional_file(
    root: Path,
    path: Path,
    selected_paths: set[PurePosixPath],
) -> PurePosixPath | None:
    try:
        entry_stat = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise ApplicationContentManifestValidationError(
            "Application-content entry could not be inspected."
        ) from None
    _reject_link_or_reparse(entry_stat)
    if not stat_module.S_ISREG(entry_stat.st_mode):
        raise ApplicationContentManifestValidationError(
            "Application-content semantic path is not a regular file."
        )
    try:
        relative_path = PurePosixPath(path.relative_to(root).as_posix())
    except ValueError:
        raise ApplicationContentManifestValidationError(
            "Application-content path escapes the application root."
        ) from None
    normalized_path = _normalize_relative_path(relative_path.as_posix())
    _validated_file_path(root, normalized_path)
    selected_paths.add(PurePosixPath(normalized_path))
    return PurePosixPath(normalized_path)


def _select_directory_tree(
    root: Path,
    directory: Path,
    selected_paths: set[PurePosixPath],
) -> set[PurePosixPath]:
    added_paths: set[PurePosixPath] = set()
    directories = [directory]
    while directories:
        current_directory = directories.pop()
        for child in _directory_children(current_directory):
            if child.name.casefold() in _SEMANTIC_DIRECTORY_IGNORES:
                continue
            if child.name.casefold() in _DEPLOYMENT_ONLY_FILE_NAMES:
                continue
            path = Path(child.path)
            try:
                entry_stat = child.stat(follow_symlinks=False)
            except OSError:
                raise ApplicationContentManifestValidationError(
                    "Application-content entry could not be inspected."
                ) from None
            _reject_link_or_reparse(entry_stat)
            if stat_module.S_ISDIR(entry_stat.st_mode):
                directories.append(path)
                continue
            if not stat_module.S_ISREG(entry_stat.st_mode):
                raise ApplicationContentManifestValidationError(
                    "Application-content tree contains a non-regular entry."
                )
            selected_path = _select_optional_file(root, path, selected_paths)
            if selected_path is not None:
                added_paths.add(selected_path)
    return added_paths


def _directory_children(directory: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as iterator:
            return sorted(iterator, key=lambda child: child.name)
    except OSError:
        raise ApplicationContentManifestValidationError(
            "Application-content root could not be scanned."
        ) from None


def _validate_unselected_tree_safety(root: Path) -> None:
    directories = [root]
    while directories:
        directory = directories.pop()
        for child in _directory_children(directory):
            path = Path(child.path)
            try:
                relative_path = PurePosixPath(path.relative_to(root).as_posix())
            except ValueError:
                raise ApplicationContentManifestValidationError(
                    "Application-content path escapes the application root."
                ) from None
            if _is_deployment_only_path(relative_path):
                continue
            try:
                entry_stat = child.stat(follow_symlinks=False)
            except OSError:
                raise ApplicationContentManifestValidationError(
                    "Application-content entry could not be inspected."
                ) from None
            _reject_link_or_reparse(entry_stat)
            if stat_module.S_ISDIR(entry_stat.st_mode):
                directories.append(path)
                continue
            if not stat_module.S_ISREG(entry_stat.st_mode):
                raise ApplicationContentManifestValidationError(
                    "Application-content tree contains a non-regular entry."
                )
            _reject_secret_path(relative_path.as_posix())


def _is_deployment_only_path(relative_path: PurePosixPath) -> bool:
    return (
        relative_path.name.casefold() in _DEPLOYMENT_ONLY_FILE_NAMES
        or any(
            component.casefold() in _SEMANTIC_DIRECTORY_IGNORES
            for component in relative_path.parts
        )
    )


def _reject_secret_path(path: str) -> None:
    components = path.split("/")
    if any(component.casefold().startswith(".env") for component in components):
        raise ApplicationContentManifestValidationError(
            "Application-content tree contains a secret path."
        )
    filename = components[-1].casefold()
    if (
        filename in _EXCLUDED_FILE_NAMES
        or filename.startswith("secret.")
        or filename.endswith(_EXCLUDED_FILE_SUFFIXES)
    ):
        raise ApplicationContentManifestValidationError(
            "Application-content tree contains a secret path."
        )


def _select_tool_import_dependencies(
    root: Path,
    tool_python_paths: set[PurePosixPath],
    selected_paths: set[PurePosixPath],
) -> None:
    pending_paths = list(tool_python_paths)
    parsed_paths: set[PurePosixPath] = set()
    while pending_paths:
        current_path = pending_paths.pop()
        if current_path in parsed_paths:
            continue
        parsed_paths.add(current_path)
        source_path, _ = _validated_file_path(root, current_path.as_posix())
        try:
            tree = ast.parse(source_path.read_bytes(), filename=current_path.as_posix())
        except (OSError, SyntaxError, ValueError):
            continue
        for module_path in _local_imported_module_paths(root, current_path, tree):
            if module_path in selected_paths:
                continue
            selected_paths.add(module_path)
            if module_path.suffix == ".py":
                pending_paths.append(module_path)
        for asset_path in _literal_asset_paths(root, current_path, tree):
            asset_stat = _validated_asset_path(root, asset_path)
            if asset_stat is None:
                continue
            if stat_module.S_ISDIR(asset_stat.st_mode):
                added_paths = _select_directory_tree(root, asset_path, selected_paths)
                pending_paths.extend(path for path in added_paths if path.suffix == ".py")
                continue
            selected_path = _select_optional_file(root, asset_path, selected_paths)
            if selected_path is not None and selected_path.suffix == ".py":
                pending_paths.append(selected_path)


def _local_imported_module_paths(
    root: Path,
    current_path: PurePosixPath,
    tree: ast.AST,
) -> set[PurePosixPath]:
    module_names: set[tuple[str, ...]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_names.update(
                tuple(alias.name.split("."))
                for alias in node.names
                if _is_local_module_name(alias.name)
            )
        elif isinstance(node, ast.ImportFrom):
            base_name = _import_from_base_name(root, current_path, node)
            if not base_name:
                continue
            module_names.add(base_name)
            module_names.update(
                (*base_name, alias.name)
                for alias in node.names
                if alias.name != "*" and _is_local_module_name(alias.name)
            )

    local_paths: set[PurePosixPath] = set()
    for module_name in module_names:
        local_paths.update(_resolve_local_module_paths(root, module_name))
    return local_paths


def _is_local_module_name(value: str) -> bool:
    return bool(value) and all(component.isidentifier() for component in value.split("."))


def _import_from_base_name(
    root: Path,
    current_path: PurePosixPath,
    node: ast.ImportFrom,
) -> tuple[str, ...]:
    if node.level == 0:
        return tuple(node.module.split(".")) if node.module and _is_local_module_name(node.module) else ()
    package_parts = _package_parts_for_path(root, current_path)
    parents_to_strip = node.level - 1
    if parents_to_strip > len(package_parts):
        return ()
    module_parts = (
        tuple(node.module.split("."))
        if node.module is not None and _is_local_module_name(node.module)
        else ()
    )
    return (*package_parts[: len(package_parts) - parents_to_strip], *module_parts)


def _package_parts_for_path(root: Path, relative_path: PurePosixPath) -> tuple[str, ...]:
    package_parts = list(relative_path.parts[:-1])
    while package_parts:
        init_path = root.joinpath(*package_parts, "__init__.py")
        try:
            entry_stat = os.lstat(init_path)
        except FileNotFoundError:
            return ()
        except OSError:
            return ()
        if not stat_module.S_ISREG(entry_stat.st_mode):
            return ()
        package_parts.pop()
    return tuple(relative_path.parts[:-1])


def _resolve_local_module_paths(
    root: Path,
    module_name: tuple[str, ...],
) -> set[PurePosixPath]:
    if not module_name:
        return set()
    base_path = root.joinpath(*module_name)
    candidates = [base_path.with_suffix(".py"), base_path / "__init__.py"]
    for index in range(1, len(module_name)):
        candidates.append(root.joinpath(*module_name[:index], "__init__.py"))
    resolved_paths: set[PurePosixPath] = set()
    for candidate in candidates:
        selected_path = _optional_regular_module_path(root, candidate)
        if selected_path is not None:
            resolved_paths.add(selected_path)
    return resolved_paths


def _optional_regular_module_path(root: Path, path: Path) -> PurePosixPath | None:
    try:
        entry_stat = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise ApplicationContentManifestValidationError(
            "Application-content entry could not be inspected."
        ) from None
    _reject_link_or_reparse(entry_stat)
    if not stat_module.S_ISREG(entry_stat.st_mode):
        return None
    try:
        relative_path = PurePosixPath(path.relative_to(root).as_posix())
    except ValueError:
        raise ApplicationContentManifestValidationError(
            "Application-content path escapes the application root."
        ) from None
    normalized_path = _normalize_relative_path(relative_path.as_posix())
    _validated_file_path(root, normalized_path)
    return PurePosixPath(normalized_path)


def _literal_asset_paths(
    root: Path,
    current_path: PurePosixPath,
    tree: ast.AST,
) -> set[Path]:
    asset_paths: set[Path] = set()
    for node in ast.walk(tree):
        resolved = _resolve_literal_path_expression(node, current_path)
        if resolved is None or not resolved[1]:
            continue
        candidate = root.joinpath(*resolved[0].parts)
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        relative_path = PurePosixPath(candidate.relative_to(root).as_posix())
        if _contains_semantic_ignored_component(relative_path):
            continue
        if _validated_asset_path(root, candidate) is not None:
            asset_paths.add(candidate)
    return asset_paths


def _validated_asset_path(root: Path, path: Path) -> os.stat_result | None:
    try:
        relative_path = PurePosixPath(path.relative_to(root).as_posix())
    except ValueError:
        return None
    if _contains_semantic_ignored_component(relative_path):
        return None
    current = root
    for index, component in enumerate(relative_path.parts):
        current = current / component
        try:
            entry_stat = os.lstat(current)
        except FileNotFoundError:
            return None
        except OSError:
            raise ApplicationContentManifestValidationError(
                "Application-content entry could not be inspected."
            ) from None
        _reject_link_or_reparse(entry_stat)
        if index < len(relative_path.parts) - 1:
            if not stat_module.S_ISDIR(entry_stat.st_mode):
                return None
            continue
        if not stat_module.S_ISDIR(entry_stat.st_mode) and not stat_module.S_ISREG(
            entry_stat.st_mode
        ):
            raise ApplicationContentManifestValidationError(
                "Application-content tree contains a non-regular entry."
            )
        return entry_stat
    return None


def _resolve_literal_path_expression(
    node: ast.AST,
    current_path: PurePosixPath,
) -> tuple[PurePosixPath, bool] | None:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Path":
        if len(node.args) == 1 and isinstance(node.args[0], ast.Name) and node.args[0].id == "__file__":
            return current_path, False
        return None
    if isinstance(node, ast.Attribute):
        resolved = _resolve_literal_path_expression(node.value, current_path)
        if resolved is None or node.attr != "parent":
            return None
        return resolved[0].parent, resolved[1]
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute):
        if node.value.attr != "parents" or not isinstance(node.slice, ast.Constant):
            return None
        resolved = _resolve_literal_path_expression(node.value.value, current_path)
        if resolved is None or not isinstance(node.slice.value, int) or node.slice.value < 0:
            return None
        path = resolved[0]
        for _ in range(node.slice.value + 1):
            path = path.parent
        return path, resolved[1]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        resolved = _resolve_literal_path_expression(node.left, current_path)
        literal = node.right.value if isinstance(node.right, ast.Constant) else None
        if resolved is None or not isinstance(literal, str):
            return None
        appended = _append_literal_path(resolved[0], literal)
        return (appended, True) if appended is not None else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        resolved = _resolve_literal_path_expression(node.func.value, current_path)
        literal_arguments = [
            argument.value
            for argument in node.args
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
        ]
        if resolved is None:
            return None
        if node.func.attr == "resolve" and not node.args:
            return resolved
        if node.func.attr == "with_name" and len(literal_arguments) == 1:
            appended = _append_literal_path(resolved[0].parent, literal_arguments[0])
            return (appended, True) if appended is not None else None
        if node.func.attr == "joinpath" and literal_arguments:
            path = resolved[0]
            for literal in literal_arguments:
                appended = _append_literal_path(path, literal)
                if appended is None:
                    return None
                path = appended
            return path, True
    return None


def _append_literal_path(base_path: PurePosixPath, literal: str) -> PurePosixPath | None:
    candidate = PurePosixPath(literal)
    if (
        not literal
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        return None
    return base_path.joinpath(*candidate.parts)


def _contains_semantic_ignored_component(relative_path: PurePosixPath) -> bool:
    return any(
        component.casefold() in _SEMANTIC_DIRECTORY_IGNORES
        for component in relative_path.parts[:-1]
    )


def _normalize_relative_path(path: str) -> str:
    if not isinstance(path, str):
        raise ApplicationContentManifestValidationError(
            "Application-content manifest path must be a string."
        )
    if not path or "\x00" in path or "\\" in path:
        raise ApplicationContentManifestValidationError(
            "Application-content manifest path is not a normalized POSIX relative path."
        )
    if path.startswith("/") or _WINDOWS_DRIVE_PATH_PATTERN.match(path):
        raise ApplicationContentManifestValidationError(
            "Application-content manifest path must be relative."
        )
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ApplicationContentManifestValidationError(
            "Application-content manifest path contains an unsafe component."
        )
    normalized_parts = tuple(unicodedata.normalize("NFC", part) for part in parts)
    normalized_path = "/".join(normalized_parts)
    if normalized_path != path:
        raise ApplicationContentManifestValidationError(
            "Application-content manifest path is not normalized."
        )
    try:
        if len(normalized_path.encode("utf-8")) > MAX_APPLICATION_CONTENT_PATH_BYTES:
            raise ApplicationContentManifestValidationError(
                "Application-content manifest path exceeds the supported bound."
            )
    except UnicodeEncodeError:
        raise ApplicationContentManifestValidationError(
            "Application-content manifest path must be valid UTF-8."
        ) from None
    _reject_excluded_path(normalized_path)
    return normalized_path


def _register_casefolded_path(path: str, casefolded_paths: dict[str, str]) -> None:
    parts = path.split("/")
    for index in range(1, len(parts) + 1):
        candidate = "/".join(parts[:index])
        key = candidate.casefold()
        existing = casefolded_paths.get(key)
        if existing is not None and existing != candidate:
            raise ApplicationContentManifestValidationError(
                "Application-content manifest contains a case-colliding path."
            )
        casefolded_paths[key] = candidate


def _reject_excluded_path(path: str) -> None:
    components = path.split("/")
    for component in components:
        lowered = component.casefold()
        if lowered in _EXCLUDED_PATH_COMPONENTS or lowered.startswith(".env"):
            raise ApplicationContentManifestValidationError(
                "Application-content manifest selects an excluded path."
            )
    filename = components[-1].casefold()
    if (
        filename in _EXCLUDED_FILE_NAMES
        or filename.startswith("secret.")
        or filename.endswith(_EXCLUDED_FILE_SUFFIXES)
    ):
        raise ApplicationContentManifestValidationError(
            "Application-content manifest selects an excluded file."
        )


def _reject_link_or_reparse(entry_stat: os.stat_result) -> None:
    attributes = getattr(entry_stat, "st_file_attributes", 0)
    if stat_module.S_ISLNK(entry_stat.st_mode) or attributes & _REPARSE_POINT_ATTRIBUTE:
        raise ApplicationContentManifestValidationError(
            "Application-content tree contains a link or reparse point."
        )


def _update_length_prefixed(hasher: _HashWriter, value: bytes) -> None:
    _update_u32(hasher, len(value))
    hasher.update(value)


def _update_u32(hasher: _HashWriter, value: int) -> None:
    if value < 0 or value >= 2**32:
        raise ApplicationContentManifestValidationError(
            "Application-content canonical field exceeds the supported bound."
        )
    hasher.update(value.to_bytes(4, "big"))


def _update_u64(hasher: _HashWriter, value: int) -> None:
    if value < 0 or value >= 2**64:
        raise ApplicationContentManifestValidationError(
            "Application-content canonical file length is invalid."
        )
    hasher.update(value.to_bytes(8, "big"))


def _update_file_bytes(
    hasher: _HashWriter,
    root: Path,
    entry: ApplicationContentManifestEntry,
) -> None:
    file_path, before_stat = _validated_file_path(root, entry.path)
    if before_stat.st_size != entry.length:
        raise ApplicationContentManifestValidationError(
            "Application-content file length does not match the manifest."
        )
    descriptor: int | None = None
    try:
        descriptor = os.open(file_path, os.O_RDONLY | _O_NOFOLLOW | _O_BINARY)
        descriptor_stat = os.fstat(descriptor)
        _reject_link_or_reparse(descriptor_stat)
        if (
            not stat_module.S_ISREG(descriptor_stat.st_mode)
            or descriptor_stat.st_size != entry.length
        ):
            raise ApplicationContentManifestValidationError(
                "Application-content file no longer matches the manifest."
            )
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = None
            remaining = entry.length
            while remaining:
                chunk = stream.read(min(_READ_CHUNK_SIZE, remaining))
                if not chunk:
                    raise ApplicationContentManifestValidationError(
                        "Application-content file ended before its declared length."
                    )
                hasher.update(chunk)
                remaining -= len(chunk)
            if stream.read(1):
                raise ApplicationContentManifestValidationError(
                    "Application-content file exceeded its declared length."
                )
    except OSError:
        raise ApplicationContentManifestValidationError(
            "Application-content file could not be read safely."
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        after_stat = os.lstat(file_path)
    except OSError:
        raise ApplicationContentManifestValidationError(
            "Application-content file changed while it was hashed."
        ) from None
    _reject_link_or_reparse(after_stat)
    if not stat_module.S_ISREG(after_stat.st_mode) or not _same_file_snapshot(
        before_stat,
        after_stat,
    ):
        raise ApplicationContentManifestValidationError(
            "Application-content file changed while it was hashed."
        )


def _validated_file_path(root: Path, relative_path: str) -> tuple[Path, os.stat_result]:
    current = root
    parts = relative_path.split("/")
    for index, component in enumerate(parts):
        current = current / component
        try:
            current_stat = os.lstat(current)
        except OSError:
            raise ApplicationContentManifestValidationError(
                "Application-content manifest references a missing file."
            ) from None
        _reject_link_or_reparse(current_stat)
        if index < len(parts) - 1:
            if not stat_module.S_ISDIR(current_stat.st_mode):
                raise ApplicationContentManifestValidationError(
                    "Application-content manifest path has an invalid parent."
                )
        elif not stat_module.S_ISREG(current_stat.st_mode):
            raise ApplicationContentManifestValidationError(
                "Application-content manifest references a non-regular file."
            )
    return current, current_stat


def _same_file_snapshot(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and before.st_ctime_ns == after.st_ctime_ns
    )
