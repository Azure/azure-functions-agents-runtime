"""Compile and validate the versioned Foundry Hosted Agent V0 profile."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .._slug import _is_single_agent_file
from ..project_composition import ProjectComposition, compose_project
from ..registration.catalog import AgentCatalog, CatalogEntry
from ..strict_json import DuplicateJsonKeyError, decode_json_object
from .fha_runtime_projection import (
    FhaProjectionCapabilities,
    FhaProjectionCatalogEntry,
    FhaProjectionMcpServer,
    FhaRuntimeProjection,
    FhaRuntimeProjectionError,
    validate_fha_runtime_projection_match,
)

_FHA_ALLOWED_TRIGGER_TYPES = frozenset({"http_trigger", "service_bus_queue_trigger"})
_MCP_CONFIG_NAME = "mcp.json"
_GLOBAL_CONFIG_NAME = "agents.config.yaml"
_MCP_SERVER_FIELDS = frozenset({"type", "url", "tools", "auth", "headers"})
_MCP_AUTH_FIELDS = frozenset({"scope", "client_id"})
_RAW_DOLLAR_PLACEHOLDER = re.compile(r"(?<!\$)\$[A-Za-z_][A-Za-z0-9_]*")
_RAW_PERCENT_PLACEHOLDER = re.compile(r"(?<!%)%[A-Za-z_][A-Za-z0-9_]*%")


class FhaV0CatalogError(ValueError):
    """The hosted catalog contains a capability outside the FHA V0 profile."""


FhaModelCatalogError = FhaV0CatalogError


@dataclass(frozen=True, slots=True)
class FhaV0ProjectCompilation:
    """The shared composition, validated catalog, and canonical V0 projection."""

    composition: ProjectComposition
    catalog: AgentCatalog
    projection: FhaRuntimeProjection


def compile_fha_v0_project(
    application_root: Path,
    *,
    project_endpoint: str,
    default_model: str,
    expected_projection: FhaRuntimeProjection | bytes | str | None = None,
) -> FhaV0ProjectCompilation:
    """Compile one substitution-free FHA V0 project through the shared pipeline."""
    root = _require_application_root(application_root)
    mcp_servers = validate_fha_v0_raw_authoring(root)
    composition = compose_project(
        root,
        strict=True,
        refresh_discovery=True,
        default_model=default_model,
    )
    validate_fha_v0_catalog(
        composition.catalog,
        composition=composition,
        mcp_servers=mcp_servers,
    )
    projection = _build_runtime_projection(
        composition,
        project_endpoint=project_endpoint,
        default_model=default_model,
        mcp_servers=mcp_servers,
    )
    if expected_projection is not None:
        validate_fha_runtime_projection_match(projection, expected_projection)
    return FhaV0ProjectCompilation(
        composition=composition,
        catalog=composition.catalog,
        projection=projection,
    )


def validate_fha_v0_catalog(
    catalog: AgentCatalog,
    *,
    composition: ProjectComposition | None = None,
    mcp_servers: Sequence[FhaProjectionMcpServer] | None = None,
) -> None:
    """Fail closed unless a catalog contains only FHA V0-supported capabilities."""
    safe_mcp_names = (
        frozenset(server.name for server in mcp_servers) if mcp_servers is not None else None
    )
    violations = [
        violation
        for slug, entry in sorted(catalog.items())
        for violation in _catalog_entry_violations(
            slug,
            entry,
            catalog=catalog,
            safe_mcp_names=safe_mcp_names,
        )
    ]
    violations.extend(_delegation_violations(catalog))
    if composition is not None:
        violations.extend(_composition_violations(composition, safe_mcp_names))
    if violations:
        raise FhaModelCatalogError(
            "Foundry Hosted Agent Responses requires the FHA V0 capability profile: "
            + "; ".join(sorted(set(violations)))
        )


def validate_fha_v0_raw_authoring(application_root: Path) -> tuple[FhaProjectionMcpServer, ...]:
    """Reject raw substitutions and return the safe V0 MCP projection inputs."""
    root = _require_application_root(application_root)
    for source_file in _catalog_authoring_files(root):
        _reject_raw_placeholders(source_file)
    return _load_safe_mcp_servers(root)


def rebuild_fha_model_only_catalog(application_root: Path) -> AgentCatalog:
    """Compatibility alias that now enforces the broader FHA V0 profile."""
    root = _require_application_root(application_root)
    mcp_servers = validate_fha_v0_raw_authoring(root)
    composition = compose_project(root, strict=True, refresh_discovery=True)
    validate_fha_v0_catalog(
        composition.catalog,
        composition=composition,
        mcp_servers=mcp_servers,
    )
    return composition.catalog


def validate_fha_model_only_catalog(catalog: AgentCatalog) -> None:
    """Compatibility alias that validates FHA V0 rather than model-only behavior."""
    validate_fha_v0_catalog(catalog)


def _build_runtime_projection(
    composition: ProjectComposition,
    *,
    project_endpoint: str,
    default_model: str,
    mcp_servers: Sequence[FhaProjectionMcpServer],
) -> FhaRuntimeProjection:
    skill_name_by_path = {
        str(path.resolve()): name for name, path in composition.skill_result.skills.items()
    }
    catalog_entries = tuple(
        FhaProjectionCatalogEntry.create(
            slug=slug,
            model=entry.resolved.model or default_model,
            trigger=entry.resolved.trigger.type if entry.resolved.trigger is not None else None,
            builtin_endpoints=_builtin_endpoint_names(entry),
            capabilities=FhaProjectionCapabilities.create(
                user_tools=_capability_names(entry.capabilities.filtered_user_tools),
                skills=tuple(
                    skill_name_by_path.get(str(path.resolve()), path.name)
                    for path in entry.capabilities.enabled_skill_paths
                ),
                mcp=_capability_names(entry.capabilities.filtered_mcp_tools),
                subagents=tuple(reference.agent for reference in entry.resolved.subagents),
            ),
        )
        for slug, entry in sorted(composition.catalog.items())
    )
    try:
        return FhaRuntimeProjection.create(
            project_endpoint=project_endpoint,
            default_model=default_model,
            catalog=catalog_entries,
            mcp_servers=mcp_servers,
        )
    except FhaRuntimeProjectionError as exc:
        raise FhaModelCatalogError("Foundry Hosted Agent runtime projection is invalid.") from exc


def _catalog_entry_violations(
    slug: str,
    entry: CatalogEntry,
    *,
    catalog: AgentCatalog,
    safe_mcp_names: frozenset[str] | None,
) -> tuple[str, ...]:
    resolved = entry.resolved
    capabilities = entry.capabilities
    violations: list[str] = []
    if resolved.sandbox_config is not None:
        violations.append(f"{slug}: Dynamic Sessions/code interpreter is configured")
    if resolved.web_request_config is not None or capabilities.web_request_tools:
        violations.append(f"{slug}: web_request system tool is configured")
    if resolved.workflows is not None or capabilities.filtered_workflow_tools:
        violations.append(f"{slug}: workflows are configured")
    if resolved.builtin_endpoints.mcp:
        violations.append(f"{slug}: built-in MCP endpoint exposure is configured")
    violations.extend(_trigger_violations(slug, entry, catalog))

    active_mcp_names = {
        _capability_name(tool) for tool in capabilities.filtered_mcp_tools or []
    }.difference({""})
    active_mcp_names.update(resolved.enabled_mcp_names)
    if safe_mcp_names is not None:
        for name in sorted(active_mcp_names.difference(safe_mcp_names)):
            violations.append(f"{slug}: MCP server `{name}` is not FHA V0-safe")
    return tuple(violations)


def _trigger_violations(
    slug: str,
    entry: CatalogEntry,
    catalog: AgentCatalog,
) -> tuple[str, ...]:
    resolved = entry.resolved
    trigger = resolved.trigger
    if trigger is not None:
        if trigger.type not in _FHA_ALLOWED_TRIGGER_TYPES:
            return (f"{slug}: trigger `{trigger.type}` is unsupported",)
        return ()
    endpoints = resolved.builtin_endpoints
    if endpoints.debug_chat_ui or endpoints.chat_api:
        return ()
    referenced_slugs = {
        reference.agent
        for catalog_entry in catalog.values()
        for reference in catalog_entry.resolved.subagents
    }
    if slug in referenced_slugs:
        return ()
    return (f"{slug}: no HTTP route, built-in chat, or service_bus_queue_trigger is configured",)


def _composition_violations(
    composition: ProjectComposition,
    safe_mcp_names: frozenset[str] | None,
) -> tuple[str, ...]:
    violations: list[str] = []
    if composition.tool_result.workflow_tools:
        violations.append("discovered workflow tools are configured")
    if composition.mcp_result.failed_loads:
        violations.append("MCP discovery has failed-load evidence")
    system_tools = composition.global_config.system_tools
    if system_tools is not None and system_tools.dynamic_sessions_code_interpreter is not None:
        violations.append("Dynamic Sessions/code interpreter is globally configured")
    if system_tools is not None and system_tools.web_request is not False:
        violations.append("web_request system tool is globally configured")
    if safe_mcp_names is not None:
        discovered_mcp_names = frozenset(composition.mcp_result.servers)
        if not discovered_mcp_names.issubset(safe_mcp_names):
            violations.append("discovered MCP configuration is not FHA V0-safe")
    return tuple(violations)


def _delegation_violations(catalog: AgentCatalog) -> tuple[str, ...]:
    references = {
        slug: tuple(reference.agent for reference in entry.resolved.subagents)
        for slug, entry in catalog.items()
    }
    violations: list[str] = []
    for slug, children in references.items():
        for child in children:
            if child == slug:
                violations.append(f"{slug}: delegation graph is cyclic")
                continue
            if references.get(child, ()):
                violations.append(
                    f"{slug}: nested delegation to `{child}` is unsupported"
                )
    if _has_delegation_cycle(references):
        violations.append("delegation graph is cyclic")
    return tuple(violations)


def _has_delegation_cycle(references: Mapping[str, tuple[str, ...]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(slug: str) -> bool:
        if slug in visiting:
            return True
        if slug in visited:
            return False
        visiting.add(slug)
        for child in references.get(slug, ()):
            if visit(child):
                return True
        visiting.remove(slug)
        visited.add(slug)
        return False

    return any(visit(slug) for slug in references)


def _catalog_authoring_files(root: Path) -> tuple[Path, ...]:
    directories = [root]
    for name in ("agents", "Agents"):
        candidate = root / name
        if candidate.is_dir():
            directories.append(candidate)
            break
    files = [root / _GLOBAL_CONFIG_NAME, root / _MCP_CONFIG_NAME]
    for directory in directories:
        try:
            children = directory.iterdir()
        except OSError:
            raise FhaModelCatalogError("Foundry Hosted Agent application root is unavailable.") from None
        for candidate in children:
            if not candidate.is_file() or candidate.name.startswith("."):
                continue
            lowered = candidate.name.casefold()
            if _is_single_agent_file(candidate.name) or lowered.endswith((".agent.md", ".claude.md")):
                files.append(candidate)
    for name in ("skills", "Skills"):
        skills_root = root / name
        if not skills_root.is_dir():
            continue
        files.extend(
            path
            for path in skills_root.rglob("SKILL.md")
            if path.is_file()
        )
        break
    return tuple(sorted({path.resolve() for path in files if path.is_file()}))


def _reject_raw_placeholders(source_file: Path) -> None:
    try:
        text = source_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise FhaModelCatalogError(
            f"Foundry Hosted Agent cannot read catalog authoring file `{source_file.name}`."
        ) from None
    if _RAW_DOLLAR_PLACEHOLDER.search(text) or _RAW_PERCENT_PLACEHOLDER.search(text):
        raise FhaModelCatalogError(
            f"Foundry Hosted Agent rejects environment substitution in `{source_file.name}`."
        )


def _load_safe_mcp_servers(root: Path) -> tuple[FhaProjectionMcpServer, ...]:
    path = root / _MCP_CONFIG_NAME
    if not path.exists():
        return ()
    try:
        document = decode_json_object(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, TypeError, DuplicateJsonKeyError):
        raise FhaModelCatalogError("Foundry Hosted Agent MCP configuration is invalid.") from None
    if set(document) != {"servers"} or not isinstance(document["servers"], dict):
        raise FhaModelCatalogError("Foundry Hosted Agent MCP configuration is invalid.")
    servers: list[FhaProjectionMcpServer] = []
    raw_servers = document["servers"]
    for name, raw_server in sorted(raw_servers.items()):
        if not isinstance(name, str) or not isinstance(raw_server, dict):
            raise FhaModelCatalogError("Foundry Hosted Agent MCP configuration is invalid.")
        servers.append(_compile_safe_mcp_server(name, raw_server))
    return tuple(servers)


def _compile_safe_mcp_server(name: str, server: dict[str, Any]) -> FhaProjectionMcpServer:
    if not set(server).issubset(_MCP_SERVER_FIELDS):
        raise FhaModelCatalogError("Foundry Hosted Agent MCP configuration has unsupported fields.")
    server_type = server.get("type")
    if server_type is not None and (
        not isinstance(server_type, str)
        or server_type.casefold() not in {"http", "streamable-http"}
    ):
        raise FhaModelCatalogError(
            "Foundry Hosted Agent supports only remote HTTP/streamable-http MCP."
        )
    url = server.get("url")
    if not isinstance(url, str):
        raise FhaModelCatalogError(
            "Foundry Hosted Agent supports only remote HTTP/streamable-http MCP."
        )
    raw_tools = server.get("tools", ["*"])
    if (
        not isinstance(raw_tools, list)
        or not raw_tools
        or any(not isinstance(tool, str) for tool in raw_tools)
    ):
        raise FhaModelCatalogError("Foundry Hosted Agent MCP allowed tools are invalid.")
    auth_scope, client_id = _compile_safe_mcp_auth(server.get("auth"))
    headers = _compile_safe_mcp_headers(server.get("headers", {}))
    try:
        return FhaProjectionMcpServer.create(
            name=name,
            url=url,
            allowed_tools=raw_tools,
            auth_scope=auth_scope,
            managed_identity_client_id=client_id,
            headers=headers,
        )
    except FhaRuntimeProjectionError as exc:
        raise FhaModelCatalogError("Foundry Hosted Agent MCP configuration is unsafe.") from exc


def _compile_safe_mcp_auth(value: object) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, dict) or not set(value).issubset(_MCP_AUTH_FIELDS):
        raise FhaModelCatalogError("Foundry Hosted Agent MCP auth configuration is unsafe.")
    scope = value.get("scope")
    client_id = value.get("client_id")
    if not isinstance(scope, str):
        raise FhaModelCatalogError("Foundry Hosted Agent MCP auth scope is required.")
    if client_id is not None and not isinstance(client_id, str):
        raise FhaModelCatalogError("Foundry Hosted Agent MCP client ID is invalid.")
    return scope, client_id


def _compile_safe_mcp_headers(value: object) -> Mapping[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(name, str) or not isinstance(header_value, str)
        for name, header_value in value.items()
    ):
        raise FhaModelCatalogError("Foundry Hosted Agent MCP static headers are invalid.")
    return value


def _builtin_endpoint_names(entry: CatalogEntry) -> tuple[str, ...]:
    endpoints = entry.resolved.builtin_endpoints
    result: list[str] = []
    if endpoints.debug_chat_ui:
        result.append("debug_chat_ui")
    if endpoints.chat_api:
        result.append("chat_api")
    if endpoints.mcp:
        result.append("mcp")
    return tuple(result)


def _capability_names(capabilities: Sequence[object] | None) -> tuple[str, ...]:
    return tuple(
        name
        for name in (
            _capability_name(capability) for capability in capabilities or ()
        )
        if name
    )


def _capability_name(capability: object) -> str:
    name = getattr(capability, "name", "")
    return name if isinstance(name, str) else ""


def _require_application_root(application_root: Path) -> Path:
    root = Path(application_root).resolve()
    if not root.is_dir():
        raise FhaModelCatalogError("Foundry Hosted Agent application root is unavailable.")
    return root
