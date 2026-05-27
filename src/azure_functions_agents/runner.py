"""Agent execution layer — runs prompts through the Microsoft Agent Framework.

This module is the single entry point for "execute a prompt against an agent".
Both the HTTP chat endpoints and triggered-agent handlers go through
:func:`run_agent` (one-shot) or :func:`run_agent_stream` (SSE).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict, cast

from ._blob_history import build_blob_provider_from_environment
from ._logger import logger
from .client_manager import build_chat_client
from .config.paths import get_app_root, resolve_config_dir
from .config.schema import AgentConfiguration
from .discovery.mcp import discover_mcp_servers
from .discovery.tools import discover_user_tools

DEFAULT_TIMEOUT = 900.0
DEFAULT_MODEL: str | None = None
DEFAULT_REASONING_EFFORT = 'high'
DEFAULT_REASONING_SUMMARY = 'concise'

_SESSION_ID_PATTERN = re.compile(r'^[A-Za-z0-9._-]{1,128}$')


class _ChatOptionsKwargs(TypedDict, total=False):
    temperature: float
    top_p: float
    max_tokens: int


_SESSION_LOCKS: dict[str, asyncio.Lock] = {}
_SESSION_LOCKS_GUARD = asyncio.Lock()


async def _get_session_lock(session_id: str) -> asyncio.Lock:
    async with _SESSION_LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            _SESSION_LOCKS[session_id] = lock
        return lock


@dataclass
class AgentResult:
    """Result of a non-streaming agent run."""

    session_id: str
    content: str
    content_intermediate: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    reasoning: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


def _validate_session_id(session_id: str | None) -> str | None:
    if session_id is None:
        return None
    if not isinstance(session_id, str) or not _SESSION_ID_PATTERN.match(session_id):
        raise ValueError(f'Invalid session_id (must match {_SESSION_ID_PATTERN.pattern})')
    return session_id


def _resolve_sessions_dir() -> Path:
    base = Path(resolve_config_dir()).resolve() / 'agent-sessions'
    base.mkdir(parents=True, exist_ok=True)
    return base


def _build_history_provider() -> Any:
    from agent_framework import FileHistoryProvider

    blob_provider = build_blob_provider_from_environment()
    if blob_provider is not None:
        return blob_provider
    return FileHistoryProvider(storage_path=_resolve_sessions_dir())


def _build_chat_options(agent_configuration: AgentConfiguration) -> Any:
    from agent_framework import ChatOptions

    kwargs: _ChatOptionsKwargs = {}
    if agent_configuration.temperature is not None:
        kwargs['temperature'] = agent_configuration.temperature
    if agent_configuration.top_p is not None:
        kwargs['top_p'] = agent_configuration.top_p
    if agent_configuration.max_tokens is not None:
        kwargs['max_tokens'] = agent_configuration.max_tokens
    return cast(Any, ChatOptions)(**kwargs)


def _env_value(name: str) -> str:
    from os import environ

    return (environ.get(name) or '').strip()


def _build_chat_options_from_environment() -> dict[str, Any]:
    return {
        'reasoning': {
            'effort': _env_value('MAF_REASONING_EFFORT') or DEFAULT_REASONING_EFFORT,
            'summary': _env_value('MAF_REASONING_SUMMARY') or DEFAULT_REASONING_SUMMARY,
        }
    }


def _invoke_agent_run(
    agent: Any,
    prompt: str,
    *,
    session: Any,
    stream: bool = False,
) -> Any:
    kwargs: dict[str, Any] = {'session': session}
    if stream:
        kwargs['stream'] = True

    options = _build_chat_options_from_environment()
    try:
        return agent.run(prompt, options=options, **kwargs)
    except TypeError as exc:
        if "unexpected keyword argument 'options'" not in str(exc):
            raise
        return agent.run(prompt, **kwargs)


def _effective_agent_configuration(
    agent_configuration: AgentConfiguration,
    model: str | None,
) -> AgentConfiguration:
    if model is None:
        return agent_configuration
    return agent_configuration.model_copy(update={'model': model})


def _build_skills_provider(skill_paths: list[Path] | None) -> Any:
    if not skill_paths:
        return None
    import warnings

    from agent_framework import SkillsProvider
    from agent_framework._feature_stage import ExperimentalWarning

    with warnings.catch_warnings():
        warnings.simplefilter('ignore', category=ExperimentalWarning)
        return SkillsProvider.from_paths(list(skill_paths))


async def _build_agent_session_history(
    *,
    instructions: str | None,
    agent_configuration: AgentConfiguration,
    session_id: str | None,
    tools: list[Any] | None,
    mcp_tools: list[Any] | None,
    skill_paths: list[Path] | None,
    use_connector_tools: bool = True,
    model: str | None = None,
    sandbox_tools: list[Any] | None,
) -> tuple[Any, Any, str]:
    del use_connector_tools

    from agent_framework import Agent, AgentSession

    effective_configuration = _effective_agent_configuration(agent_configuration, model)
    chat_client = build_chat_client(effective_configuration)

    validated_id = _validate_session_id(session_id)
    if validated_id is None:
        session = AgentSession()
        resolved_id = session.session_id
    else:
        resolved_id = validated_id
        session = AgentSession(session_id=resolved_id)

    history_provider = _build_history_provider()
    app_root = get_app_root()
    resolved_tools: list[Any] = list(discover_user_tools(app_root)) if tools is None else list(tools)

    if sandbox_tools:
        resolved_tools.extend(sandbox_tools)

    resolved_mcp_tools = list(discover_mcp_servers(app_root).values()) if mcp_tools is None else list(mcp_tools)
    if resolved_mcp_tools:
        resolved_tools.extend(resolved_mcp_tools)

    context_providers: list[Any] = [history_provider]
    skills_provider = _build_skills_provider(skill_paths)
    if skills_provider is not None:
        context_providers.append(skills_provider)

    agent = Agent(
        chat_client,
        instructions=instructions.strip() if instructions and instructions.strip() else None,
        tools=resolved_tools,
        default_options=_build_chat_options(effective_configuration),
        context_providers=context_providers,
    )

    return agent, session, resolved_id


def _content_type(item: Any) -> str:
    return str(getattr(item, 'type', '') or '')


def _content_text(item: Any) -> str:
    return str(getattr(item, 'text', '') or '')


def _function_call_event(item: Any) -> dict[str, Any]:
    return {
        'type': 'tool_start',
        'tool_call_id': getattr(item, 'call_id', None) or getattr(item, 'id', None),
        'tool_name': getattr(item, 'name', None),
        'arguments': getattr(item, 'arguments', None),
    }


def _merge_tool_arguments(previous: Any, current: Any) -> Any:
    if previous is None:
        return current
    if current is None:
        return previous
    if isinstance(previous, str) and isinstance(current, str):
        if current.startswith(previous):
            return current
        return previous + current
    return current


def _is_complete_json_argument(value: Any) -> bool:
    if not isinstance(value, str):
        return value is not None
    text = value.strip()
    if not text:
        return False
    try:
        json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return False
    return True


def _function_result_event(item: Any) -> dict[str, Any]:
    return {
        'type': 'tool_end',
        'tool_call_id': getattr(item, 'call_id', None) or getattr(item, 'id', None),
        'tool_name': getattr(item, 'name', None),
        'result': getattr(item, 'result', None),
    }


async def run_agent(
    prompt: str,
    *,
    instructions: str | None = None,
    agent_configuration: AgentConfiguration,
    tools: list[Any] | None = None,
    mcp_tools: list[Any] | None = None,
    skill_paths: list[Path] | None = None,
    use_connector_tools: bool = True,
    model: str | None = None,
    session_id: str | None = None,
    sandbox_tools: list[Any] | None = None,
) -> AgentResult:
    effective_configuration = _effective_agent_configuration(agent_configuration, model)
    timeout = (
        float(effective_configuration.timeout)
        if effective_configuration.timeout is not None
        else None
    )

    agent, session, resolved_id = await _build_agent_session_history(
        instructions=instructions,
        agent_configuration=effective_configuration,
        session_id=session_id,
        tools=tools,
        mcp_tools=mcp_tools,
        skill_paths=skill_paths,
        use_connector_tools=use_connector_tools,
        model=None,
        sandbox_tools=sandbox_tools,
    )

    lock = await _get_session_lock(resolved_id)
    async with lock:
        try:
            response = await asyncio.wait_for(
                _invoke_agent_run(agent, prompt, session=session),
                timeout=timeout,
            )
        except TimeoutError:
            raise RuntimeError(f'Agent run timed out after {timeout}s') from None

    text = ''
    try:
        text = str(getattr(response, 'text', '') or '')
    except Exception:
        text = ''
    if not text:
        try:
            for msg in getattr(response, 'messages', None) or []:
                for item in getattr(msg, 'contents', None) or []:
                    if _content_type(item) == 'text':
                        text += _content_text(item)
        except Exception as exc:
            logger.debug('Failed to extract response text: %s', exc)

    tool_calls: list[dict[str, Any]] = []
    try:
        for msg in getattr(response, 'messages', None) or []:
            for item in getattr(msg, 'contents', None) or []:
                ctype = _content_type(item)
                if ctype == 'function_call':
                    tool_calls.append(_function_call_event(item))
                elif ctype == 'function_result':
                    call_id = getattr(item, 'call_id', None) or getattr(item, 'id', None)
                    matched = next(
                        (tc for tc in reversed(tool_calls) if tc.get('tool_call_id') == call_id),
                        None,
                    )
                    if matched is not None:
                        matched['result'] = getattr(item, 'result', None)
    except Exception as exc:
        logger.debug('Failed to extract tool_calls: %s', exc)

    return AgentResult(
        session_id=resolved_id,
        content=text,
        tool_calls=tool_calls,
    )


async def _iter_with_deadline[T](
    stream: AsyncIterable[T], deadline: float | None
) -> AsyncIterator[T]:
    iterator = stream.__aiter__()
    while True:
        remaining = None if deadline is None else max(0.0, deadline - asyncio.get_event_loop().time())
        try:
            update = await asyncio.wait_for(iterator.__anext__(), timeout=remaining)
        except StopAsyncIteration:
            return
        yield update


async def run_agent_stream(
    prompt: str,
    *,
    instructions: str | None = None,
    agent_configuration: AgentConfiguration,
    tools: list[Any] | None = None,
    mcp_tools: list[Any] | None = None,
    skill_paths: list[Path] | None = None,
    use_connector_tools: bool = True,
    model: str | None = None,
    session_id: str | None = None,
    sandbox_tools: list[Any] | None = None,
) -> AsyncIterator[str]:
    effective_configuration = _effective_agent_configuration(agent_configuration, model)
    timeout = (
        float(effective_configuration.timeout)
        if effective_configuration.timeout is not None
        else None
    )

    try:
        agent, session, resolved_id = await _build_agent_session_history(
            instructions=instructions,
            agent_configuration=effective_configuration,
            session_id=session_id,
            tools=tools,
            mcp_tools=mcp_tools,
            skill_paths=skill_paths,
            use_connector_tools=use_connector_tools,
            model=None,
            sandbox_tools=sandbox_tools,
        )
    except Exception as exc:
        logger.error('Failed to build agent session: %s', exc, exc_info=True)
        yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
        return

    yield f"data: {json.dumps({'type': 'session', 'session_id': resolved_id})}\n\n"

    lock = await _get_session_lock(resolved_id)
    async with lock:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout if timeout is not None else None
        pending_tool_calls: dict[str, dict[str, Any]] = {}
        emitted_tool_calls: set[str] = set()

        def buffer_function_call(item: Any) -> tuple[str | None, dict[str, Any]]:
            event = _function_call_event(item)
            call_id = event.get('tool_call_id')
            if not isinstance(call_id, str) or not call_id:
                return None, event

            pending = pending_tool_calls.setdefault(
                call_id,
                {
                    'type': 'tool_start',
                    'tool_call_id': call_id,
                    'tool_name': event.get('tool_name'),
                    'arguments': None,
                },
            )
            if event.get('tool_name'):
                pending['tool_name'] = event['tool_name']
            pending['arguments'] = _merge_tool_arguments(
                pending.get('arguments'),
                event.get('arguments'),
            )
            return call_id, pending

        async def emit_tool_start_if_ready(call_id: str, event: dict[str, Any]) -> AsyncIterator[str]:
            if call_id in emitted_tool_calls:
                return
            if not _is_complete_json_argument(event.get('arguments')):
                return
            emitted_tool_calls.add(call_id)
            yield f"data: {json.dumps(event)}\n\n"

        async def emit_tool_start_before_result(call_id: str | None) -> AsyncIterator[str]:
            if call_id is None or call_id in emitted_tool_calls:
                return
            event = pending_tool_calls.get(call_id)
            if event is None:
                return
            emitted_tool_calls.add(call_id)
            yield f"data: {json.dumps(event)}\n\n"

        try:
            stream = _invoke_agent_run(agent, prompt, stream=True, session=session)
            async for update in _iter_with_deadline(stream, deadline):
                for item in getattr(update, 'contents', None) or []:
                    ctype = _content_type(item)
                    if ctype == 'text':
                        text = _content_text(item)
                        if text:
                            yield f"data: {json.dumps({'type': 'delta', 'content': text})}\n\n"
                    elif ctype == 'text_reasoning':
                        text = _content_text(item)
                        if text:
                            yield f"data: {json.dumps({'type': 'intermediate', 'content': text})}\n\n"
                    elif ctype == 'function_call':
                        call_id, event = buffer_function_call(item)
                        if call_id is None:
                            yield f"data: {json.dumps(event)}\n\n"
                        else:
                            async for output in emit_tool_start_if_ready(call_id, event):
                                yield output
                    elif ctype == 'function_result':
                        call_id = getattr(item, 'call_id', None) or getattr(item, 'id', None)
                        async for output in emit_tool_start_before_result(
                            call_id if isinstance(call_id, str) else None
                        ):
                            yield output
                        yield f"data: {json.dumps(_function_result_event(item), default=str)}\n\n"
            for call_id, event in pending_tool_calls.items():
                if call_id not in emitted_tool_calls:
                    emitted_tool_calls.add(call_id)
                    yield f"data: {json.dumps(event)}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except TimeoutError:
            yield f"data: {json.dumps({'type': 'error', 'content': f'Timeout after {timeout}s'})}\n\n"
        except Exception as exc:
            logger.error('Agent stream failed: %s', exc, exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
