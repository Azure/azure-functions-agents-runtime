"""Opt-in ACA Sandbox smoke test; never runs without explicit operator approval."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict

import pytest

from azure_functions_agents.transport.aca_sdk import AcaSandboxAdapter
from azure_functions_agents.transport.manifest import (
    SESSION_MANIFEST_PATH,
    ExpectedSandboxManifestBinding,
)
from azure_functions_agents.transport.transport_models import (
    DiskIdSource,
    DiskSource,
    PersistedSandboxBinding,
    PresetSource,
    SandboxCreateRequest,
    SandboxGroupBinding,
    SandboxProvisioningLabels,
)

_APP_HASH = "a1-" + ("b" * 52)
_OWNER_HASH = "o1-" + ("a" * 52)

if os.environ.get("AZURE_FUNCTIONS_AGENTS_RUN_ACA_SMOKE") != "1":
    pytest.skip(
        "Set AZURE_FUNCTIONS_AGENTS_RUN_ACA_SMOKE=1 after human authorization to run live ACA.",
        allow_module_level=True,
    )


def _source_from_environment() -> DiskSource | DiskIdSource | PresetSource:
    values = {
        "disk": os.environ.get("AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_DISK"),
        "disk_id": os.environ.get("AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_DISK_ID"),
        "preset": os.environ.get("AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_PRESET"),
    }
    selected = [(kind, value) for kind, value in values.items() if value]
    if len(selected) != 1:
        pytest.fail("Exactly one explicit ACA smoke source environment variable is required.")
    kind, value = selected[0]
    assert value is not None
    if kind == "disk":
        return DiskSource.create(value)
    if kind == "disk_id":
        return DiskIdSource.create(value)
    return PresetSource.create(value)


async def _force_delete_by_id(adapter: AcaSandboxAdapter, sandbox_id: str) -> None:
    """Reconcile a stopped-but-unresumed sandbox directly by ID.

    Used only from this opt-in smoke test's ``finally`` block, for the one
    window where neither a ``created`` nor a ``resumed`` handle exists (the
    prior handle was closed after ``stop()``, and ``resume()`` itself then
    failed before yielding a new one).
    """

    poller = await adapter._group_client.begin_delete_sandbox(sandbox_id)
    await poller.result()


@pytest.mark.live_aca
@pytest.mark.asyncio
async def test_live_aca_file_exec_stop_resume_delete_smoke() -> None:
    group_resource_id = os.environ.get("AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID")
    if not group_resource_id:
        pytest.fail("AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID is required.")

    session_id = f"p4a-smoke-{uuid.uuid4().hex}"
    adapter = await AcaSandboxAdapter.open(group_resource_id)
    created = None
    resumed = None
    sandbox_id: str | None = None
    deleted = False
    try:
        group_binding = SandboxGroupBinding.create(
            resource_id=adapter.group.resource_id,
            region=adapter.group.region,
        )
        request = SandboxCreateRequest.create(
            source=_source_from_environment(),
            labels=SandboxProvisioningLabels.create(
                owner_hash_version="o1",
                owner_kind="function_app",
                owner_hash=_OWNER_HASH,
                app_hash=_APP_HASH,
                session_id=session_id,
            ),
            remaining_setup_budget_seconds=30.0,
            environment={"P4A_SMOKE": "1"},
        )
        created = await adapter.create(request, persisted_group=group_binding)
        # Captured immediately so `finally` can always reconcile this sandbox,
        # even across the stop()/close()/resume() window below where neither
        # `created` nor `resumed` is set.
        sandbox_id = created.identity.sandbox_id

        root = f"/tmp/{session_id}"
        path = f"{root}/file.bin"
        await created.mkdir(root)
        await created.write_file(path, b"p4a-direct-file")
        listed = await created.list_files(root)
        stat = await created.stat_file(path)
        assert any(entry.path == path for entry in listed)
        assert stat.size == len(b"p4a-direct-file")
        assert await created.read_file(path) == b"p4a-direct-file"
        await created.delete_file(path)

        # Overwrite semantics: write, then a second *direct* write with distinct
        # content of a different length, and require the final read-back and
        # size to match the overwrite exactly. Production has no delete/write
        # fallback, so this must fail here if the real data plane does not
        # actually replace the prior bytes on a second direct write.
        overwrite_path = f"{root}/overwrite.bin"
        first_content = b"p4a-overwrite-before"
        second_content = b"p4a-overwrite-after-with-a-different-length"
        await created.write_file(overwrite_path, first_content)
        assert await created.read_file(overwrite_path) == first_content

        await created.write_file(overwrite_path, second_content)
        overwrite_stat = await created.stat_file(overwrite_path)
        assert overwrite_stat.size == len(second_content)
        assert await created.read_file(overwrite_path) == second_content
        await created.delete_file(overwrite_path)

        expected = ExpectedSandboxManifestBinding.create(
            manifest_version=1,
            protocol_version="p4a-smoke-v1",
            session_id=session_id,
            owner_hash_version="o1",
            owner_hash=_OWNER_HASH,
            app_hash=_APP_HASH,
            sandbox_group_resource_id=group_binding.resource_id,
            sandbox_id=sandbox_id,
            generation=1,
            digest_kind="smoke",
            digest="sha256:" + "b" * 64,
            state_store_fingerprint="s1-" + ("c" * 52),
        )
        await created.write_file(
            SESSION_MANIFEST_PATH,
            json.dumps(asdict(expected), sort_keys=True).encode("utf-8"),
            create_dirs=True,
        )
        result = await created.exec("printf p4a-smoke", timeout_seconds=10)
        assert result.exit_code == 0
        assert result.stdout == "p4a-smoke"

        await created.stop()
        await created.close()
        created = None

        resumed = await adapter.resume(
            PersistedSandboxBinding.create(sandbox_id=sandbox_id, group=group_binding),
            expected,
            readiness_timeout_seconds=30,
        )
        assert await resumed.read_file(SESSION_MANIFEST_PATH)
        await resumed.delete()
        deleted = True
    finally:
        if resumed is not None:
            if not deleted:
                await resumed.delete()
            await resumed.close()
        elif created is not None:
            if not deleted:
                await created.delete()
            await created.close()
        elif sandbox_id is not None and not deleted:
            await _force_delete_by_id(adapter, sandbox_id)
        await adapter.close()
