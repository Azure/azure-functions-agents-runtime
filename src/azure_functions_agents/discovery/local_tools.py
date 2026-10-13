"""Discover Markdown-described local tools without executing customer code."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import frontmatter
import yaml  # type: ignore[import-untyped]
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from .._local_tool_descriptor import (
    LocalToolParameter,
    LocalToolSpec,
    PortableToolType,
)
from ..local_tools.providers import supported_source_files

type _NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_PARAMETER_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class _ParameterDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: _NonEmptyText
    type: PortableToolType
    description: _NonEmptyText

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not _PARAMETER_NAME_PATTERN.fullmatch(value):
            raise ValueError("must contain only letters, digits, and underscores")
        return value


class _ToolDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: _NonEmptyText
    description: _NonEmptyText
    entrypoint: _NonEmptyText | None = None
    parameters: list[_ParameterDocument] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not _TOOL_NAME_PATTERN.fullmatch(value):
            raise ValueError("must contain only letters, digits, dots, underscores, and hyphens")
        return value

    @model_validator(mode="after")
    def _validate_parameters(self) -> _ToolDocument:
        names = [parameter.name for parameter in self.parameters]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate parameter names: {duplicates}")
        return self


@dataclass(frozen=True)
class LocalToolDiscoveryResult:
    tools: tuple[LocalToolSpec, ...]
    failed_loads: tuple[tuple[str, str], ...]


def _contained_path(path: Path, app_root: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(app_root)
    except ValueError as exc:
        raise ValueError(f"{path} resolves outside the application root") from exc
    if not resolved.is_file():
        raise ValueError(f"{path} is not a regular file")
    return resolved


def _format_validation_error(exc: ValidationError) -> str:
    issue = max(exc.errors(), key=lambda error: len(error.get("loc", ())))
    location = ".".join(str(part) for part in issue.get("loc", ())) or "<root>"
    return f"field `{location}`: {issue.get('msg', str(exc))}"


def _load_spec(tool_dir: Path, app_root: Path) -> LocalToolSpec:
    metadata_path = _contained_path(tool_dir / "tool.md", app_root)
    source_candidates = supported_source_files(tool_dir)
    if len(source_candidates) != 1:
        raise ValueError(
            "expected exactly one non-hidden file with a supported lowercase extension, "
            f"found {len(source_candidates)}"
        )
    source_candidate, provider = source_candidates[0]
    source_path = _contained_path(source_candidate, app_root)

    try:
        post = frontmatter.load(str(metadata_path))
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML frontmatter: {exc}") from exc
    except Exception as exc:
        raise ValueError(f"failed to parse frontmatter: {exc}") from exc
    if post.content.strip():
        raise ValueError("Markdown body must be empty; use the `description` field")
    metadata: Any = post.metadata or {}
    if not isinstance(metadata, dict):
        raise ValueError("frontmatter must be a mapping")
    try:
        document = _ToolDocument.model_validate(metadata)
    except ValidationError as exc:
        raise ValueError(_format_validation_error(exc)) from exc

    if provider.requires_explicit_entrypoint and document.entrypoint is None:
        raise ValueError("field `entrypoint`: is required for Go tools")
    entrypoint = document.entrypoint or document.name
    parameters = tuple(
        LocalToolParameter(
            name=parameter.name,
            type=parameter.type,
            description=parameter.description,
        )
        for parameter in document.parameters
    )
    spec = LocalToolSpec.create(
        name=document.name,
        description=document.description,
        entrypoint=entrypoint,
        source_path=source_path,
        metadata_path=metadata_path,
        language=provider.language,
        parameters=parameters,
    )
    provider.validate_spec(spec)
    return spec


def discover_local_tools(app_root: Path) -> LocalToolDiscoveryResult:
    """Discover direct-child process-backed tool units under ``tools/``."""
    resolved_root = app_root.resolve()
    tools_dir = resolved_root / "tools"
    if not tools_dir.is_dir():
        return LocalToolDiscoveryResult((), ())

    discovered: list[LocalToolSpec] = []
    failures: list[tuple[str, str]] = []
    for candidate in sorted(path for path in tools_dir.iterdir() if path.is_dir()):
        if not (candidate / "tool.md").exists():
            continue
        try:
            discovered.append(_load_spec(candidate, resolved_root))
        except Exception as exc:
            failures.append((str(candidate), f"{type(exc).__name__}: {exc}"))
    return LocalToolDiscoveryResult(tuple(discovered), tuple(failures))
