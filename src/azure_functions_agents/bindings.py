"""Public Python smart input binding for hybrid Azure Functions apps."""

from __future__ import annotations

import asyncio
import functools
import inspect
import threading
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar, cast

import azure.durable_functions as df
import azure.functions as func

from ._observability import FaultDomain, LifecycleStage, configure_observability, start_span
from .composition import ProjectSnapshot, compose_binding_target
from .composition import load_project_snapshot as _load_project_snapshot
from .config.paths import get_app_root
from .durable import (
    _INTERNAL_AGENT_ACTIVITY_NAME,
    DurableAgentContext,
    _normalize_agent_prompt,
    _parse_activity_input,
)
from .hydration import (
    AgentBlueprint,
    InvocationMetadata,
    open_agent,
    run_blueprint,
)

_F = TypeVar("_F", bound=Callable[..., Any])


class _BindingRuntime:
    def __init__(self, app: func.FunctionApp, app_root: Path | None) -> None:
        self.app_root = Path(app_root).resolve() if app_root is not None else get_app_root()
        self._snapshot: ProjectSnapshot | None = None
        self._blueprints: dict[str, AgentBlueprint] = {}
        self._lock = threading.RLock()

    def initialize(self) -> ProjectSnapshot:
        with self._lock:
            if self._snapshot is None:
                self._snapshot = _load_project_snapshot(self.app_root)
                if self._snapshot.discovery.failed_loads:
                    failures = "; ".join(
                        f"{source}: {reason}"
                        for source, reason in self._snapshot.discovery.failed_loads
                    )
                    raise ValueError(
                        f"Agent binding app-wide capability discovery failed: {failures}. "
                        "Smart bindings discover app-level tools, skills, and MCP servers "
                        "before global tool exclusions, and binding definitions have no "
                        "per-agent capability filters in v1. Any discovery failure therefore "
                        "prevents binding registration. "
                        "Fix or remove the failing assets."
                    )
            return self._snapshot

    def resolve(self, agent_name: str) -> AgentBlueprint:
        with self._lock:
            blueprint = self._blueprints.get(agent_name)
            if blueprint is not None:
                return blueprint
            entry = compose_binding_target(self.initialize(), agent_name)
            existing = self._blueprints.get(entry.definition.slug)
            blueprint = existing if existing is not None else AgentBlueprint(entry)
            self._blueprints[agent_name] = blueprint
            self._blueprints[entry.definition.slug] = blueprint
            self._blueprints[entry.definition.filename_stem] = blueprint
            return blueprint

_RUNTIMES: weakref.WeakKeyDictionary[func.FunctionApp, _BindingRuntime] = (
    weakref.WeakKeyDictionary()
)
_RUNTIMES_LOCK = threading.Lock()


def _runtime_for(app: func.FunctionApp, app_root: Path | None = None) -> _BindingRuntime:
    with _RUNTIMES_LOCK:
        runtime = _RUNTIMES.get(app)
        if runtime is None:
            runtime = _BindingRuntime(app, app_root)
            _RUNTIMES[app] = runtime
        elif app_root is not None and runtime.app_root != Path(app_root).resolve():
            raise ValueError(
                f"The app already owns an agent binding runtime for {runtime.app_root}; "
                f"it cannot also use {Path(app_root).resolve()}"
            )
        return runtime


def _worker_signature(handler: Callable[..., Any], arg_name: str) -> inspect.Signature:
    signature = inspect.signature(handler)
    parameter = signature.parameters.get(arg_name)
    if parameter is None:
        raise TypeError(
            f"markdown_agent arg_name {arg_name!r} is not present in handler "
            f"{handler.__name__!r}"
        )
    if parameter.kind in {
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.VAR_POSITIONAL,
        inspect.Parameter.VAR_KEYWORD,
    }:
        raise TypeError(
            f"markdown_agent parameter {arg_name!r} must be positional-or-keyword or keyword-only"
        )
    return signature.replace(
        parameters=[
            candidate
            for candidate in signature.parameters.values()
            if candidate.name != arg_name
        ]
    )


