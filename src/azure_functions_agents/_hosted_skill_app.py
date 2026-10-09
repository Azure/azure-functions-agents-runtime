"""Azure Functions app subclasses that inject catalog-backed HostedSkill facades."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from contextlib import suppress
from typing import Any, TypeVar, cast, get_type_hints

import azure.durable_functions as df
import azure.functions as func

from .client_manager import get_client_manager
from .harness._harness_binding import AppHarness, HarnessKind, UnsupportedCapabilityError
from .hosted_skill import HostedSkill
from .registration.catalog import AgentCatalog, CatalogEntry

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
    resolved = entry.resolved
    if resolved.subagents:
        raise UnsupportedCapabilityError(
            f"HostedSkill agent {resolved.slug!r} cannot declare chat-time subagents."
        )
    if resolved.workflows is not None and resolved.workflows.enabled:
        raise UnsupportedCapabilityError(
            f"HostedSkill agent {resolved.slug!r} cannot enable Dynamic Workflows."
        )
    if harness.name is HarnessKind.MAF:
        get_client_manager().validate_provider_settings(resolved.model)


class _HostedSkillAppMixin:
    _hosted_skill_catalog: AgentCatalog
    _hosted_skill_harness: AppHarness

    def _configure_hosted_skills(
        self,
        catalog: AgentCatalog,
        harness: AppHarness,
    ) -> None:
        self._hosted_skill_catalog = catalog
        self._hosted_skill_harness = harness

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
        http_auth_level: func.AuthLevel | str = func.AuthLevel.FUNCTION,
    ) -> None:
        super().__init__(http_auth_level=http_auth_level)
        self._configure_hosted_skills(catalog, harness)


class HostedSkillDFApp(_HostedSkillAppMixin, df.DFApp):
    """DFApp with catalog-backed HostedSkill injection."""

    def __init__(
        self,
        *,
        catalog: AgentCatalog,
        harness: AppHarness,
        http_auth_level: func.AuthLevel | str = func.AuthLevel.FUNCTION,
    ) -> None:
        super().__init__(http_auth_level=http_auth_level)
        self._configure_hosted_skills(catalog, harness)