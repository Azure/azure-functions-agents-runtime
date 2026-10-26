from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from azure.core.exceptions import (
    ResourceExistsError,
    ResourceNotFoundError,
    ServiceRequestError,
)

from azure_functions_agents.durable._reference_store import (
    BlobDurableReferenceStore,
    ReferenceExpiredError,
    ReferenceIntegrityError,
    ReferenceNotFoundError,
    ReferenceScope,
    ReferenceStoreError,
)
from azure_functions_agents.harness._checkpoint import ContinuationRole
from azure_functions_agents.harness._harness_binding import HarnessKind
from azure_functions_agents.harness._session_storage import OwnedBlobService

_OWNER_DIGEST = "b" * 64


class _Downloader:
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def readall(self) -> bytes:
        return self.content


class _Blob:
    def __init__(self, service, name: str) -> None:
        self.service = service
        self.name = name

    async def upload_blob(
        self,
        content: bytes,
        *,
        overwrite: bool,
        metadata: dict[str, str],
    ) -> None:
        failure = self.service.failures.get(("upload", self.name))
        if failure:
            raise failure
        if not overwrite and self.name in self.service.blobs:
            raise ResourceExistsError("exists")
        self.service.blobs[self.name] = {
            "content": content,
            "metadata": dict(metadata),
        }

    async def get_blob_properties(self):
        failure = self.service.failures.get(("properties", self.name))
        if failure:
            raise failure
        record = self.service.blobs.get(self.name)
        if record is None:
            raise ResourceNotFoundError("missing")
        return SimpleNamespace(
            size=len(record["content"]),
            metadata=record["metadata"],
        )

    async def download_blob(self):
        failure = self.service.failures.get(("download", self.name))
        if failure:
            raise failure
        record = self.service.blobs.get(self.name)
        if record is None:
            raise ResourceNotFoundError("missing")
        return _Downloader(record["content"])

    async def delete_blob(self) -> None:
        if self.name not in self.service.blobs:
            raise ResourceNotFoundError("missing")
        del self.service.blobs[self.name]


class _Container:
    def __init__(self, service) -> None:
        self.service = service

    async def create_container(self) -> None:
        if self.service.container_created:
            raise ResourceExistsError("exists")
        self.service.container_created = True


class _BlobService:
    def __init__(self) -> None:
        self.blobs: dict[str, dict[str, object]] = {}
        self.failures: dict[tuple[str, str], Exception] = {}
        self.container_created = False
        self.closed = False

    def get_container_client(self, container: str):
        del container
        return _Container(self)

    def get_blob_client(self, *, container: str, blob: str):
        del container
        return _Blob(self, blob)

    async def close(self) -> None:
        self.closed = True


def _scope(**changes) -> ReferenceScope:
    values = {
        "backend_namespace": "hub-a",
        "agent_slug": "agent",
        "owner_scope_digest": _OWNER_DIGEST,
        "entry_id": "entry",
        "session_id": "session",
        "session_incarnation_id": "incarnation",
        "run_id": "run",
        "turn_generation": 3,
    }
    values.update(changes)
    return ReferenceScope.create(**values)


def _store() -> tuple[BlobDurableReferenceStore, _BlobService]:
    service = _BlobService()
    owned = OwnedBlobService(service)
    return (
        BlobDurableReferenceStore(
            owned,
            container_name="container",
            backend_namespace="hub-a",
        ),
        service,
    )


def _expires() -> datetime:
    return datetime.now(UTC) + timedelta(days=1)


def test_reference_scope_requires_validated_factory() -> None:
    with pytest.raises(TypeError):
        ReferenceScope(
            backend_namespace="hub-a",
            agent_slug="agent",
            owner_scope_digest=_OWNER_DIGEST,
            entry_id="entry",
            session_id="session",
            session_incarnation_id="incarnation",
            run_id="run",
            turn_generation=3,
        )

    scope = ReferenceScope.create(
        backend_namespace=" hub-a ",
        agent_slug=" agent ",
        owner_scope_digest=_OWNER_DIGEST,
        entry_id=" entry ",
        session_id=" session ",
        session_incarnation_id=" incarnation ",
        run_id=" run ",
        turn_generation=3,
    )

    assert scope.backend_namespace == "hub-a"
    assert scope.agent_slug == "agent"
    assert scope.entry_id == "entry"
    assert scope.session_id == "session"
    assert scope.session_incarnation_id == "incarnation"
    assert scope.run_id == "run"

    with pytest.raises(ValueError, match="turn generation"):
        _scope(turn_generation=True)


