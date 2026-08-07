from __future__ import annotations

import hashlib
import json

import pytest

from azure_functions_agents.controller.bootstrap_delivery import (
    BOOT_READY_PATH,
    BOOTSTRAP_DIGEST_PATH,
    BOOTSTRAP_PATH,
    BootstrapArtifact,
    deliver_content_and_bootstrap,
    deliver_sandbox_bootstrap,
)
from azure_functions_agents.controller.package import (
    CONTENT_ARCHIVE_PATH,
    CapturedContentPackage,
)
from azure_functions_agents.journal_paths import CONTENT_MANIFEST_SEED_PATH
from azure_functions_agents.transport.manifest import ExpectedSandboxManifestBinding
from azure_functions_agents.transport.transport_models import ProvisionedSandboxIdentity
from tests.doubles.fake_sandbox_transport import FakeSandboxTransport

_GROUP_ID = (
    "/subscriptions/subscription/resourceGroups/group/"
    "providers/Microsoft.App/sandboxGroups/sandbox-group"
)


def _expected() -> ExpectedSandboxManifestBinding:
    return ExpectedSandboxManifestBinding.create(
        manifest_version=1,
        protocol_version="1",
        session_id="session-1",
        owner_hash_version="o1",
        owner_hash="o1-" + ("a" * 52),
        app_hash="a1-" + ("b" * 52),
        sandbox_group_resource_id=_GROUP_ID,
        sandbox_id="sandbox-1",
        generation=1,
        digest_kind="funcs_zip",
        digest="sha256:" + ("c" * 64),
        state_store_fingerprint="s1-" + ("d" * 52),
    )


def _package() -> CapturedContentPackage:
    archive = b"captured-content"
    return CapturedContentPackage.create(
        archive_bytes=archive,
        digest_kind="funcs_zip",
        digest=f"sha256:{hashlib.sha256(archive).hexdigest()}",
    )


@pytest.mark.asyncio
async def test_bootstrap_delivery_writes_and_verifies_sentinel_last() -> None:
    transport = FakeSandboxTransport()
    artifact = BootstrapArtifact.create(b"print('bootstrap')\n")

    delivered = await deliver_sandbox_bootstrap(transport, artifact)

    assert delivered == artifact
    write_paths = [call.path for call in transport.calls if call.operation == "write_file"]
    assert write_paths == [BOOTSTRAP_PATH, BOOTSTRAP_DIGEST_PATH, BOOT_READY_PATH]
    assert transport.calls[-1].operation == "read_file"
    assert transport.calls[-1].path == BOOT_READY_PATH
    assert await transport.read_file(BOOT_READY_PATH) == b"ready\n"
    assert await transport.read_file(BOOTSTRAP_DIGEST_PATH) == artifact.digest_sidecar


@pytest.mark.asyncio
async def test_combined_delivery_unblocks_boot_only_after_content_is_verified() -> None:
    transport = FakeSandboxTransport()
    expected = _expected()
    package = _package()
    expected = ExpectedSandboxManifestBinding.create(
        manifest_version=expected.manifest_version,
        protocol_version=expected.protocol_version,
        session_id=expected.session_id,
        owner_hash_version=expected.owner_hash_version,
        owner_hash=expected.owner_hash,
        app_hash=expected.app_hash,
        sandbox_group_resource_id=expected.sandbox_group_resource_id,
        sandbox_id=expected.sandbox_id,
        generation=expected.generation,
        digest_kind=package.digest_kind,
        digest=package.digest,
        state_store_fingerprint=expected.state_store_fingerprint,
    )
    identity = ProvisionedSandboxIdentity.create(
        sandbox_id=expected.sandbox_id,
        group_resource_id=_GROUP_ID,
        region="westus2",
    )

    await deliver_content_and_bootstrap(
        transport,
        package,
        expected,
        identity,
        artifact=BootstrapArtifact.create(b"print('bootstrap')\n"),
    )

    write_paths = [call.path for call in transport.calls if call.operation == "write_file"]
    delete_paths = [call.path for call in transport.calls if call.operation == "delete_file"]
    assert delete_paths == [BOOT_READY_PATH]
    assert write_paths.index(BOOT_READY_PATH) > write_paths.index(CONTENT_ARCHIVE_PATH)
    assert await transport.read_file(BOOT_READY_PATH) == b"ready\n"
    seed = json.loads(
        await transport.read_file(
            CONTENT_MANIFEST_SEED_PATH
        )
    )
    assert seed["digest"] == package.digest


@pytest.mark.asyncio
async def test_bootstrap_delivery_is_idempotent_after_the_ready_sentinel_exists() -> None:
    transport = FakeSandboxTransport()
    transport.seed_file(BOOT_READY_PATH, b"ready\n")

    artifact = BootstrapArtifact.create(b"print('bootstrap')\n")
    delivered = await deliver_sandbox_bootstrap(transport, artifact)

    assert delivered == artifact
    assert await transport.read_file(BOOT_READY_PATH) == b"ready\n"
