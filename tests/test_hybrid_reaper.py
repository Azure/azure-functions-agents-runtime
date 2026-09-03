from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest

from azure_functions_agents.experimental.hybrid_config import HybridSandboxSettings
from azure_functions_agents.experimental.hybrid_reaper import (
    hybrid_app_hash,
    reap_hybrid_orphans,
)
from azure_functions_agents.session_state import (
    AppIdentity,
    AppIdentityResolutionError,
    compute_app_hash,
)
from azure_functions_agents.transport.transport_models import SandboxSummary


class _Provider:
    def __init__(self, sandboxes: tuple[SandboxSummary, ...]) -> None:
        self.sandboxes = sandboxes
        self.deleted: list[str] = []
        self.closed = False

    async def list_sandboxes(
        self, *, labels: dict[str, str], max_items: int | None = None
    ) -> tuple[SandboxSummary, ...]:
        assert labels == {
            "owner_kind": "hybrid_spike",
            "app_hash": f"a1-{'a' * 52}",
        }
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
        orphan_age_seconds=1200,
    )


_APP_HASH = f"a1-{'a' * 52}"


def _owned_labels() -> MappingProxyType[str, str]:
    return MappingProxyType({"owner_kind": "hybrid_spike", "app_hash": _APP_HASH})


@pytest.mark.asyncio
async def test_reaper_deletes_only_expired_labeled_inventory() -> None:
    now = datetime(2026, 9, 2, 22, tzinfo=UTC)
    provider = _Provider(
        (
            SandboxSummary(
                sandbox_id="old",
                labels=_owned_labels(),
                created_at=(now - timedelta(seconds=1201)).isoformat(),
            ),
            SandboxSummary(
                sandbox_id="live",
                labels=_owned_labels(),
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
        app_hash=_APP_HASH,
    )

    assert deleted == 1
    assert provider.deleted == ["old"]
    assert provider.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "labels",
    [
        MappingProxyType({}),
        MappingProxyType({"owner_kind": "hybrid_spike"}),
        MappingProxyType({"app_hash": _APP_HASH}),
        MappingProxyType({"owner_kind": "other_kind", "app_hash": _APP_HASH}),
        MappingProxyType({"owner_kind": "hybrid_spike", "app_hash": f"a1-{'b' * 52}"}),
        MappingProxyType(
            {"owner_kind": "hybrid_spike ", "app_hash": _APP_HASH}
        ),
    ],
)
async def test_reaper_skips_expired_inventory_whose_labels_do_not_match(
    labels: MappingProxyType[str, str],
) -> None:
    now = datetime(2026, 9, 2, 22, tzinfo=UTC)
    provider = _Provider(
        (
            SandboxSummary(
                sandbox_id="unowned",
                labels=labels,
                created_at=(now - timedelta(seconds=1201)).isoformat(),
            ),
        )
    )

    async def provider_factory() -> _Provider:
        return provider

    deleted = await reap_hybrid_orphans(
        settings=_settings(),
        provider_factory=provider_factory,
        now=now,
        app_hash=_APP_HASH,
    )

    assert deleted == 0
    assert provider.deleted == []
    assert provider.closed is True


@pytest.mark.asyncio
async def test_reaper_deletes_owned_inventory_beside_a_mismatched_neighbor() -> None:
    now = datetime(2026, 9, 2, 22, tzinfo=UTC)
    expired = (now - timedelta(seconds=1201)).isoformat()
    provider = _Provider(
        (
            SandboxSummary(
                sandbox_id="other-app",
                labels=MappingProxyType(
                    {"owner_kind": "hybrid_spike", "app_hash": f"a1-{'c' * 52}"}
                ),
                created_at=expired,
            ),
            SandboxSummary(
                sandbox_id="owned",
                labels=_owned_labels(),
                created_at=expired,
            ),
        )
    )

    async def provider_factory() -> _Provider:
        return provider

    deleted = await reap_hybrid_orphans(
        settings=_settings(),
        provider_factory=provider_factory,
        now=now,
        app_hash=_APP_HASH,
    )

    assert deleted == 1
    assert provider.deleted == ["owned"]


@pytest.mark.asyncio
async def test_reaper_rejects_invalid_explicit_app_hash() -> None:
    with pytest.raises(ValueError, match="app hash is invalid"):
        await reap_hybrid_orphans(settings=_settings(), app_hash="shared-root")


def test_hybrid_app_hash_uses_stable_platform_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = AppIdentity.create(
        "00000000-0000-0000-0000-000000000000",
        "function-app",
    )
    monkeypatch.setattr(
        "azure_functions_agents.experimental.hybrid_reaper.resolve_function_app_identity",
        lambda: identity,
    )

    assert hybrid_app_hash() == compute_app_hash(identity)


def test_hybrid_app_hash_fails_closed_without_platform_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in ("WEBSITE_OWNER_NAME", "WEBSITE_SITE_NAME", "WEBSITE_SLOT_NAME"):
        monkeypatch.delenv(variable, raising=False)

    with pytest.raises(AppIdentityResolutionError):
        hybrid_app_hash()


@pytest.mark.asyncio
async def test_reaper_fails_closed_before_listing_when_identity_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in ("WEBSITE_OWNER_NAME", "WEBSITE_SITE_NAME", "WEBSITE_SLOT_NAME"):
        monkeypatch.delenv(variable, raising=False)
    factory_calls = 0

    async def provider_factory() -> _Provider:
        nonlocal factory_calls
        factory_calls += 1
        return _Provider(())

    with pytest.raises(AppIdentityResolutionError):
        await reap_hybrid_orphans(
            settings=_settings(),
            provider_factory=provider_factory,
        )

    assert factory_calls == 0
