from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest

from azure_functions_agents.experimental.hybrid_config import HybridSandboxSettings
from azure_functions_agents.experimental.hybrid_reaper import reap_hybrid_orphans
from azure_functions_agents.transport.transport_models import SandboxSummary


class _Provider:
    def __init__(self, sandboxes: tuple[SandboxSummary, ...]) -> None:
        self.sandboxes = sandboxes
        self.deleted: list[str] = []
        self.closed = False

    async def list_sandboxes(
        self, *, labels: dict[str, str], max_items: int | None = None
    ) -> tuple[SandboxSummary, ...]:
        assert labels == {"owner_kind": "hybrid_spike"}
        assert max_items == 100
        return self.sandboxes

    async def delete_sandbox(self, sandbox_id: str) -> None:
        self.deleted.append(sandbox_id)

    async def close(self) -> None:
        self.closed = True


def _settings() -> HybridSandboxSettings:
    return HybridSandboxSettings(
        group_resource_id="group",
        region="westus2",
        allowed_hosts=(),
        sandbox_disk="python-3.13",
        create_timeout_seconds=90,
        ready_timeout_seconds=45,
        drain_timeout_seconds=10,
        auto_delete_seconds=1800,
        orphan_age_seconds=1200,
    )


@pytest.mark.asyncio
async def test_reaper_deletes_only_expired_labeled_inventory() -> None:
    now = datetime(2026, 9, 2, 22, tzinfo=UTC)
    provider = _Provider(
        (
            SandboxSummary(
                sandbox_id="old",
                labels=MappingProxyType({}),
                created_at=(now - timedelta(seconds=1201)).isoformat(),
            ),
            SandboxSummary(
                sandbox_id="live",
                labels=MappingProxyType({}),
                created_at=(now - timedelta(seconds=100)).isoformat(),
            ),
        )
    )

    async def provider_factory() -> _Provider:
        return provider

    deleted = await reap_hybrid_orphans(
        settings=_settings(),
        provider_factory=provider_factory,
        now=now,
    )

    assert deleted == 1
    assert provider.deleted == ["old"]
    assert provider.closed is True
