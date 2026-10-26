"""Azure Functions app subclasses that inject catalog-backed HostedSkill facades."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Sequence
from contextlib import suppress
from typing import Any, TypeVar, cast, get_type_hints

import azure.durable_functions as df
import azure.functions as func
from pydantic import BaseModel

from ._agent_run_diagnostics import AgentExecutionSurface
from ._in_app_tools import InAppToolRegistry, build_in_app_tool, unwrap_function_builder
from ._logger import logger
from ._tool_descriptor import ApprovalMode
from .harness._harness_binding import AppHarness, UnsupportedCapabilityError
from .hosted_skill_binding import HostedSkill
from .registration.capabilities import validate_runtime_tool_name_collisions
from .registration.catalog import AgentCatalog, CatalogEntry
from .system_tools._sandbox_endpoint import sandbox_runtime_tool_enabled

type _Handler = Callable[..., Any]
_F = TypeVar("_F", bound=_Handler)


def _worker_signature(handler: _Handler, arg_name: str) -> inspect.Signature:
    signature = inspect.signature(handler)
    parameter = signature.parameters.get(arg_name)
    if parameter is None:
        raise TypeError(
            f"hosted_skill arg_name {arg_name!r} is not present in handler "
            f"{handler.__name__!r}"
        )
    if parameter.kind in {
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.VAR_POSITIONAL,
        inspect.Parameter.VAR_KEYWORD,
    }:
        raise TypeError(
            f"hosted_skill parameter {arg_name!r} must be "
            "positional-or-keyword or keyword-only"
        )
    annotation = parameter.annotation
    with suppress(NameError, TypeError):
        annotation = get_type_hints(handler).get(arg_name, annotation)
    if annotation is not HostedSkill:
        raise TypeError(f"hosted_skill parameter {arg_name!r} must be annotated HostedSkill")
    return signature.replace(
        parameters=[
            candidate
            for candidate in signature.parameters.values()
            if candidate.name != arg_name
        ]
    )


def _source_call(
    handler: _Handler,
    source_signature: inspect.Signature,
    worker_signature: inspect.Signature,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    arg_name: str,
    injected: HostedSkill,
) -> Any:
    if arg_name in kwargs:
        raise TypeError(f"hosted_skill parameter {arg_name!r} is runtime-managed")
    bound = worker_signature.bind(*args, **kwargs)
    bound.apply_defaults()
    values = dict(bound.arguments)
    values[arg_name] = injected
    positional: list[Any] = []
    keywords: dict[str, Any] = {}
    for parameter in source_signature.parameters.values():
        if parameter.kind in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }:
            positional.append(values[parameter.name])
        elif parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            positional.extend(values.get(parameter.name, ()))
        elif parameter.kind is inspect.Parameter.VAR_KEYWORD:
            keywords.update(values.get(parameter.name, {}))
        elif parameter.name in values:
            keywords[parameter.name] = values[parameter.name]
    return handler(*positional, **keywords)


def _validate_entry(entry: CatalogEntry, harness: AppHarness) -> None:
    from .harness._agent_runner import get_agent_runner

    resolved = entry.resolved
    if resolved.subagents:
        raise UnsupportedCapabilityError(
            f"HostedSkill agent {resolved.slug!r} cannot declare chat-time subagents."
        )
    if resolved.workflows is not None and resolved.workflows.enabled:
        raise UnsupportedCapabilityError(
            f"HostedSkill agent {resolved.slug!r} cannot enable Dynamic Workflows."
        )
    try:
        validate_runtime_tool_name_collisions(
            resolved,
            authored_tools=entry.capabilities.filtered_user_tools or (),
            workflow_enabled=False,
            sandbox_enabled=sandbox_runtime_tool_enabled(
                resolved.sandbox_config,
                tools_disabled=resolved.tools_disabled,
            ),
        )
    except ValueError as exc:
        raise UnsupportedCapabilityError(str(exc)) from exc
    validator = get_agent_runner(harness).validate_hosted_skill
    if validator is not None:
        validator(resolved)


class _HostedSkillAppMixin:
    _hosted_skill_catalog: AgentCatalog
    _hosted_skill_harness: AppHarness
    _in_app_tools: InAppToolRegistry

    def _configure_hosted_skills(
        self,
        catalog: AgentCatalog,
        harness: AppHarness,
        in_app_tools: InAppToolRegistry | None = None,
    ) -> None:
        self._hosted_skill_catalog = catalog
        self._hosted_skill_harness = harness
        self._in_app_tools = in_app_tools if in_app_tools is not None else InAppToolRegistry()

    def tool(
        self,
        fn: Any = None,
        /,
        *,
        name: str | None = None,
        description: str | None = None,
        schema: type[BaseModel] | None = None,
        agents: Sequence[str] | None = None,
        approval_mode: ApprovalMode | str | None = None,
    ) -> Any:
        """Expose a function in this app as a tool for agents.

        Works bare (``@app.tool``) or with options, standalone or stacked with a trigger
        decorator. The handler runs in-process; trigger-backed handlers need ``schema=``
        describing the payload they read. ``agents=None`` exposes the tool to every agent.
        """
        scoped = self._validate_tool_agents(agents)

        def decorate(target: Any) -> Any:
            handler, _ = unwrap_function_builder(target)
            descriptor = build_in_app_tool(
                handler,
                name=name,
                description=description,
                schema=schema,
                approval_mode=approval_mode,
            )
            self._check_tool_name_collisions(descriptor.name, scoped)
            self._in_app_tools.add(descriptor, scoped)
            return target

        if fn is not None:
            return decorate(fn)
        return decorate

    def _validate_tool_agents(self, agents: Sequence[str] | None) -> tuple[str, ...] | None:
        if agents is None:
            return None
        if isinstance(agents, str):
            raise TypeError("app.tool agents must be a list of agent slugs, not a string")
        unknown = sorted(set(agents) - set(self._hosted_skill_catalog))
        if unknown:
            known = ", ".join(sorted(self._hosted_skill_catalog)) or "<none>"
            raise ValueError(f"Unknown agent slug(s) in app.tool agents: {unknown}. Known: {known}.")
        return tuple(agents)

    def _check_tool_name_collisions(self, tool_name: str, scoped: tuple[str, ...] | None) -> None:
        for slug, entry in self._hosted_skill_catalog.items():
            if scoped is not None and slug not in scoped:
                continue
            discovered = {tool.name for tool in entry.capabilities.filtered_user_tools or ()}
            if tool_name in discovered:
                raise ValueError(
                    f"app.tool name {tool_name!r} collides with a tools/ tool already "
                    f"available to agent {slug!r}."
                )

    def hosted_skill(
        self,
        *,
        arg_name: str,
        agent_name: str,
    ) -> Callable[[_F], _F]:
        """Inject a fresh HostedSkill facade into each handler invocation."""
        if not isinstance(arg_name, str) or not arg_name:
            raise ValueError("hosted_skill arg_name must be a non-empty string")
        if not isinstance(agent_name, str) or not agent_name:
            raise ValueError("hosted_skill agent_name must be a non-empty string")
        entry = self._hosted_skill_catalog.get(agent_name)
        if entry is None:
            known = ", ".join(sorted(self._hosted_skill_catalog)) or "<none>"
            raise ValueError(
                f"Unknown HostedSkill agent {agent_name!r}. Known agent slugs: {known}."
            )
        _validate_entry(entry, self._hosted_skill_harness)

        def decorate(handler: _F) -> _F:
            if not inspect.isfunction(handler):
                raise TypeError(
                    "hosted_skill must be the innermost decorator, immediately above the handler"
                )
            if not inspect.iscoroutinefunction(handler):
                raise TypeError("hosted_skill requires an async def handler")

            source_signature = inspect.signature(handler)
            visible_signature = _worker_signature(handler, arg_name)
            logger.info(
                "HostedSkill binding defined: execution_surface=%s "
                "agent_name=%s handler=%s arg_name=%s",
                AgentExecutionSurface.HOSTED_SKILL_BINDING,
                entry.resolved.slug,
                handler.__name__,
                arg_name,
            )

            @functools.wraps(handler)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await _source_call(
                    handler,
                    source_signature,
                    visible_signature,
                    args,
                    kwargs,
                    arg_name,
                    HostedSkill(entry, self._hosted_skill_harness),
                )

            async_wrapper.__annotations__ = dict(async_wrapper.__annotations__)
            async_wrapper.__annotations__.pop(arg_name, None)
            async_wrapper.__signature__ = visible_signature  # type: ignore[attr-defined]
            return cast(_F, async_wrapper)

        return decorate


class HostedSkillFunctionApp(_HostedSkillAppMixin, func.FunctionApp):
    """FunctionApp with catalog-backed HostedSkill injection."""

    def __init__(
        self,
        *,
        catalog: AgentCatalog,
        harness: AppHarness,
        in_app_tools: InAppToolRegistry | None = None,
        http_auth_level: func.AuthLevel | str = func.AuthLevel.FUNCTION,
    ) -> None:
        super().__init__(http_auth_level=http_auth_level)
        self._configure_hosted_skills(catalog, harness, in_app_tools)


class HostedSkillDFApp(_HostedSkillAppMixin, df.DFApp):
    """DFApp with catalog-backed HostedSkill injection."""

    def __init__(
        self,
        *,
        catalog: AgentCatalog,
        harness: AppHarness,
        in_app_tools: InAppToolRegistry | None = None,
        http_auth_level: func.AuthLevel | str = func.AuthLevel.FUNCTION,
    ) -> None:
        super().__init__(http_auth_level=http_auth_level)
        self._configure_hosted_skills(catalog, harness, in_app_tools)