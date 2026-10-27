"""Azure Functions app subclasses with catalog-backed injection and smart retry."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, TypeVar, cast, get_type_hints

import azure.durable_functions as df
import azure.functions as func
from azure.functions.decorators.function_app import Function

from ._agent_run_diagnostics import AgentExecutionSurface
from ._hosted_recovery_input import prepare_recovery_input
from ._hosted_skill_recovery import _validate_retry_options, recover_handler
from ._logger import logger
from ._smart_retry_advisor import build_default_advisor
from .config.schema import GlobalConfig
from .harness._harness_binding import AppHarness, UnsupportedCapabilityError, validate_agent
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


@dataclass
class _RecoveryBinding:
    signature: inspect.Signature
    argument: str | None

    def select(self, name: str) -> None:
        """Bind recovery to a concrete worker-visible parameter."""
        parameter = self.signature.parameters.get(name)
        if parameter is None or parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            raise TypeError("Recovery request_arg must name a visible handler parameter")
        self.argument = name


class _HostedSkillAppMixin:
    _hosted_skill_catalog: AgentCatalog
    _hosted_skill_harness: AppHarness
    _hosted_skill_recovery_bindings: dict[_Handler, _RecoveryBinding]
    _hosted_skill_wrappers: set[_Handler]
    _default_smart_retry_advisor: CatalogEntry

    def _configure_hosted_skills(
        self,
        catalog: AgentCatalog,
        harness: AppHarness,
        default_advisor: CatalogEntry | None = None,
    ) -> None:
        self._hosted_skill_catalog = catalog
        self._hosted_skill_harness = harness
        self._hosted_skill_recovery_bindings = {}
        self._hosted_skill_wrappers = set()
        self._default_smart_retry_advisor = (
            default_advisor if default_advisor is not None else build_default_advisor(GlobalConfig())
        )

    def _decorator_entry(self, agent_name: str, decorator: str) -> CatalogEntry:
        """Qualify one catalog agent without acquiring execution resources."""
        if not isinstance(agent_name, str) or not agent_name:
            raise ValueError(f"{decorator} agent_name must be a non-empty string")
        entry = self._hosted_skill_catalog.get(agent_name)
        if entry is None:
            known = ", ".join(sorted(self._hosted_skill_catalog)) or "<none>"
            raise ValueError(
                f"Unknown HostedSkill agent {agent_name!r}. Known agent slugs: {known}."
            )
        _validate_entry(entry, self._hosted_skill_harness)
        return entry

    def _validate_decorator_handler(self, handler: _Handler, decorator: str) -> None:
        """Require a single runtime decorator directly on an async handler."""
        if not inspect.isfunction(handler):
            raise TypeError(
                f"{decorator} must be the innermost decorator, immediately above the handler"
            )
        if handler in self._hosted_skill_wrappers:
            raise TypeError("hosted_skill and smart_retry cannot be combined or repeated")
        if not inspect.iscoroutinefunction(handler):
            raise TypeError(f"{decorator} requires an async def handler")

    def _resolve_recovery_bindings(self, functions: list[Function]) -> None:
        """Select trigger arguments after outer decorators have attached metadata."""
        for function in functions:
            binding = self._hosted_skill_recovery_bindings.get(function.get_user_function())
            if binding is None or binding.argument is not None:
                continue
            trigger = function.get_trigger()
            if trigger is None:
                raise TypeError("Recovery requires a trigger binding or explicit request_arg")
            binding.select(trigger.name)

    def hosted_skill(
        self,
        *,
        arg_name: str,
        agent_name: str,
    ) -> Callable[[_F], _F]:
        """Inject a fresh HostedSkill facade into each handler invocation."""
        if not isinstance(arg_name, str) or not arg_name:
            raise ValueError("hosted_skill arg_name must be a non-empty string")
        entry = self._decorator_entry(agent_name, "hosted_skill")

        def decorate(handler: _F) -> _F:
            self._validate_decorator_handler(handler, "hosted_skill")

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
                    handler, source_signature, visible_signature, args, kwargs, arg_name,
                    HostedSkill(entry, self._hosted_skill_harness),
                )

            async_wrapper.__annotations__ = dict(async_wrapper.__annotations__)
            async_wrapper.__annotations__.pop(arg_name, None)
            async_wrapper.__signature__ = visible_signature  # type: ignore[attr-defined]
            self._hosted_skill_wrappers.add(async_wrapper)
            return cast(_F, async_wrapper)

        return decorate

    def smart_retry(
        self,
        *,
        agent_name: str | None = None,
        max_retries: int = 1,
        timeout: float = 60,
        request_arg: str | None = None,
        audit_only: bool = False,
    ) -> Callable[[_F], _F]:
        """Recover a replay-safe async handler without injecting an application argument."""
        _validate_retry_options(
            request_arg=request_arg, timeout=timeout, max_retries=max_retries,
            audit_only=audit_only,
        )
        if agent_name is None:
            entry = self._default_smart_retry_advisor
            validate_agent(self._hosted_skill_harness, entry.resolved, entry.capabilities)
            _validate_entry(entry, self._hosted_skill_harness)
        else:
            entry = self._decorator_entry(agent_name, "smart_retry")

        def decorate(handler: _F) -> _F:
            self._validate_decorator_handler(handler, "smart_retry")
            signature = inspect.signature(handler)
            binding = _RecoveryBinding(signature, None)
            if request_arg is not None:
                binding.select(request_arg)

            @functools.wraps(handler)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                selected = binding.argument
                if selected is None:
                    raise RuntimeError(
                        "Recovery trigger argument is unresolved; index the app with "
                        "get_functions() or supply an explicit request_arg for direct calls"
                    )
                request = await prepare_recovery_input(bound.arguments[selected])

                async def execute(corrected_request: object) -> Any:
                    call = signature.bind(*bound.args, **bound.kwargs)
                    call.arguments[selected] = corrected_request
                    return await handler(*call.args, **call.kwargs)

                try:
                    return await execute(request.request)
                except Exception as error:
                    return await recover_handler(
                        HostedSkill(entry, self._hosted_skill_harness), request, error,
                        execute, handler, self._hosted_skill_harness.app_root, timeout,
                        max_retries=max_retries, audit_only=audit_only,
                    )

            self._hosted_skill_wrappers.add(async_wrapper)
            self._hosted_skill_recovery_bindings[async_wrapper] = binding
            return cast(_F, async_wrapper)

        return decorate


class HostedSkillFunctionApp(_HostedSkillAppMixin, func.FunctionApp):
    """FunctionApp with catalog-backed HostedSkill injection."""

    def __init__(
        self,
        *,
        catalog: AgentCatalog,
        harness: AppHarness,
        default_advisor: CatalogEntry | None = None,
        http_auth_level: func.AuthLevel | str = func.AuthLevel.FUNCTION,
    ) -> None:
        super().__init__(http_auth_level=http_auth_level)
        self._configure_hosted_skills(catalog, harness, default_advisor)

    def get_functions(self) -> list[Function]:
        """Index functions and resolve automatic recovery input selection."""
        functions = super().get_functions()
        self._resolve_recovery_bindings(functions)
        return functions


class HostedSkillDFApp(_HostedSkillAppMixin, df.DFApp):
    """DFApp with catalog-backed HostedSkill injection."""

    def __init__(
        self,
        *,
        catalog: AgentCatalog,
        harness: AppHarness,
        default_advisor: CatalogEntry | None = None,
        http_auth_level: func.AuthLevel | str = func.AuthLevel.FUNCTION,
    ) -> None:
        super().__init__(http_auth_level=http_auth_level)
        self._configure_hosted_skills(catalog, harness, default_advisor)

    def get_functions(self) -> list[Function]:
        """Index functions and resolve automatic recovery input selection."""
        functions = super().get_functions()
        self._resolve_recovery_bindings(functions)
        return functions