"""Stage a secret-free Hosted Agent Responses source artifact."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Self

from .. import __version__ as _runtime_version
from .fha_resilient_responses_entrypoint import render_fha_hosted_responses_entrypoint
from .fha_runtime_projection import (
    FHA_RUNTIME_PROJECTION_FILENAME,
    FhaRuntimeProjection,
    serialize_fha_runtime_projection,
)

FHA_HOSTED_ENTRYPOINT_FILENAME = "fha_hosted_responses_entrypoint.py"
FHA_HOSTED_REQUIREMENTS_FILENAME = "requirements.txt"
_FHA_DENIED_DIRECTORY_NAMES = frozenset(
    {
        ".azure",
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".venv",
        "__pycache__",
        "env",
        "venv",
    }
)
_FHA_DENIED_SUFFIXES = frozenset({".cer", ".crt", ".der", ".key", ".pem", ".pfx", ".p12"})
_FHA_PINNED_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!-]*$")
_FHA_REQUIREMENT_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9_,.-]+\])?"
    r"(?:\s*(?:===|==|!=|~=|>=|<=|>|<)\s*[A-Za-z0-9*+!._-]+"
    r"(?:\s*,\s*(?:===|==|!=|~=|>=|<=|>|<)\s*[A-Za-z0-9*+!._-]+)*)?"
    r"(?:\s*;\s*[A-Za-z0-9_ .<>=!\"'()-]+)?$"
)
_FHA_GENERATED_DISTRIBUTIONS = frozenset(
    {
        "azure-ai-agentserver-core",
        "azure-ai-agentserver-responses",
        "azurefunctions-agents-runtime",
    }
)
_FHA_RUNTIME_REQUIREMENT_PATTERN = re.compile(
    r"^azurefunctions-agents-runtime(?:\[[A-Za-z0-9_,.-]+\])?=="
    r"(?P<version>[A-Za-z0-9*+!._-]+)$",
    re.IGNORECASE,
)
_FHA_RUNTIME_WHEEL_PATTERN = re.compile(
    r"^(?:\./)?wheels/azurefunctions_agents_runtime-"
    r"(?P<version>[A-Za-z0-9+!._-]+)-py3-none-any\.whl"
    r"(?:\[[A-Za-z0-9_,.-]+\])?$",
    re.IGNORECASE,
)
_MAX_APPLICATION_REQUIREMENTS_BYTES = 64 * 1024


class FhaHostedSourceStagingError(ValueError):
    """The selected source cannot safely form a hosted-agent artifact."""


@dataclass(frozen=True, slots=True)
class FhaHostedDependencyPins:
    """Exact hosted-agent package requirements emitted with staged source."""

    runtime: str
    agentserver_core: str
    agentserver_responses: str

    @classmethod
    def create(
        cls,
        *,
        runtime: str,
        agentserver_core: str,
        agentserver_responses: str,
    ) -> Self:
        """Create pins that keep the generated host on known dependency versions."""
        return cls(
            runtime=_require_exact_pin(runtime, "azurefunctions-agents-runtime"),
            agentserver_core=_require_exact_pin(agentserver_core, "azure-ai-agentserver-core"),
            agentserver_responses=_require_exact_pin(
                agentserver_responses,
                "azure-ai-agentserver-responses",
            ),
        )

    def render(self, additional_requirements: Iterable[str] = ()) -> str:
        """Render the generated, secret-free requirements artifact."""
        return "\n".join(
            (
                self.runtime,
                self.agentserver_core,
                self.agentserver_responses,
                *tuple(additional_requirements),
                "",
            )
        )


@dataclass(frozen=True, slots=True)
class FhaHostedSourceArtifact:
    """Paths and manifest inputs produced by source-only hosted staging."""

    stage_root: Path
    entrypoint_path: Path
    projection_path: Path
    requirements_path: Path
    selected_relative_paths: tuple[PurePosixPath, ...]
    rendered_entrypoint: str


def resolve_fha_runtime_pin(
    application_root: Path,
    override: str | None = None,
) -> str:
    """Resolve the hosted runtime pin from an override or application requirements."""
    if override is not None:
        return _require_exact_pin(override, "azurefunctions-agents-runtime")
    requirements_path = Path(application_root) / FHA_HOSTED_REQUIREMENTS_FILENAME
    if not requirements_path.is_file():
        return f"azurefunctions-agents-runtime=={_runtime_version}"
    try:
        lines = requirements_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise FhaHostedSourceStagingError(
            "Application requirements could not be read safely."
        ) from exc
    versions = {
        match.group("version")
        for line in lines
        if (match := _match_runtime_requirement(line.strip())) is not None
    }
    if len(versions) > 1:
        raise FhaHostedSourceStagingError(
            "Application requirements contain conflicting runtime versions."
        )
    version = next(iter(versions), _runtime_version)
    return f"azurefunctions-agents-runtime=={version}"


def stage_fha_hosted_source(
    *,
    application_root: Path,
    stage_root: Path,
    selected_relative_paths: Iterable[str],
    dependency_pins: FhaHostedDependencyPins,
    projection: FhaRuntimeProjection,
) -> FhaHostedSourceArtifact:
    """Copy explicit non-secret inputs and generate the hosted entrypoint files."""
    source_candidate = Path(application_root)
    destination_candidate = Path(stage_root)
    if source_candidate.is_symlink() or destination_candidate.is_symlink():
        raise FhaHostedSourceStagingError("Hosted source roots must not be links.")
    source_root = source_candidate.resolve()
    destination_root = destination_candidate.resolve()
    _validate_stage_roots(source_root, destination_root)
    relative_paths = _normalize_selected_paths(selected_relative_paths)
    rendered_entrypoint = render_fha_hosted_responses_entrypoint()
    try:
        projection_bytes = serialize_fha_runtime_projection(projection).encode("utf-8")
    except (AttributeError, TypeError, ValueError):
        raise FhaHostedSourceStagingError(
            "Generated FHA runtime projection is invalid."
        ) from None

    destination_root.mkdir(parents=True, exist_ok=True)
    if any(destination_root.iterdir()):
        raise FhaHostedSourceStagingError("Hosted source staging directory must be empty.")

    application_requirements: tuple[str, ...] = ()
    for relative_path in relative_paths:
        source_path = source_root.joinpath(*relative_path.parts)
        _validate_source_file(source_root, source_path, relative_path)
        if relative_path == PurePosixPath(FHA_HOSTED_REQUIREMENTS_FILENAME):
            application_requirements = _read_application_requirements(source_path)
            continue
        destination_path = destination_root.joinpath(*relative_path.parts)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_bytes(source_path.read_bytes())

    entrypoint_path = destination_root / FHA_HOSTED_ENTRYPOINT_FILENAME
    projection_path = destination_root / FHA_RUNTIME_PROJECTION_FILENAME
    requirements_path = destination_root / FHA_HOSTED_REQUIREMENTS_FILENAME
    entrypoint_path.write_bytes(rendered_entrypoint.encode("utf-8"))
    projection_path.write_bytes(projection_bytes)
    requirements_path.write_text(
        dependency_pins.render(application_requirements),
        encoding="utf-8",
    )
    return FhaHostedSourceArtifact(
        stage_root=destination_root,
        entrypoint_path=entrypoint_path,
        projection_path=projection_path,
        requirements_path=requirements_path,
        selected_relative_paths=relative_paths,
        rendered_entrypoint=rendered_entrypoint,
    )


def _require_exact_pin(requirement: str, distribution: str) -> str:
    normalized = requirement.strip()
    prefix = f"{distribution}=="
    if (
        not normalized.startswith(prefix)
        or normalized == prefix
        or any(character.isspace() for character in normalized)
        or _FHA_PINNED_VERSION_PATTERN.fullmatch(normalized.removeprefix(prefix)) is None
    ):
        raise FhaHostedSourceStagingError(
            f"Hosted source requires an exact pin for {distribution}."
        )
    return normalized


def _validate_stage_roots(source_root: Path, destination_root: Path) -> None:
    if not source_root.is_dir():
        raise FhaHostedSourceStagingError("Hosted source application root is unavailable.")
    if source_root == destination_root or _is_within(destination_root, source_root):
        raise FhaHostedSourceStagingError(
            "Hosted source staging directory must be outside the application root."
        )
    if destination_root.exists() and not destination_root.is_dir():
        raise FhaHostedSourceStagingError("Hosted source staging path is not a directory.")


def _normalize_selected_paths(paths: Iterable[str]) -> tuple[PurePosixPath, ...]:
    normalized: list[PurePosixPath] = []
    seen: set[PurePosixPath] = set()
    for raw_path in paths:
        if not isinstance(raw_path, str) or not raw_path or "\\" in raw_path:
            raise FhaHostedSourceStagingError("Hosted source path must be a relative POSIX path.")
        relative_path = PurePosixPath(raw_path)
        if relative_path.is_absolute() or ".." in relative_path.parts or relative_path == PurePosixPath("."):
            raise FhaHostedSourceStagingError("Hosted source path escapes the application root.")
        if relative_path in {
            PurePosixPath(FHA_HOSTED_ENTRYPOINT_FILENAME),
            PurePosixPath(FHA_RUNTIME_PROJECTION_FILENAME),
        }:
            raise FhaHostedSourceStagingError("Hosted source path conflicts with a generated artifact.")
        if relative_path in seen:
            raise FhaHostedSourceStagingError("Hosted source paths must be unique.")
        seen.add(relative_path)
        normalized.append(relative_path)
    return tuple(sorted(normalized, key=str))


def _validate_source_file(
    source_root: Path,
    source_path: Path,
    relative_path: PurePosixPath,
) -> None:
    if _is_denied_path(relative_path):
        raise FhaHostedSourceStagingError("Hosted source selection contains a secret or cache path.")
    if not _is_within(source_path, source_root):
        raise FhaHostedSourceStagingError("Hosted source path escapes the application root.")
    if _contains_link(source_root, relative_path) or source_path.is_symlink():
        raise FhaHostedSourceStagingError("Hosted source selection must not follow links.")
    if not source_path.is_file():
        raise FhaHostedSourceStagingError("Hosted source selection must contain files.")


def _read_application_requirements(path: Path) -> tuple[str, ...]:
    try:
        payload = path.read_bytes()
        if len(payload) > _MAX_APPLICATION_REQUIREMENTS_BYTES:
            raise FhaHostedSourceStagingError(
                "Application requirements exceed the supported size."
            )
        text = payload.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise FhaHostedSourceStagingError(
            "Application requirements could not be read safely."
        ) from exc

    requirements: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if _match_runtime_requirement(line) is not None:
            continue
        if (
            line.startswith("-")
            or "@" in line
            or "/" in line
            or "\\" in line
            or "://" in line
            or "${" in line
            or "%" in line
            or _FHA_REQUIREMENT_PATTERN.fullmatch(line) is None
        ):
            raise FhaHostedSourceStagingError(
                "Application requirements contain an unsupported dependency form."
            )
        distribution = re.split(r"[\[<>=!~; ]", line, maxsplit=1)[0]
        canonical_distribution = re.sub(r"[-_.]+", "-", distribution).casefold()
        if canonical_distribution in _FHA_GENERATED_DISTRIBUTIONS:
            if canonical_distribution == "azurefunctions-agents-runtime":
                continue
            raise FhaHostedSourceStagingError(
                "Application requirements conflict with hosted runtime dependencies."
            )
        if line not in seen:
            requirements.append(line)
            seen.add(line)
    return tuple(requirements)


def _match_runtime_requirement(line: str) -> re.Match[str] | None:
    return _FHA_RUNTIME_REQUIREMENT_PATTERN.fullmatch(
        line
    ) or _FHA_RUNTIME_WHEEL_PATTERN.fullmatch(line)


def _is_denied_path(relative_path: PurePosixPath) -> bool:
    lowered_parts = tuple(part.lower() for part in relative_path.parts)
    if any(part in _FHA_DENIED_DIRECTORY_NAMES for part in lowered_parts[:-1]):
        return True
    filename = relative_path.name.lower()
    return (
        filename == "local.settings.json"
        or filename == ".env"
        or filename.startswith(".env.")
        or PurePosixPath(filename).suffix in _FHA_DENIED_SUFFIXES
    )


def _contains_link(source_root: Path, relative_path: PurePosixPath) -> bool:
    current = source_root
    for part in relative_path.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
