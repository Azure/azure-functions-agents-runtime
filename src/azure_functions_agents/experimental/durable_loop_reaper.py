"""Bounded authoritative-inventory cleanup for durable-loop sandboxes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from .._logger import logger
from ..transport.ports import SandboxSessionProvider
from ..transport.transport_models import SandboxSummary
from .durable_loop_config import DurableLoopSettings
from .hybrid_config import HybridSandboxSettings
from .hybrid_reaper import hybrid_app_hash

DURABLE_LOOP_REAPER_SCHEDULE = "0 */10 * * * *"
_DURABLE_WAVE_OWNER_KIND = "durable_loop_wave"
_DURABLE_RETAINED_OWNER_KIND = "durable_loop_retained"
_MAX_REAPER_ITEMS = 100


async def reap_durable_loop_sandboxes(
    *,
    settings: DurableLoopSettings,
    sandbox_settings: HybridSandboxSettings,
    provider_factory: Callable[
        [],
        Awaitable[SandboxSessionProvider],
    ] | None = None,
    now: datetime | None = None,
    app_hash: str | None = None,
) -> int:
    """Delete only expired app-owned wave and retained sandboxes."""
    factory = provider_factory or _provider_factory(sandbox_settings)
    provider = await factory()
    observed_now = now or datetime.now(UTC)
    resolved_app_hash = app_hash or hybrid_app_hash()
    deleted = 0
    try:
        for owner_kind, maximum_age in (
            (_DURABLE_WAVE_OWNER_KIND, settings.sandbox_reaper_age_seconds),
            (
                _DURABLE_RETAINED_OWNER_KIND,
                settings.retained_sandbox_auto_delete_seconds,
            ),
        ):
            sandboxes = await provider.list_sandboxes(
                labels={
                    "app_hash": resolved_app_hash,
                    "owner_kind": owner_kind,
                },
                max_items=_MAX_REAPER_ITEMS,
            )
            for sandbox in sandboxes:
                if not _is_owned(sandbox, owner_kind, resolved_app_hash):
                    continue
                if not _is_expired(sandbox, observed_now, maximum_age):
                    continue
                try:
                    await provider.delete_sandbox(sandbox.sandbox_id)
                except Exception:
                    logger.exception("Durable-loop sandbox reaper deletion failed.")
                    continue
                deleted += 1
        return deleted
    finally:
        await provider.close()


def _is_owned(
    sandbox: SandboxSummary,
    owner_kind: str,
    app_hash: str,
) -> bool:
    return (
        sandbox.labels.get("owner_kind") == owner_kind
        and sandbox.labels.get("app_hash") == app_hash
    )


def _is_expired(
    sandbox: SandboxSummary,
    now: datetime,
    maximum_age: int,
) -> bool:
    if sandbox.created_at is None:
        return False
    try:
        created_at = datetime.fromisoformat(
            sandbox.created_at.replace("Z", "+00:00")
        )
    except ValueError:
        return False
    if created_at.tzinfo is None:
        return False
    return (now - created_at.astimezone(UTC)).total_seconds() >= maximum_age


def _provider_factory(
    settings: HybridSandboxSettings,
) -> Callable[[], Awaitable[SandboxSessionProvider]]:
    async def open_provider() -> SandboxSessionProvider:
        from ..transport.aca_sdk import AcaSandboxAdapter

        return await AcaSandboxAdapter.open(
            settings.group_resource_id,
            region=settings.region,
        )

    return open_provider
