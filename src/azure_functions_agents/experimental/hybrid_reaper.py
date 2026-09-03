"""Bounded worker-crash orphan reaping for hybrid invocation sandboxes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from .._logger import logger
from ..session_state import (
    APP_HASH_VERSION,
    LABEL_SAFE_PAYLOAD_PATTERN,
    compute_app_hash,
    resolve_function_app_identity,
)
from ..transport.ports import SandboxSessionProvider
from ..transport.transport_models import SandboxSummary
from .hybrid_config import HybridSandboxSettings
from .hybrid_observability import HybridMetric, record_hybrid_count

HYBRID_OWNER_KIND = "hybrid_spike"
HYBRID_REAPER_SCHEDULE = "0 */10 * * * *"
_MAX_REAPER_ITEMS = 100


async def reap_hybrid_orphans(
    *,
    settings: HybridSandboxSettings | None = None,
    provider_factory: Callable[[], Awaitable[SandboxSessionProvider]] | None = None,
    now: datetime | None = None,
    app_hash: str | None = None,
) -> int:
    """Delete only expired sandboxes owned by this Function App's hybrid runtime."""
    resolved = settings or HybridSandboxSettings.from_environment()
    if resolved is None:
        return 0
    resolved_app_hash = _validate_app_hash(app_hash or hybrid_app_hash())
    factory = provider_factory or _provider_factory(
        resolved.group_resource_id,
        resolved.region,
    )
    provider = await factory()
    deleted = 0
    observed_now = now or datetime.now(UTC)
    try:
        sandboxes = await provider.list_sandboxes(
            labels={
                "owner_kind": HYBRID_OWNER_KIND,
                "app_hash": resolved_app_hash,
            },
            max_items=_MAX_REAPER_ITEMS,
        )
        for sandbox in sandboxes:
            if not _is_expired(sandbox, observed_now, resolved.orphan_age_seconds):
                continue
            try:
                await provider.delete_sandbox(sandbox.sandbox_id)
            except Exception:
                record_hybrid_count(HybridMetric.SANDBOX_DELETE_FAILURES)
                logger.exception("Hybrid orphan sandbox deletion failed.")
                continue
            deleted += 1
            record_hybrid_count(HybridMetric.SANDBOX_REAPED)
        return deleted
    finally:
        await provider.close()


def hybrid_app_hash() -> str:
    """Return the stable platform-derived application label for hybrid ownership."""
    return compute_app_hash(resolve_function_app_identity())


def _validate_app_hash(value: str) -> str:
    version, separator, payload = value.partition("-")
    if (
        not separator
        or version != APP_HASH_VERSION
        or LABEL_SAFE_PAYLOAD_PATTERN.fullmatch(payload) is None
    ):
        raise ValueError("Hybrid reaper app hash is invalid.")
    return value


def _provider_factory(
    group_resource_id: str,
    region: str,
) -> Callable[[], Awaitable[SandboxSessionProvider]]:
    async def open_provider() -> SandboxSessionProvider:
        from ..transport.aca_sdk import AcaSandboxAdapter

        return await AcaSandboxAdapter.open(group_resource_id, region=region)

    return open_provider


def _is_expired(
    sandbox: SandboxSummary,
    now: datetime,
    orphan_age_seconds: int,
) -> bool:
    if sandbox.created_at is None:
        return False
    try:
        created_at = datetime.fromisoformat(sandbox.created_at.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Hybrid orphan sandbox has an invalid creation timestamp.")
        return False
    if created_at.tzinfo is None:
        logger.warning("Hybrid orphan sandbox has a timezone-free creation timestamp.")
        return False
    return (now - created_at.astimezone(UTC)).total_seconds() > orphan_age_seconds