@pytest.mark.asyncio
async def test_content_round_trip_uses_opaque_immutable_key() -> None:
    store, service = _store()
    scope = _scope()
    await store.initialize()

    reference = await store.write_content(
        scope,
        b'{"value":1}',
        content_type="application/json",
        expires_at=_expires(),
    )

    assert await store.read_content(scope, reference) == b'{"value":1}'
    assert scope.session_id not in reference.opaque_content_key
    assert reference.opaque_content_key in service.blobs


@pytest.mark.asyncio
async def test_continuation_round_trip_verifies_authorized_scope() -> None:
    store, _ = _store()
    scope = _scope()
    reference = await store.write_continuation(
        scope,
        b"native-state",
        harness_kind=HarnessKind.MAF,
        connector_version="1",
        connector_profile_id="maf-test",
        state_format_version="1",
        continuation_role=ContinuationRole.MID_TURN,
        expires_at=_expires(),
    )

    assert await store.read_continuation(scope, reference) == b"native-state"
    with pytest.raises(ReferenceIntegrityError, match="authorized scope"):
        await store.read_continuation(_scope(run_id="different"), reference)


@pytest.mark.asyncio
async def test_read_rejects_tampered_content_and_metadata() -> None:
    store, service = _store()
    scope = _scope()
    reference = await store.write_content(
        scope,
        b"original",
        content_type="text/plain",
        expires_at=_expires(),
    )
    record = service.blobs[reference.opaque_content_key]
    record["content"] = b"tampered"

    with pytest.raises(ReferenceIntegrityError, match="digest verification"):
        await store.read_content(scope, reference)

    record["content"] = b"original"
    record["metadata"]["scope_digest"] = "wrong"
    with pytest.raises(ReferenceIntegrityError, match="metadata verification"):
        await store.read_content(scope, reference)


@pytest.mark.asyncio
async def test_read_binds_reference_lifetime_to_blob_metadata() -> None:
    store, service = _store()
    scope = _scope()
    reference = await store.write_content(
        scope,
        b"original",
        content_type="text/plain",
        expires_at=_expires(),
    )

    altered_reference = reference.model_copy(
        update={"expires_at": reference.expires_at + timedelta(days=1)}
    )
    with pytest.raises(ReferenceIntegrityError, match="metadata verification"):
        await store.read_content(scope, altered_reference)

    record = service.blobs[reference.opaque_content_key]
    record["metadata"]["expires_at"] = (reference.expires_at + timedelta(days=1)).isoformat()
    with pytest.raises(ReferenceIntegrityError, match="metadata verification"):
        await store.read_content(scope, reference)


@pytest.mark.asyncio
async def test_missing_expired_and_deleted_references_fail_explicitly() -> None:
    store, service = _store()
    scope = _scope()
    reference = await store.write_content(
        scope,
        b"value",
        content_type="text/plain",
        expires_at=_expires(),
    )
    expired = reference.model_copy(
        update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
    )
    with pytest.raises(ReferenceExpiredError):
        await store.read_content(scope, expired)

    del service.blobs[reference.opaque_content_key]
    with pytest.raises(ReferenceNotFoundError):
        await store.read_content(scope, reference)

    await store.delete(scope, reference)


@pytest.mark.asyncio
async def test_backend_failures_are_sanitized() -> None:
    store, service = _store()
    scope = _scope()
    reference = await store.write_content(
        scope,
        b"value",
        content_type="text/plain",
        expires_at=_expires(),
    )
    service.failures[("properties", reference.opaque_content_key)] = ServiceRequestError(
        "fixture-secret"
    )

    with pytest.raises(ReferenceStoreError, match="could not be read") as error:
        await store.read_content(scope, reference)
    assert "fixture-secret" not in str(error.value)


@pytest.mark.asyncio
async def test_invalid_metadata_is_rejected_before_upload() -> None:
    store, service = _store()

    with pytest.raises(ValueError, match="content type exceeds"):
        await store.write_content(
            _scope(),
            b"value",
            content_type="x" * 256,
            expires_at=_expires(),
        )

    with pytest.raises(ValueError, match="harness kind is invalid"):
        await store.write_continuation(
            _scope(),
            b"native-state",
            harness_kind="maf",
            connector_version="1",
            connector_profile_id="maf-test",
            state_format_version="1",
            continuation_role=ContinuationRole.MID_TURN,
            expires_at=_expires(),
        )

    with pytest.raises(ValueError, match="continuation role is invalid"):
        await store.write_continuation(
            _scope(),
            b"native-state",
            harness_kind=HarnessKind.MAF,
            connector_version="1",
            connector_profile_id="maf-test",
            state_format_version="1",
            continuation_role="mid_turn",
            expires_at=_expires(),
        )

    assert service.blobs == {}


@pytest.mark.asyncio
async def test_store_closes_owned_blob_service() -> None:
    store, service = _store()

    await store.close()

    assert service.closed is True
