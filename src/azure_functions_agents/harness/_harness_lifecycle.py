"""Shutdown callbacks for resources acquired by selected app paths."""

from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack

_SHUTDOWN_CALLBACKS: set[Callable[[], Awaitable[None]]] = set()


def _register_shutdown(callback: Callable[[], Awaitable[None]]) -> None:
    _SHUTDOWN_CALLBACKS.add(callback)


def _unregister_shutdown(callback: Callable[[], Awaitable[None]]) -> None:
    _SHUTDOWN_CALLBACKS.discard(callback)


async def _shutdown_harnesses() -> None:
    """Close only resource owners acquired by selected app paths."""
    async with AsyncExitStack() as cleanup:
        for callback in tuple(_SHUTDOWN_CALLBACKS):
            cleanup.push_async_callback(callback)
