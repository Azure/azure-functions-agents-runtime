from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from azure_functions_agents.experimental.durable_loop_config import DurableLoopSettings
from azure_functions_agents.experimental.durable_loop_reaper import (
    reap_durable_loop_sandboxes,
)
from azure_functions_agents.experimental.hybrid_config import HybridSandboxSettings
from azure_functions_agents.transport.transport_models import SandboxSummary


class _Provider:
    group = SimpleNamespace(resource_id="group", region="eastus2")

    def __init__(self, items: dict[str, tuple[SandboxSummary, ...]]) -> None:
        self.items = items
        self.deleted: list[str] = []
        self.closed = 0

    async def list_sandboxes(self, *, labels, max_items=None):
        del max_items
        return self.items.get(labels["owner_kind"], ())

    async def delete_sandbox(self, sandbox_id: str) -> None:
        self.deleted.append(sandbox_id)

    async def close(self) -> None:
        self.closed += 1


@pytest.mark.asyncio
async def test_reaper_filters_owner_and_uses_profile_age_bounds() -> None:
    now = datetime(2026, 9, 7, tzinfo=UTC)
    app_hash = "h1-app"
    old = (now - timedelta(hours=2)).isoformat()
    recent = (now - timedelta(minutes=2)).isoformat()
    provider = _Provider(
        {
            "durable_loop_wave": (
                SandboxSummary.create(
                    sandbox_id="wave-old",
                    labels={
                        "owner_kind": "durable_loop_wave",
                        "app_hash": app_hash,
                    },
                    created_at=old,
                ),
                SandboxSummary.create(
                    sandbox_id="wave-recent",
                    labels={
                        "owner_kind": "durable_loop_wave",
                        "app_hash": app_hash,
                    },
                    created_at=recent,
                ),
            ),
            "durable_loop_retained": (
                SandboxSummary.create(
                    sandbox_id="retained-old",
                    labels={
                        "owner_kind": "durable_loop_retained",
                        "app_hash": app_hash,
                    },
                    created_at=old,
                ),
            ),
        }
    )
    sandbox_settings = HybridSandboxSettings(
        group_resource_id=(
            "/subscriptions/s/resourceGroups/r/providers/"
            "Microsoft.App/sandboxGroups/g"
        ),
        region="eastus2",
        allowed_hosts=(),
        sandbox_disk="python-3.13",
        create_timeout_seconds=30,
        ready_timeout_seconds=30,
        drain_timeout_seconds=5,
        orphan_age_seconds=1200,
    )

    deleted = await reap_durable_loop_sandboxes(
        settings=DurableLoopSettings(
            sandbox_reaper_age_seconds=600,
            retained_sandbox_auto_delete_seconds=3600,
        ),
        sandbox_settings=sandbox_settings,
        provider_factory=lambda: _provider(provider),
        now=now,
        app_hash=app_hash,
    )

    assert deleted == 2
    assert provider.deleted == ["wave-old", "retained-old"]
    assert provider.closed == 1


async def _provider(provider: _Provider) -> _Provider:
    return provider