def _source_call(
    handler: Callable[..., Any],
    source_signature: inspect.Signature,
    worker_signature: inspect.Signature,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    arg_name: str,
    injected: Any,
) -> Any:
    if arg_name in kwargs:
        raise TypeError(f"markdown_agent parameter {arg_name!r} is runtime-managed")
    bound = worker_signature.bind(*args, **kwargs)
    bound.apply_defaults()
    values = dict(bound.arguments)
    values[arg_name] = injected
    positional: list[Any] = []
    keywords: dict[str, Any] = {}
    for parameter in source_signature.parameters.values():
        if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            positional.append(values[parameter.name])
        elif parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            positional.extend(values.get(parameter.name, ()))
        elif parameter.kind is inspect.Parameter.VAR_KEYWORD:
            keywords.update(values.get(parameter.name, {}))
        elif parameter.name in values:
            keywords[parameter.name] = values[parameter.name]
    return handler(*positional, **keywords)


def _invocation_metadata(
    worker_signature: inspect.Signature,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> InvocationMetadata:
    bound = worker_signature.bind(*args, **kwargs)
    for value in bound.arguments.values():
        if isinstance(value, func.Context):
            return InvocationMetadata(
                function_name=str(value.function_name or "") or None,
                invocation_id=str(value.invocation_id or "") or None,
            )
    return InvocationMetadata()


def markdown_agent(
    app: func.FunctionApp,
    *,
    arg_name: str,
    agent_name: str,
) -> Callable[[_F], _F]:
    """Inject a hydrated raw Agent into an async Function or Durable activity."""
    runtime = _runtime_for(app)

    def decorate(handler: _F) -> _F:
        if not inspect.isfunction(handler):
            raise TypeError(
                "markdown_agent must be the innermost decorator, immediately above the handler"
            )
        source_signature = inspect.signature(handler)
        visible_signature = _worker_signature(handler, arg_name)
        if not inspect.iscoroutinefunction(handler):
            raise TypeError(
                "markdown_agent requires an async def handler because "
                "MAF Agent execution and lifecycle are asynchronous"
            )

        configure_observability()
        blueprint = runtime.resolve(agent_name)

        @functools.wraps(handler)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            invocation = _invocation_metadata(visible_signature, args, kwargs)
            with start_span(
                f"agent.binding.invoke {blueprint.slug}",
                fault_domain=FaultDomain.RUNTIME,
                lifecycle_stage=LifecycleStage.AGENT_RUN,
                attributes={
                    "gen_ai.agent.name": blueprint.slug,
                    "gen_ai.request.model": blueprint.entry.config.model,
                    "faas.name": invocation.function_name,
                    "faas.invocation_id": invocation.invocation_id,
                },
            ) as span:
                try:
                    async with open_agent(blueprint, invocation) as agent:
                        result = await _source_call(
                            handler,
                            source_signature,
                            visible_signature,
                            args,
                            kwargs,
                            arg_name,
                            agent,
                        )
                except asyncio.CancelledError:
                    span.set_attribute("af.binding.outcome", "cancelled")
                    raise
                except BaseException:
                    span.set_attribute("af.binding.outcome", "error")
                    raise
                span.set_attribute("af.binding.outcome", "success")
                return result

        async_wrapper.__signature__ = visible_signature  # type: ignore[attr-defined]
        return cast(_F, async_wrapper)

    return decorate


class AiApp(func.FunctionApp):
    """FunctionApp with the smart ``markdown_agent`` decorator."""

    def __init__(
        self,
        http_auth_level: func.AuthLevel | str = func.AuthLevel.FUNCTION,
        *,
        app_root: Path | None = None,
    ) -> None:
        super().__init__(http_auth_level=http_auth_level)
        self._agent_binding_root = app_root

    def markdown_agent(
        self,
        *,
        arg_name: str,
        agent_name: str,
    ) -> Callable[[_F], _F]:
        _runtime_for(self, self._agent_binding_root)
        return markdown_agent(
            self,
            arg_name=arg_name,
            agent_name=agent_name,
        )


class DurableAiApp(df.DFApp):  # type: ignore[misc]
    """DFApp with Agent injection and replay-safe orchestrator Agent calls."""

    def __init__(
        self,
        http_auth_level: func.AuthLevel | str = func.AuthLevel.FUNCTION,
        *,
        app_root: Path | None = None,
    ) -> None:
        super().__init__(http_auth_level=http_auth_level)
        self._agent_binding_root = app_root
        self._markdown_agent_activity_registered = False
        self._agent_binding_functions_enumerated = False

    def _ensure_markdown_agent_activity(self) -> None:
        if self._markdown_agent_activity_registered:
            return
        runtime = _runtime_for(self, self._agent_binding_root)
        runtime.initialize()
        blueprint = df.Blueprint()

        @blueprint.activity_trigger(input_name="payload")  # type: ignore[untyped-decorator]
        async def azure_functions_agents_run_markdown_agent(
            payload: object,
            context: func.Context,
        ) -> str:
            parsed = _parse_activity_input(payload)
            agent_blueprint = runtime.resolve(parsed["agent_name"])
            response = await run_blueprint(
                agent_blueprint,
                _normalize_agent_prompt(parsed["input"]),
                invocation=InvocationMetadata(
                    function_name=(
                        str(context.function_name or "")
                        or _INTERNAL_AGENT_ACTIVITY_NAME
                    ),
                    invocation_id=str(context.invocation_id or "") or None,
                    durable_instance_id=parsed["durable_instance_id"],
                ),
                enable_persistent_history=False,
            )
            text = getattr(response, "text", None)
            if not isinstance(text, str):
                raise TypeError("Markdown Agent activity response.text must be a string")
            return text

        self.register_blueprint(blueprint)
        self._markdown_agent_activity_registered = True

    def orchestration_trigger(
        self,
        context_name: str,
        orchestration: str | None = None,
        input_type: type | None = None,
    ) -> Callable[[_F], Any]:
        """Register an orchestrator whose named context supports ``call_agent``."""
        sdk_orchestration_trigger = super().orchestration_trigger
        if input_type is None:
            sdk_decorator = sdk_orchestration_trigger(
                context_name=context_name,
                orchestration=orchestration,
            )
        elif "input_type" in inspect.signature(sdk_orchestration_trigger).parameters:
            sdk_decorator = sdk_orchestration_trigger(
                context_name=context_name,
                orchestration=orchestration,
                input_type=input_type,
            )
        else:
            raise TypeError(
                "The installed azure-functions-durable version does not support "
                "orchestration_trigger(input_type=...)"
            )

        def decorate(handler: _F) -> Any:
            if not inspect.isgeneratorfunction(handler):
                raise TypeError(
                    "DurableAiApp orchestration_trigger requires a synchronous "
                    "generator function"
                )
            signature = inspect.signature(handler)
            parameter = signature.parameters.get(context_name)
            if parameter is None:
                raise TypeError(
                    f"orchestration context_name {context_name!r} is not present "
                    f"in handler {handler.__name__!r}"
                )
            if parameter.kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD:
                raise TypeError(
                    f"orchestration context parameter {context_name!r} must be "
                    "positional-or-keyword"
                )

            self._ensure_markdown_agent_activity()

            @functools.wraps(handler)
            def proxy_orchestrator(*args: Any, **kwargs: Any) -> Any:
                bound = signature.bind(*args, **kwargs)
                context = cast(df.DurableOrchestrationContext, bound.arguments[context_name])
                bound.arguments[context_name] = DurableAgentContext(context)
                result = handler(*bound.args, **bound.kwargs)
                return (yield from result)

            proxy_orchestrator.__signature__ = signature  # type: ignore[attr-defined]
            return sdk_decorator(proxy_orchestrator)

        return decorate

    def get_functions(self) -> list[Any]:
        if self._agent_binding_functions_enumerated:
            self.functions_bindings.clear()
        try:
            functions = cast(list[Any], super().get_functions())
        except ValueError as exc:
            self._agent_binding_functions_enumerated = True
            if _INTERNAL_AGENT_ACTIVITY_NAME in str(exc):
                raise ValueError(
                    f"Function name {_INTERNAL_AGENT_ACTIVITY_NAME!r} is reserved by "
                    "DurableAiApp.call_agent(); rename the conflicting customer function"
                ) from exc
            raise
        self._agent_binding_functions_enumerated = True
        return functions

    def markdown_agent(
        self,
        *,
        arg_name: str,
        agent_name: str,
    ) -> Callable[[_F], _F]:
        _runtime_for(self, self._agent_binding_root)
        return markdown_agent(
            self,
            arg_name=arg_name,
            agent_name=agent_name,
        )