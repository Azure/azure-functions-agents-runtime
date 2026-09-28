"""Private app-level harness selection and the bounded local preview contract."""

from __future__ import annotations

import hashlib
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from ._function_tool import FunctionTool, tool
from ._logger import logger
from .config.paths import get_app_root, resolve_config_dir
from .config.schema import AgentConfiguration, ResolvedAgent

if TYPE_CHECKING:
    from .registration.capabilities import AgentCapabilities

FLAG = "AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT"
SDK_VERSION = "1.0.14"
RUNTIME_VERSION = "1.0.85"
PROTOCOL_VERSION = 3

type ExecutionRole = Literal["primary", "delegate", "workflow_subagent"]


class CopilotPreviewError(RuntimeError):
    """A safe, actionable preview error; never includes underlying SDK details."""


class UnsupportedCapabilityError(ValueError):
    """The preview cannot preserve a requested capability's semantics."""


@dataclass(frozen=True)
class AppHarness:
    name: Literal["maf", "copilot"]
    app_root: Path
    storage_root: Path | None = None
    default_model: str | None = None
    provider: Literal["openai", "foundry"] | None = None
    endpoint: str | None = field(default=None, repr=False)


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
        installed = version("github-copilot-sdk")
    except PackageNotFoundError:
        raise CopilotPreviewError(
            "Copilot preview requires azurefunctions-agents-runtime[copilot]. "
            f"Install github-copilot-sdk=={SDK_VERSION}."
        ) from None
    if installed != SDK_VERSION:
        raise CopilotPreviewError(f"Copilot preview requires github-copilot-sdk=={SDK_VERSION}.")


def get_harness(app_root: Path | None = None, *, new_app: bool = False) -> AppHarness:
    """Capture each app independently; standalone calls retain a first-use root default."""
    root = (app_root or get_app_root()).resolve()
    with _SELECTION_LOCK:
        existing = _HARNESSES.get(root)
        if existing is not None and not new_app:
            return existing
        if not _flag_enabled(os.environ.get(FLAG)):
            selected = AppHarness("maf", root)
        else:
            check_sdk_dependency()
            provider = os.environ.get("AZURE_FUNCTIONS_AGENTS_PROVIDER", "").strip().lower()
            if provider not in {"openai", "foundry"}:
                raise UnsupportedCapabilityError(
                    "Copilot preview supports AZURE_FUNCTIONS_AGENTS_PROVIDER=foundry "
                    "(project Responses + Entra) or openai (BYOK Chat Completions), "
                    "with external native stdio only."
                )
            endpoint = None
            default_model = os.environ.get("AZURE_FUNCTIONS_AGENTS_MODEL") or None
            if provider == "foundry":
                endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").strip().rstrip("/")
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
                        "Copilot Foundry preview requires FOUNDRY_PROJECT_ENDPOINT in the form "
                        "https://<resource>.services.ai.azure.com/api/projects/<project>."
                    )
                default_model = os.environ.get("FOUNDRY_MODEL") or default_model
            if os.environ.get("FUNCTIONS_WORKER_PROCESS_COUNT", "1").strip() != "1":
                raise UnsupportedCapabilityError(
                    "Copilot local preview requires FUNCTIONS_WORKER_PROCESS_COUNT=1."
                )
            if os.environ.get("WEBSITE_INSTANCE_ID"):
                raise UnsupportedCapabilityError(
                    "Copilot preview supports local execution only; Azure hosting is not qualified."
                )
            for name in (
                "AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT",
                "AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY",
            ):
                if os.environ.get(name):
                    raise UnsupportedCapabilityError(f"Copilot preview does not support {name}.")
            app_key = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:32]
            storage_root = Path(resolve_config_dir()).resolve() / "copilot-preview" / app_key
            selected = AppHarness(
                "copilot",
                root,
                storage_root,
                default_model,
                "foundry" if provider == "foundry" else "openai",
                endpoint,
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
    reject_unsupported(agent_framework=configuration.agent_framework is not None)


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
    if harness.name == "maf":
        return
    validate_configuration(resolved.agent_configuration)
    reject_unsupported(
        non_http_trigger=resolved.trigger is not None and resolved.trigger.type != "http_trigger",
        debug_chat_ui=resolved.builtin_endpoints.debug_chat_ui,
        mcp_endpoint=resolved.builtin_endpoints.mcp,
        mcp=bool(capabilities.filtered_mcp_tools),
        skills=bool(capabilities.enabled_skill_paths),
        web_request=bool(capabilities.web_request_tools),
        execute_python=resolved.sandbox_config is not None and not resolved.tools_disabled,
        subagents=bool(resolved.subagents),
        workflows=resolved.workflows is not None and resolved.workflows.enabled,
    )
    if not (resolved.model or harness.default_model):
        raise UnsupportedCapabilityError("Copilot preview requires an explicit model.")
    prepare_tools(list(capabilities.filtered_user_tools or []))
