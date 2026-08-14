"""Public Python smart input binding for hybrid Azure Functions apps."""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import threading
import weakref
from collections.abc import Callable, Generator, Mapping
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

import azure.durable_functions as df
import azure.functions as func

from ._observability import FaultDomain, LifecycleStage, configure_observability, start_span
from .composition import ProjectSnapshot, compose_binding_target
from .composition import load_project_snapshot as _load_project_snapshot
from .config.paths import get_app_root
from .hydration import (
    AgentBlueprint,
    InvocationMetadata,
    open_agent,
    run_blueprint,
)

type AgentInputMode = Literal["function", "activity", "orchestrator"]

_F = TypeVar("_F", bound=Callable[..., Any])
_DURABLE_ACTIVITY_NAME = "_afa_agent_binding_run"


class DurableAiAgent:
    """Replay-safe orchestrator facade that schedules model work as an activity."""

    def __init__(
        self,
        context: df.DurableOrchestrationContext,
        blueprint: AgentBlueprint,
    ) -> None:
        self._context = context
        self._blueprint = blueprint

    def run(
        self,
        messages: Any = None,
        *,
        options: Mapping[str, Any] | None = None,
        stream: bool = False,
    ) -> Any:
        if stream:
            raise ValueError(
                "DurableAiAgent does not support streaming; yield one non-streaming run() task"
            )
        payload = {
            "agent_slug": self._blueprint.slug,
            "messages": messages,
            "options": dict(options) if options is not None else None,
            "instance_id": self._context.instance_id,
        }
        try:
            json.dumps(payload)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "DurableAiAgent messages and options must be JSON-serializable"
            ) from exc
        return self._context.call_activity(_DURABLE_ACTIVITY_NAME, payload)


