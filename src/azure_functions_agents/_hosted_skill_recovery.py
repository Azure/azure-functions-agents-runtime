"""Invocation-local source inspection and bounded handler recovery."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from keyword import iskeyword
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from ._hosted_recovery_input import (
    AUDIT_INSTRUCTIONS,
    RECOVERY_INSTRUCTIONS,
    RecoveryArguments,
    RecoveryInput,
)
from ._hosted_recovery_source import RecoverySource
from ._logger import logger
from ._tool_descriptor import ToolDescriptor
from .hosted_skill_binding import HostedSkill

_UNSET = object()
_FAILURE = "Recovery could not complete. Review the handler inputs and project source, then retry."


def _validate_retry_options(
    *, request_arg: str | None = None, timeout: float = 60, max_retries: int = 1,
    audit_only: bool = False,
) -> None:
    """Validate plain smart-retry options before acquiring execution resources."""
    if request_arg is not None and (
        not isinstance(request_arg, str)
        or not request_arg.isidentifier()
        or iskeyword(request_arg)
    ):
        raise ValueError("smart_retry request_arg must be a parameter name")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("smart_retry timeout must be finite and positive")
    if type(max_retries) is not int or max_retries <= 0:
        raise ValueError("smart_retry max_retries must be a positive integer")
    if type(audit_only) is not bool:
        raise ValueError("smart_retry audit_only must be a boolean")


class _Arguments(RecoveryArguments):
    pass


class _ReadArguments(_Arguments):
    path: str


class _SearchArguments(_Arguments):
    query: str


@dataclass
class _Invocation:
    deadline: float
    active: bool = True
    attempts: int = 0
    retry_in_progress: bool = False
    result: Any = _UNSET
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)

    def check_active(self) -> None:
        if not self.active or asyncio.get_running_loop().time() >= self.deadline:
            raise RuntimeError("Recovery tools are no longer active")


def _relative(path: str, root: Path) -> str | None:
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _prompt(
    request: RecoveryInput[Any], error: Exception, handler: Callable[..., Any], root: Path,
    max_retries: int, audit_only: bool = False,
) -> str:
    try:
        source = inspect.getsourcefile(handler)
    except (OSError, TypeError):
        source = None
    frames = []
    traceback = error.__traceback__
    while traceback is not None:
        code = traceback.tb_frame.f_code
        path = _relative(code.co_filename, root)
        if path is not None:
            frames.append({"path": path, "function": code.co_name, "line": traceback.tb_lineno})
        traceback = traceback.tb_next
    instructions = AUDIT_INSTRUCTIONS if audit_only else RECOVERY_INSTRUCTIONS
    return instructions + "\n" + json.dumps(
        {
            "max_retries": max_retries,
            "audit_only": audit_only,
            "error_type": type(error).__name__,
            "handler": getattr(handler, "__qualname__", type(handler).__name__),
            "source_hint": _relative(source, root) if source else None,
            "traceback": frames[-20:],
            "request": request.diagnostic(),
        },
        allow_nan=False,
    )


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


async def recover_handler[T: BaseModel](
    skill: HostedSkill,
    request: RecoveryInput[T],
    error: Exception,
    handler: Callable[[object], Awaitable[Any]],
    source_handler: Callable[..., Any],
    root: Path,
    timeout: float,
    max_retries: int = 1,
    *,
    audit_only: bool = False,
) -> Any:
    """Recover only after the wrapper's original handler invocation has failed."""
    _validate_retry_options(timeout=timeout, max_retries=max_retries, audit_only=audit_only)
    retry_budget = 0 if audit_only else max_retries
    state = _Invocation(asyncio.get_running_loop().time() + timeout)
    source = RecoverySource(root)

    def tool[A: BaseModel](
        name: str, description: str, model: type[A],
        operation: Callable[[A], Awaitable[Any]],
    ) -> ToolDescriptor:
        async def invoke(**arguments: Any) -> Any:
            state.check_active()
            task = asyncio.current_task()
            if task is not None:
                state.tasks.add(task)
            try:
                try:
                    values = model.model_validate(arguments)
                except ValidationError:
                    raise ValueError("Invalid recovery tool arguments") from None
                return await operation(values)
            except Exception as exc:
                return {"status": "failed", "error_type": type(exc).__name__}
            finally:
                if task is not None:
                    state.tasks.discard(task)

        return ToolDescriptor.create(
            name=name, description=description, func=invoke, parameters=model.model_json_schema()
        )

    async def list_files(_arguments: _Arguments) -> list[str]:
        return source.list_files()

    async def read_file(arguments: _ReadArguments) -> str:
        return source.read_file(arguments.path)

    async def search_source(arguments: _SearchArguments) -> list[str]:
        return source.search_source(arguments.query)

    async def retry_function(arguments: T) -> dict[str, str]:
        if state.result is not _UNSET:
            raise RuntimeError("Recovery has already succeeded")
        if state.retry_in_progress:
            raise RuntimeError("A recovery retry is already running")
        if state.attempts >= retry_budget:
            raise RuntimeError("Recovery retry budget exhausted")
        corrected = request.corrected_arguments(arguments)
        state.attempts += 1
        state.retry_in_progress = True
        try:
            state.result = await handler(corrected)
        finally:
            state.retry_in_progress = False
        return {"status": "succeeded"}

    tools = [
        tool("list_files", "List project-relative Python source files.", _Arguments, list_files),
        tool("read_file", "Read one project-relative Python source file.", _ReadArguments, read_file),
        tool("search_source", "Search project Python source literally.", _SearchArguments, search_source),
    ]
    if not audit_only:
        tools.append(tool(
            "retry_function", f"Retry the original handler with corrected inputs, up to "
            f"{retry_budget} sequential attempts; stop on success. {request.retry_description}",
            request.retry_arguments, retry_function,
        ))
    advice = _FAILURE
    owner = asyncio.current_task()
    cancellations = owner.cancelling() if owner is not None else 0
    try:
        async with asyncio.timeout_at(state.deadline):
            result = await skill._run_recovery(
                _prompt(request, error, source_handler, root, retry_budget, audit_only),
                tools=tools, timeout=timeout, audit_only=audit_only,
            )
            state.check_active()
            advice = result.content.strip()[:2000] or _FAILURE
    except Exception:
        advice = _FAILURE
    finally:
        state.active = False
        pending = state.tasks - {asyncio.current_task()}
        for task in pending:
            task.add_done_callback(_consume_task_exception)
            task.cancel()
        try:
            if pending:
                _, pending = await asyncio.wait(pending, timeout=1)
        finally:
            remaining = {task for task in pending if not task.done()}
            for task in remaining:
                task.cancel()
            if remaining:
                logger.warning(
                    "Recovery tool tasks remain after shutdown; cooperative cancellation "
                    "is required and work may continue after recovery exits."
                )
            # The timeout scope has uncancelled its own request; external cancellation wins.
            if owner is not None and owner.cancelling() > cancellations:
                raise asyncio.CancelledError
    if state.result is not _UNSET:
        return state.result
    if audit_only:
        logger.warning("smart_retry audit-only remediation: %s", advice)
    return request.failure(advice)