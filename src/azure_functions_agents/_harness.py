"""Private app-level harness selection and the bounded local preview contract."""

from __future__ import annotations

import hashlib
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from ._function_tool import FunctionTool, tool
from ._logger import logger
from .config.paths import get_app_root, resolve_config_dir
from .config.schema import HTTP_TRIGGER_TYPE, AgentConfiguration, ResolvedAgent

if TYPE_CHECKING:
    from ._native_session_identity import StorageRoute
    from .registration.capabilities import AgentCapabilities

FLAG = "AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT"
SDK_DISTRIBUTION = "github-copilot-sdk"
PROVIDER_ENV = "AZURE_FUNCTIONS_AGENTS_PROVIDER"
FOUNDRY_ENDPOINT_ENV = "FOUNDRY_PROJECT_ENDPOINT"
WORKER_COUNT_ENV = "FUNCTIONS_WORKER_PROCESS_COUNT"

type ExecutionRole = Literal["primary", "delegate", "workflow_subagent"]


class HarnessKind(StrEnum):
    MAF = "maf"
    COPILOT = "copilot"


class ProviderKind(StrEnum):
    OPENAI = "openai"
    FOUNDRY = "foundry"


class CopilotPreviewError(RuntimeError):
    """A safe, actionable preview error; never includes underlying SDK details."""


class UnsupportedCapabilityError(ValueError):
    """The preview cannot preserve a requested capability's semantics."""


@dataclass(frozen=True)
class AppHarness:
    name: HarnessKind
    app_root: Path
    storage_root: Path | None = None
    default_model: str | None = None
    provider: ProviderKind | None = None
    endpoint: str | None = field(default=None, repr=False)
    session_storage: StorageRoute | None = field(default=None, repr=False)


@dataclass(frozen=True)
class HarnessRequest:
    """Normalized inputs for the supported primary, non-streaming adapter."""

    prompt: str
    instructions: str | None
    agent_slug: str
    session_id: str
    new_session: bool
    model: str
    tools: list[FunctionTool]
    max_output_tokens: int | None
    deadline: float


_HARNESSES: dict[Path, AppHarness] = {}
_SELECTION_LOCK = threading.Lock()


def _flag_enabled(value: str | None) -> bool:
    if value is None:
        return False
    normalized = value.strip().lower()
    if normalized in {"false", "0"}:
        return False
    if normalized in {"true", "1"}:
        return True
    raise ValueError(f"{FLAG} must be true, false, 1, or 0 (or unset).")


def check_sdk_dependency() -> None:
    try:
        version(SDK_DISTRIBUTION)
    except PackageNotFoundError:
        raise CopilotPreviewError(
            "Copilot preview requires azurefunctions-agents-runtime[copilot]. "
            f"Install {SDK_DISTRIBUTION} through the optional extra."
        ) from None


def get_harness(app_root: Path | None = None, *, new_app: bool = False) -> AppHarness:
    """Capture each app independently; standalone calls retain a first-use root default."""
    root = (app_root or get_app_root()).resolve()
    with _SELECTION_LOCK:
        existing = _HARNESSES.get(root)
        if existing is not None and not new_app:
            return existing
        if not _flag_enabled(os.environ.get(FLAG)):
            selected = AppHarness(HarnessKind.MAF, root)
        else:
            from ._native_session_identity import resolve_route

            check_sdk_dependency()
            if os.environ.get(WORKER_COUNT_ENV, "1").strip() != "1":
                raise UnsupportedCapabilityError(
                    f"Copilot local preview requires {WORKER_COUNT_ENV}=1. "
                    "Multi-worker hosting is not qualified for native session persistence."
                )
            if os.environ.get("WEBSITE_INSTANCE_ID"):
                raise UnsupportedCapabilityError(
                    "Copilot preview supports local execution only; "
                    "Azure hosting is not qualified."
                )
            raw_provider = os.environ.get(PROVIDER_ENV, "").strip().lower()
            try:
                provider = ProviderKind(raw_provider)
            except ValueError:
                raise UnsupportedCapabilityError(
                    f"Copilot preview supports {PROVIDER_ENV}=foundry "
                    "(project Responses + Entra) or openai (BYOK Chat Completions), "
                    "with external native stdio only."
                ) from None
            endpoint = None
            default_model = os.environ.get("AZURE_FUNCTIONS_AGENTS_MODEL") or None
            if provider is ProviderKind.FOUNDRY:
                endpoint = os.environ.get(FOUNDRY_ENDPOINT_ENV, "").strip().rstrip("/")
                try:
                    url = urlsplit(endpoint)
                    port = url.port
                except ValueError:
                    raise UnsupportedCapabilityError(
                        "Copilot Foundry preview requires a valid HTTPS project endpoint."
                    ) from None
                if (
                    url.scheme != "https"
                    or not (url.hostname or "").endswith(".services.ai.azure.com")
                    or url.username is not None
                    or url.password is not None
                    or url.query
                    or url.fragment
                    or port not in {None, 443}
                    or not re.fullmatch(r"/api/projects/[A-Za-z0-9_-]+", url.path)
                ):
                    raise UnsupportedCapabilityError(
                        f"Copilot Foundry preview requires {FOUNDRY_ENDPOINT_ENV} in the form "
                        "https://<resource>.services.ai.azure.com/api/projects/<project>."
                    )
                default_model = os.environ.get("FOUNDRY_MODEL") or default_model
            route = resolve_route(root)
            for name in (
                "AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT",
                "AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY",
            ):
                if os.environ.get(name):
                    raise UnsupportedCapabilityError(f"Copilot preview does not support {name}.")
            app_key = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:32]
            storage_root = Path(resolve_config_dir()).resolve() / "copilot-preview" / app_key
            selected = AppHarness(
                HarnessKind.COPILOT,
                root,
                storage_root,
                default_model,
                provider,
                endpoint,
                route,
            )
        if not new_app:
            _HARNESSES[root] = selected
        logger.info("Agent harness selected: harness=%s", selected.name)
        return selected


