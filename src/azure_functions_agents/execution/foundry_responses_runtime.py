"""Lazy app-scoped dependencies for Foundry Hosted Agent Responses."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ..controller.readiness import StateStoreBinding
from ..session_state import (
    AppIdentity,
    build_store_from_service_client,
    get_table_service_client,
)
from ..transport.foundry_responses import FoundryResponsesAdapter, FoundryResponsesTransport
from .foundry_responses_binding import FoundryResponsesRuntimeBinding

type FoundryResponsesTransportFactory = Callable[[], Awaitable[FoundryResponsesTransport]]
type FoundryResponsesStateStoreFactory = Callable[[], Awaitable[StateStoreBinding]]


@dataclass(slots=True)
class _FoundryResponsesAsyncSingleton[T]:
    """Lazily construct one app-scoped async dependency."""

    factory: Callable[[], Awaitable[T]]
    _value: T | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def get(self) -> T:
        if self._value is not None:
            return self._value
        async with self._lock:
            if self._value is None:
                self._value = await self.factory()
            return self._value


@dataclass(frozen=True, slots=True)
class FoundryResponsesRuntime:
    """Validated app binding with lazily opened Responses and Table dependencies."""

    binding: FoundryResponsesRuntimeBinding
    app_identity: AppIdentity
    _transport: _FoundryResponsesAsyncSingleton[FoundryResponsesTransport] = field(
        repr=False,
        compare=False,
    )
    _state_store: _FoundryResponsesAsyncSingleton[StateStoreBinding] = field(
        repr=False,
        compare=False,
    )

    @classmethod
    def create(
        cls,
        *,
        binding: FoundryResponsesRuntimeBinding,
        app_identity: AppIdentity,
        transport_factory: FoundryResponsesTransportFactory | None = None,
        state_store_factory: FoundryResponsesStateStoreFactory | None = None,
    ) -> FoundryResponsesRuntime:
        """Create the local runtime shell without opening network-backed resources."""
        binding.validate_fingerprint(app_identity)

        async def default_transport_factory() -> FoundryResponsesTransport:
            return await FoundryResponsesAdapter.open(
                project_endpoint=binding.project_endpoint,
                agent_name=binding.managed_agent_name,
            )

        async def default_state_store_factory() -> StateStoreBinding:
            service_client, fingerprint = await get_table_service_client()
            store = await build_store_from_service_client(service_client)
            await store.ensure_table()
            return StateStoreBinding.create(
                store=store,
                state_store_fingerprint=fingerprint,
            )

        return cls(
            binding=binding,
            app_identity=app_identity,
            _transport=_FoundryResponsesAsyncSingleton(
                transport_factory or default_transport_factory
            ),
            _state_store=_FoundryResponsesAsyncSingleton(
                state_store_factory or default_state_store_factory
            ),
        )

    async def get_transport(self) -> FoundryResponsesTransport:
        """Return the app-scoped agent-bound Foundry Responses transport."""
        return await self._transport.get()

    async def get_state_store(self) -> StateStoreBinding:
        """Return the app-scoped Table state store after ensuring its table exists."""
        return await self._state_store.get()