class _BindingRuntime:
    def __init__(self, app: func.FunctionApp, app_root: Path | None) -> None:
        self._app_ref = weakref.ref(app)
        self.app_root = Path(app_root).resolve() if app_root is not None else get_app_root()
        self._snapshot: ProjectSnapshot | None = None
        self._blueprints: dict[str, AgentBlueprint] = {}
        self._lock = threading.RLock()
        self._durable_activity_registered = False

    def resolve(self, agent_name: str) -> AgentBlueprint:
        with self._lock:
            blueprint = self._blueprints.get(agent_name)
            if blueprint is not None:
                return blueprint
            if self._snapshot is None:
                self._snapshot = _load_project_snapshot(self.app_root)
                if self._snapshot.discovery.failed_loads:
                    failures = "; ".join(
                        f"{source}: {reason}"
                        for source, reason in self._snapshot.discovery.failed_loads
                    )
                    raise ValueError(f"Agent binding discovery failed: {failures}")
            entry = compose_binding_target(self._snapshot, agent_name)
            existing = self._blueprints.get(entry.definition.slug)
            blueprint = existing if existing is not None else AgentBlueprint(entry)
            self._blueprints[agent_name] = blueprint
            self._blueprints[entry.definition.slug] = blueprint
            self._blueprints[entry.definition.filename_stem] = blueprint
            return blueprint

    def blueprint_for_slug(self, slug: str) -> AgentBlueprint:
        with self._lock:
            blueprint = self._blueprints.get(slug)
        if blueprint is None:
            raise ValueError(f"Durable agent binding target {slug!r} is not registered")
        return blueprint

    def register_durable_activity(self) -> None:
        with self._lock:
            if self._durable_activity_registered:
                return
            app = self._app_ref()
            if app is None:
                raise RuntimeError("The FunctionApp owning this agent binding runtime was collected")
            if not isinstance(app, df.DFApp):
                raise TypeError(
                    "Durable agent_input modes require DurableAiApp or azure.durable_functions.DFApp"
                )

            async def _afa_agent_binding_run(
                payload: dict,  # type: ignore[type-arg]
            ) -> dict[str, Any]:
                slug = str(payload.get("agent_slug") or "")
                blueprint = self.blueprint_for_slug(slug)
                instance_id = str(payload.get("instance_id") or "") or None
                response = await run_blueprint(
                    blueprint,
                    payload.get("messages"),
                    session_id=instance_id,
                    options=payload.get("options"),
                    invocation=InvocationMetadata(durable_instance_id=instance_id),
                )
                response_data = response.to_dict() if hasattr(response, "to_dict") else {}
                result = {
                    "text": str(getattr(response, "text", "") or ""),
                    "messages": response_data.get("messages", []),
                    "response_id": getattr(response, "response_id", None),
                    "usage": response_data.get("usage_details"),
                }
                try:
                    json.dumps(result)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        f"Agent binding result for {slug!r} is not JSON-serializable "
                        "for Durable history"
                    ) from exc
                return result

            activity_decorator = cast(Any, app).activity_trigger(
                input_name="payload",
                activity=_DURABLE_ACTIVITY_NAME,
            )
            activity_decorator(_afa_agent_binding_run)
            self._durable_activity_registered = True


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
            f"agent_input arg_name {arg_name!r} is not present in handler {handler.__name__!r}"
        )
    if parameter.kind in {
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.VAR_POSITIONAL,
        inspect.Parameter.VAR_KEYWORD,
    }:
        raise TypeError(
            f"agent_input parameter {arg_name!r} must be positional-or-keyword or keyword-only"
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
        raise TypeError(f"agent_input parameter {arg_name!r} is runtime-managed")
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


def _durable_context(
    worker_signature: inspect.Signature,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> df.DurableOrchestrationContext:
    bound = worker_signature.bind(*args, **kwargs)
    for value in bound.arguments.values():
        if isinstance(value, df.DurableOrchestrationContext):
            return value
    raise TypeError(
        "orchestrator mode requires a DurableOrchestrationContext handler parameter"
    )


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


def agent_input(
    app: func.FunctionApp,
    *,
    arg_name: str,
    agent_name: str,
    mode: AgentInputMode = "function",
) -> Callable[[_F], _F]:
    """Inject a hydrated raw Agent or a replay-safe Durable proxy."""
    if mode not in {"function", "activity", "orchestrator"}:
        raise ValueError(
            "agent_input mode must be 'function', 'activity', or 'orchestrator'"
        )
    if mode != "function" and not isinstance(app, df.DFApp):
        raise TypeError(
            "Durable agent_input modes require DurableAiApp or azure.durable_functions.DFApp"
        )
    runtime = _runtime_for(app)

    def decorate(handler: _F) -> _F:
        if not inspect.isfunction(handler):
            raise TypeError(
                "agent_input must be the innermost decorator, immediately above the handler"
            )
        source_signature = inspect.signature(handler)
        visible_signature = _worker_signature(handler, arg_name)
        if mode == "orchestrator" and not inspect.isgeneratorfunction(handler):
            raise TypeError("orchestrator mode requires a synchronous generator handler")
        if mode != "orchestrator" and not inspect.iscoroutinefunction(handler):
            raise TypeError(
                f"agent_input mode {mode!r} requires an async def handler because "
                "MAF Agent execution and lifecycle are asynchronous"
            )

        configure_observability()
        blueprint = runtime.resolve(agent_name)
        if mode == "orchestrator":
            runtime.register_durable_activity()

            @functools.wraps(handler)
            def orchestrator_wrapper(*args: Any, **kwargs: Any) -> Generator[Any, Any, Any]:
                context = _durable_context(visible_signature, args, kwargs)
                result = _source_call(
                    handler,
                    source_signature,
                    visible_signature,
                    args,
                    kwargs,
                    arg_name,
                    DurableAiAgent(context, blueprint),
                )
                return (yield from result)

            wrapped: Callable[..., Any] = orchestrator_wrapper
        else:

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

            wrapped = async_wrapper

        wrapped.__signature__ = visible_signature  # type: ignore[attr-defined]
        return cast(_F, wrapped)

    return decorate


class AiApp(func.FunctionApp):
    """FunctionApp with the smart ``agent_input`` decorator."""

    def __init__(
        self,
        http_auth_level: func.AuthLevel | str = func.AuthLevel.FUNCTION,
        *,
        app_root: Path | None = None,
    ) -> None:
        super().__init__(http_auth_level=http_auth_level)
        self._agent_binding_root = app_root

    def agent_input(
        self,
        *,
        arg_name: str,
        agent_name: str,
        mode: AgentInputMode = "function",
    ) -> Callable[[_F], _F]:
        _runtime_for(self, self._agent_binding_root)
        return agent_input(
            self,
            arg_name=arg_name,
            agent_name=agent_name,
            mode=mode,
        )


class DurableAiApp(df.DFApp):  # type: ignore[misc]
    """DFApp with async Function/activity and orchestrator agent injection."""

    def __init__(
        self,
        http_auth_level: func.AuthLevel | str = func.AuthLevel.FUNCTION,
        *,
        app_root: Path | None = None,
    ) -> None:
        super().__init__(http_auth_level=http_auth_level)
        self._agent_binding_root = app_root

    def agent_input(
        self,
        *,
        arg_name: str,
        agent_name: str,
        mode: AgentInputMode = "function",
    ) -> Callable[[_F], _F]:
        _runtime_for(self, self._agent_binding_root)
        return agent_input(
            self,
            arg_name=arg_name,
            agent_name=agent_name,
            mode=mode,
        )