def reject_unsupported(**capabilities: bool) -> None:
    unsupported = [name for name, enabled in capabilities.items() if enabled]
    if unsupported:
        raise UnsupportedCapabilityError(
            "Copilot preview does not support: "
            + ", ".join(unsupported)
            + ". Disable these explicitly or restart with "
            + FLAG
            + "=false to use MAF. No fallback was attempted."
        )


def validate_configuration(configuration: AgentConfiguration) -> None:
    reject_unsupported(
        agent_framework=(
            configuration.agent_framework is not None
            and configuration.agent_framework.compaction is not None
            and configuration.agent_framework.compaction.max_context_window_tokens is not None
        ),
        max_output_tokens=configuration.max_output_tokens is not None,
    )


def prepare_tools(tools: list[FunctionTool | Callable[..., Any]]) -> list[FunctionTool]:
    """Preserve existing schemas/invocation, rejecting unsupported policy instead of dropping it."""
    prepared: list[FunctionTool] = []
    names: set[str] = set()
    for candidate in tools:
        function = candidate if isinstance(candidate, FunctionTool) else tool(candidate)
        if (
            type(function) is not FunctionTool
            or function.approval_mode != "never_require"
            or function.max_invocations is not None
            or function.max_invocation_exceptions is not None
            or function.declaration_only
            or function.result_parser is not None
            or getattr(function, "_context_parameter_name", None) is not None
        ):
            raise UnsupportedCapabilityError(
                "Copilot preview supports simple FunctionTool callables only; custom tool "
                "classes, approval, invocation limits, declaration-only tools, result parsers, "
                "and injected invocation context are not supported."
            )
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", function.name):
            raise UnsupportedCapabilityError("Copilot preview requires OpenAI-compatible tool names.")
        if function.name in names:
            raise UnsupportedCapabilityError("Copilot preview requires unique custom tool names.")
        names.add(function.name)
        prepared.append(function)
    return prepared


def validate_agent(
    harness: AppHarness, resolved: ResolvedAgent, capabilities: AgentCapabilities
) -> None:
    """Fail before FunctionApp mutation, native startup, or provider/tool execution."""
    from .registration.capabilities import SANDBOX_TOOL_NAME

    if harness.name is HarnessKind.MAF:
        return
    validate_configuration(resolved.agent_configuration)
    reject_unsupported(
        non_http_trigger=resolved.trigger is not None and resolved.trigger.type != HTTP_TRIGGER_TYPE,
        debug_chat_ui=resolved.builtin_endpoints.debug_chat_ui,
        mcp_endpoint=resolved.builtin_endpoints.mcp,
        mcp=bool(capabilities.filtered_mcp_tools),
        skills=bool(capabilities.enabled_skill_paths),
        subagents=bool(resolved.subagents),
        workflows=resolved.workflows is not None and resolved.workflows.enabled,
    )
    if not (resolved.model or harness.default_model):
        raise UnsupportedCapabilityError("Copilot preview requires an explicit model.")
    prepared = prepare_tools(
        [
            *list(capabilities.filtered_user_tools or []),
            *list(capabilities.web_request_tools or []),
        ]
    )
    if (
        resolved.sandbox_config is not None
        and not resolved.tools_disabled
        and any(function.name == SANDBOX_TOOL_NAME for function in prepared)
    ):
        raise UnsupportedCapabilityError("Copilot preview requires unique custom tool names.")


def bind_harness(resolved: ResolvedAgent, capabilities: AgentCapabilities) -> AppHarness:
    """Validate a direct registration once; reuse the binding for every request."""
    if capabilities._harness is not None:
        return capabilities._harness
    harness = get_harness()
    validate_agent(harness, resolved, capabilities)
    capabilities._harness = harness
    return harness
