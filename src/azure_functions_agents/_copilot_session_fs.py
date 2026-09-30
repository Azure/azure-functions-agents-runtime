"""Lease-fenced, single-envelope native Copilot session filesystem."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from azure.core import MatchConditions
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)
from azure.storage.blob import StorageErrorCode
from copilot.generated.rpc import DebugCollectLogsEntryKind, SessionFSReaddirWithTypesEntry
from copilot.session_fs_provider import SessionFsFileInfo, SessionFsProvider
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ._native_session_identity import (
    CorruptSessionError,
    IncompatibleSessionError,
    LeaseLostError,
    NativeSessionError,
    PersistenceUnavailableError,
    SessionCapacityError,
    SessionConflictError,
    StorageMode,
    StorageRoute,
    _open_owned_blob_service,
    _OwnedBlobService,
    state_name,
    validate_identity,
)

if TYPE_CHECKING:
    from azure.storage.blob.aio import BlobClient, BlobLeaseClient, BlobServiceClient

    from ._harness import AppHarness

SDK_VERSION = "1.0.14"
NATIVE_VERSION = "1.0.85"
PROTOCOL_VERSION = 4
MAX_ENVELOPE_BYTES = 4 * 1024 * 1024
LEASE_SECONDS = 30


class SessionState(StrEnum):
    EMPTY = "empty"
    PREPARING = "preparing"
    ACTIVE = "active"
    READY = "ready"
    UNCERTAIN = "uncertain"
    DELETED = "deleted"


def normalize_path(path: str) -> str:
    if not isinstance(path, str) or "\x00" in path or "\\" in path:
        raise ValueError("Invalid native filesystem path.")
    parts: list[str] = []
    for part in path.split("/"):
        if part in {"", "."}:
            continue
        if part == ".." or any(ord(c) < 32 for c in part):
            raise ValueError("Native filesystem path escapes its virtual root.")
        parts.append(part)
    return "/" + "/".join(parts)


type PathConventions = Literal["posix", "windows"]

HOST_PATH_CONVENTIONS: PathConventions = "windows" if os.name == "nt" else "posix"

WORKSPACE_ROOT = "/workspace"
SESSION_STATE_ROOT = "/session-state"
VIRTUAL_ROOTS = (WORKSPACE_ROOT, SESSION_STATE_ROOT)


def _workspace_relative(path: str, alias: str, conventions: PathConventions) -> str | None:
    """Return the alias-relative remainder, or None when the path is not under the alias."""
    if not alias:
        return None
    subject = path.replace("\\", "/") if conventions == "windows" else path
    prefix = (alias.replace("\\", "/") if conventions == "windows" else alias).rstrip("/")
    if not prefix:
        return None
    compared, against = (
        (subject.casefold(), prefix.casefold()) if conventions == "windows" else (subject, prefix)
    )
    if compared == against:
        return ""
    if compared.startswith(against + "/"):
        return subject[len(prefix) + 1 :]
    return None


def normalize_callback_path(
    path: str, conventions: PathConventions, workspace_aliases: Sequence[str] = ()
) -> str:
    """Canonicalize one SDK path against the virtual session roots.

    Declared host workspace roots map onto ``/workspace``; any other host-qualified
    path is rejected instead of being adopted into the virtual tree.
    """
    if not isinstance(path, str) or "\x00" in path:
        raise ValueError("Invalid native filesystem path.")
    for alias in workspace_aliases:
        remainder = _workspace_relative(path, alias, conventions)
        if remainder is not None:
            return normalize_path(f"{WORKSPACE_ROOT}/{remainder}")
    if path.startswith(("\\\\", "//")) or re.match(r"[A-Za-z]:", path):
        raise ValueError("Invalid native filesystem path.")
    if conventions == "windows":
        path = path.replace("\\", "/")
    normalized = normalize_path(path)
    if normalized != "/" and not any(
        normalized == root or normalized.startswith(f"{root}/") for root in VIRTUAL_ROOTS
    ):
        raise _PathResultError(errno.EACCES, normalized)
    return normalized


class _Document(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FileMetadata(_Document):
    birthtime: float
    mtime: float

    @model_validator(mode="after")
    def valid_times(self) -> FileMetadata:
        if not math.isfinite(self.birthtime) or not math.isfinite(self.mtime):
            raise ValueError("Invalid native filesystem time.")
        return self


class FileContent(FileMetadata):
    content: str
    size_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def valid_size(self) -> FileContent:
        if len(self.content.encode("utf-8")) != self.size_bytes:
            raise ValueError("Invalid native filesystem size.")
        return self


class WorkingTree(_Document):
    directories: dict[str, FileMetadata]
    files: dict[str, FileContent]

    @model_validator(mode="after")
    def valid_paths(self) -> WorkingTree:
        if "/" not in self.directories:
            raise ValueError("Native filesystem root is missing.")
        if set(self.directories) & set(self.files):
            raise ValueError("Native filesystem path collision.")
        for path in (*self.directories, *self.files):
            if path != normalize_path(path):
                raise ValueError("Noncanonical native filesystem path.")
            if path != "/":
                parent = path.rpartition("/")[0] or "/"
                if parent not in self.directories:
                    raise ValueError("Native filesystem parent is missing.")
        if "/" in self.files:
            raise ValueError("Native filesystem root is a file.")
        return self


def empty_tree() -> WorkingTree:
    """Start every tree with the virtual workspace root the native runtime resolves its cwd to."""
    now = time.time()
    return WorkingTree(
        directories={
            "/": FileMetadata(birthtime=now, mtime=now),
            WORKSPACE_ROOT: FileMetadata(birthtime=now, mtime=now),
        },
        files={},
    )


class StateEnvelope(_Document):
    schema_version: Literal[1] = 1
    sdk_version: Literal["1.0.14"] = "1.0.14"
    native_version: Literal["1.0.85"] = "1.0.85"
    protocol_version: Literal[4] = 4
    app_key: str
    agent_slug: str
    session_id: str
    native_session_id: str
    owner_epoch: int = Field(ge=0)
    revision: int = Field(ge=0)
    state: SessionState
    workspace_path: str = ""
    handoff_may_have_started: bool = False
    working: WorkingTree | None = None
    completed: WorkingTree | None = None
    integrity_sha256: str = ""

    @field_validator("state", mode="before")
    @classmethod
    def parse_state(cls, state: object) -> SessionState:
        if isinstance(state, str):
            return SessionState(state)
        raise ValueError("Invalid native session state.")

    @model_validator(mode="after")
    def valid_state(self) -> StateEnvelope:
        validate_identity(self.agent_slug, self.session_id)
        if (
            self.state in {SessionState.EMPTY, SessionState.DELETED}
            and (self.working is not None or self.completed is not None
                 or self.handoff_may_have_started)
        ) or (
            self.state is SessionState.READY
            and (self.working is not None or self.completed is None or self.handoff_may_have_started)
        ) or (
            self.state in {SessionState.PREPARING, SessionState.ACTIVE, SessionState.UNCERTAIN}
            and self.working is None
        ):
            raise ValueError("Invalid native session state.")
        if self.state is SessionState.PREPARING and self.handoff_may_have_started:
            raise ValueError("Invalid native handoff marker.")
        return self


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def serialize(envelope: StateEnvelope) -> bytes:
    fields = envelope.model_dump(mode="json", exclude={"integrity_sha256"})
    checksum = hashlib.sha256(_canonical(fields)).hexdigest()
    payload = _canonical({**fields, "integrity_sha256": checksum})
    if len(payload) > MAX_ENVELOPE_BYTES:
        raise SessionCapacityError(
            "Native session exceeds the single-object storage limit; use a new ID."
        )
    return payload


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate native session field.")
        result[key] = value
    return result


def deserialize(data: bytes, route: StorageRoute, slug: str, session_id: str,
                native_id: str) -> StateEnvelope:
    try:
        if len(data) > MAX_ENVELOPE_BYTES:
            raise ValueError("Oversize native session.")
        raw = json.loads(data, object_pairs_hook=_no_duplicates)
        if isinstance(raw, dict) and (
            raw.get("schema_version") != 1
            or raw.get("sdk_version") != SDK_VERSION
            or raw.get("native_version") != NATIVE_VERSION
            or raw.get("protocol_version") != PROTOCOL_VERSION
        ):
            raise IncompatibleSessionError("Native session format or SDK version is unsupported.")
        envelope = StateEnvelope.model_validate(raw)
        expected = hashlib.sha256(
            _canonical(envelope.model_dump(mode="json", exclude={"integrity_sha256"}))
        ).hexdigest()
        if envelope.integrity_sha256 != expected:
            raise ValueError("Invalid native session integrity.")
    except IncompatibleSessionError:
        raise
    except (ValueError, UnicodeError, ValidationError, TypeError):
        raise CorruptSessionError("Native session state is corrupt; no reset was attempted.") from None
    if (envelope.app_key, envelope.agent_slug, envelope.session_id,
            envelope.native_session_id) != (route.app_key, slug, session_id, native_id):
        raise IncompatibleSessionError("Native session identity does not match this app and agent.")
    return envelope


def _new_envelope(route: StorageRoute, slug: str, session_id: str,
                  native_id: str) -> StateEnvelope:
    return StateEnvelope(
        app_key=route.app_key, agent_slug=slug, session_id=session_id,
        native_session_id=native_id, owner_epoch=0, revision=0, state=SessionState.EMPTY,
    )


class _LocalStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def _safe_paths(self) -> None:
        if any(
            part.is_symlink() or (sys.platform == "win32" and part.is_junction())
            for part in (self.path, self.path.with_suffix(".lock"), *self.path.parents)
        ):
            raise PersistenceUnavailableError("Native local storage cannot traverse symlinks.")

    async def acquire(self, deadline: float) -> None:
        if sys.platform not in {"linux", "win32"}:
            raise PersistenceUnavailableError("Native local locking supports Linux and Windows only.")

        try:
            self._safe_paths()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._safe_paths()
            fd = os.open(
                self.path.with_suffix(".lock"),
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600,
            )
            self._safe_paths()
            if sys.platform == "win32" and os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            while True:
                try:
                    if sys.platform == "win32":
                        import msvcrt

                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._fd = fd
                    return
                except OSError as exc:
                    if sys.platform == "win32" and exc.errno not in {13, 36}:
                        raise
                    if sys.platform != "win32" and not isinstance(exc, BlockingIOError):
                        raise
                    if asyncio.get_running_loop().time() >= deadline:
                        raise SessionConflictError("Native session is busy; retry after its active turn.") from exc
                    await asyncio.sleep(min(.05, max(0, deadline - asyncio.get_running_loop().time())))
        except BaseException:
            if "fd" in locals() and self._fd is None:
                os.close(fd)
            raise

    async def load(self) -> tuple[bytes | None, str | None]:
        self._safe_paths()
        try:
            return self.path.read_bytes(), None
        except FileNotFoundError:
            return None, None

    async def save(self, data: bytes, _etag: str | None) -> str | None:
        self._safe_paths()
        temp: str | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".state-",
                                             delete=False) as output:
                temp = output.name
                os.chmod(temp, 0o600)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, self.path)
            if sys.platform != "win32":
                dir_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            return None
        finally:
            if temp is not None and os.path.exists(temp):
                os.unlink(temp)

    async def check(self) -> None:
        if self._fd is None:
            raise LeaseLostError("Native session ownership was lost.")

    async def release(self) -> None:
        if self._fd is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    os.lseek(self._fd, 0, os.SEEK_SET)
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


class _BlobStore:
    def __init__(
        self, blob: BlobClient, owned_service: _OwnedBlobService | None = None
    ) -> None:
        self.blob = blob
        self._owned_service = owned_service
        self.lease: BlobLeaseClient | None = None
        self._renewal: asyncio.Task[None] | None = None
        self.failure: LeaseLostError | None = None
        self.failed = asyncio.Event()

    async def create_if_absent(self, data: bytes) -> None:
        try:
            await self.blob.upload_blob(data, overwrite=False)
        except ResourceExistsError:
            return
        except HttpResponseError as exc:
            # A leased blob rejects this unleased create as LeaseIdMissing, not BlobAlreadyExists.
            # azure-storage attaches error_code dynamically; azure-core does not declare it.
            if getattr(exc, "error_code", None) != StorageErrorCode.LEASE_ID_MISSING:
                raise

    async def acquire(self, deadline: float) -> None:
        while True:
            try:
                self.lease = await self.blob.acquire_lease(lease_duration=LEASE_SECONDS)
                self._renewal = asyncio.create_task(self._renew())
                return
            except HttpResponseError as exc:
                if exc.status_code not in {409, 412}:
                    raise
                if asyncio.get_running_loop().time() >= deadline:
                    raise SessionConflictError("Native session is busy; retry after its active turn.") from exc
                await asyncio.sleep(min(.2, max(0, deadline - asyncio.get_running_loop().time())))

    async def _renew(self) -> None:
        try:
            while True:
                await asyncio.sleep(LEASE_SECONDS / 3)
                assert self.lease is not None
                await self.lease.renew()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failure = LeaseLostError("Native session lease was lost; continuation is blocked.")
            self.failed.set()

    async def load(self) -> tuple[bytes | None, str | None]:
        try:
            downloader = await self.blob.download_blob(lease=self.lease)
            return await downloader.readall(), downloader.properties["etag"]
        except ResourceNotFoundError:
            return None, None

    async def save(self, data: bytes, etag: str | None) -> str | None:
        await self.check()
        if etag is None:
            raise LeaseLostError("Native session state disappeared while owned.")
        try:
            response = await self.blob.upload_blob(
                data, overwrite=True, lease=self.lease, etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
            await self.check()
            return str(response["etag"])
        except (ResourceModifiedError, ResourceNotFoundError, HttpResponseError):
            self.failure = LeaseLostError("Native session lease or state changed while owned.")
            self.failed.set()
            raise LeaseLostError("Native session lease or state changed while owned.") from None

    async def check(self) -> None:
        if self.failure is not None:
            raise self.failure
        if self.lease is None:
            raise LeaseLostError("Native session lease was lost.")

    async def release(self) -> None:
        try:
            if self._renewal is not None:
                self._renewal.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._renewal
                self._renewal = None
            if self.lease is not None and self.failure is None:
                with contextlib.suppress(Exception):
                    await self.lease.release()
            self.lease = None
        finally:
            owned_service, self._owned_service = self._owned_service, None
            if owned_service is not None:
                await owned_service.close()


type _Store = _LocalStore | _BlobStore


# Container creation is a once-per-worker-lifetime route concern; a create that failed for
# any other reason (including missing permission) is never remembered as ensured.
_ENSURED_CONTAINERS: set[tuple[str, str]] = set()
_ENSURED_CONTAINERS_LOCK = asyncio.Lock()


def clear_container_cache() -> None:
    """Drop remembered container ensures at worker shutdown."""
    _ENSURED_CONTAINERS.clear()


async def _ensure_container(route: StorageRoute, service: BlobServiceClient) -> None:
    key = (route.identity_key, route.container)
    if key in _ENSURED_CONTAINERS:
        return
    async with _ENSURED_CONTAINERS_LOCK:
        if key in _ENSURED_CONTAINERS:
            return
        container = service.get_container_client(route.container)
        with contextlib.suppress(ResourceExistsError):
            await container.create_container()
        _ENSURED_CONTAINERS.add(key)


async def _blob_client(
    route: StorageRoute, name: str
) -> tuple[BlobClient, _OwnedBlobService]:
    owned = await _open_owned_blob_service(route)
    try:
        await _ensure_container(route, owned.service)
        return (
            owned.service.get_blob_client(container=route.container, blob=name),
            owned,
        )
    except BaseException as error:
        try:
            await owned.close()
        except BaseException as close_error:
            raise BaseExceptionGroup(
                "Blob client setup and resource cleanup failed.",
                [error, close_error],
            ) from None
        raise


class NativeSession:
    """One owner, one envelope and one serialized callback stream."""

    def __init__(self, route: StorageRoute, slug: str, session_id: str, native_id: str,
                 store: _Store, envelope: StateEnvelope, etag: str | None) -> None:
        self.route, self.slug, self.session_id = route, slug, session_id
        self.native_id, self.store, self.envelope, self.etag = native_id, store, envelope, etag
        self.lock = asyncio.Lock()
        self.failure: NativeSessionError | None = None
        self.failed = asyncio.Event()

    @classmethod
    async def open(cls, harness: AppHarness, slug: str, session_id: str,
                   native_id: str, deadline: float) -> NativeSession:
        route = harness.session_storage
        if route is None:
            raise PersistenceUnavailableError("Native session storage is not configured.")
        name = state_name(route, slug, session_id)
        try:
            if route.mode is StorageMode.LOCAL:
                store: _Store = _LocalStore(route.local_dir / name)
                await store.acquire(deadline)
            else:
                blob, owned_service = await _blob_client(route, name)
                store = _BlobStore(blob, owned_service)
            try:
                if isinstance(store, _BlobStore):
                    await store.create_if_absent(
                        serialize(_new_envelope(route, slug, session_id, native_id))
                    )
                    await store.acquire(deadline)
                data, etag = await store.load()
                envelope = (
                    deserialize(data, route, slug, session_id, native_id)
                    if data is not None else _new_envelope(route, slug, session_id, native_id)
                )
                owner = cls(route, slug, session_id, native_id, store, envelope, etag)
                await owner.transition(owner_epoch=envelope.owner_epoch + 1)
                return owner
            except BaseException:
                await store.release()
                raise
        except (NativeSessionError, asyncio.CancelledError):
            raise
        except Exception:
            raise PersistenceUnavailableError("Native session storage is unavailable.") from None

    async def check(self) -> None:
        if self.failure is not None:
            raise self.failure
        await self.store.check()

    def latch(self, error: BaseException) -> None:
        if self.failure is None:
            self.failure = error if isinstance(error, NativeSessionError) else PersistenceUnavailableError(
                "Native SessionFs callback failed; the turn cannot continue."
            )
            if isinstance(self.store, _BlobStore):
                self.store.failure = LeaseLostError("Native session ownership is no longer assured.")
                self.store.failed.set()
            self.failed.set()

    async def _save(self, candidate: StateEnvelope) -> None:
        await self.check()
        candidate.revision = self.envelope.revision + 1
        try:
            data = serialize(candidate)
            self.etag = await self.store.save(data, self.etag)
        except BaseException as exc:
            self.latch(exc)
            raise
        self.envelope = candidate

    async def transition(self, **fields: object) -> None:
        async with self.lock:
            await self.check()
            candidate = self.envelope.model_copy(deep=True, update=fields)
            candidate = StateEnvelope.model_validate(candidate.model_dump())
            await self._save(candidate)

    async def prepare(self, *, new_session: bool, workspace_path: str = "") -> None:
        """Record the creating worker's workspace once; a resume keeps the persisted one."""
        state = self.envelope.state
        if state is SessionState.EMPTY:
            if not new_session:
                raise IncompatibleSessionError("Native session has no completed turn to resume.")
        elif state is SessionState.READY:
            if new_session:
                raise IncompatibleSessionError("Native session already exists; refusing to reset it.")
        else:
            raise IncompatibleSessionError("Native session is not safely resumable; use a new ID.")
        baseline = self.envelope.completed.model_copy(deep=True) if self.envelope.completed else empty_tree()
        recorded = self.envelope.workspace_path or workspace_path
        await self.transition(state=SessionState.PREPARING, working=baseline,
                              workspace_path=recorded, handoff_may_have_started=False)

    async def rollback(self) -> None:
        await self.check()
        completed = self.envelope.completed
        await self.transition(state=SessionState.READY if completed else SessionState.EMPTY,
                              working=None, handoff_may_have_started=False)

    async def recover_preparing(self) -> None:
        """Discard an unhanded-off tree under the newly acquired owner fence."""
        if (self.envelope.state not in {SessionState.PREPARING, SessionState.ACTIVE}
                or self.envelope.handoff_may_have_started):
            raise IncompatibleSessionError("Native session is not safely resumable; use a new ID.")
        await self.rollback()

    async def complete(self) -> None:
        async with self.lock:
            await self.check()
            if self.envelope.state is not SessionState.ACTIVE or self.envelope.working is None:
                raise IncompatibleSessionError("Native turn cannot be completed from this state.")
            completed = self.envelope.working.model_copy(deep=True)
            await self._save(self.envelope.model_copy(
                deep=True, update={"state": SessionState.READY, "working": None,
                                   "completed": completed, "handoff_may_have_started": False},
            ))
            await self.check()

    async def uncertain(self) -> None:
        if self.failure is None:
            await self.transition(state=SessionState.UNCERTAIN)

    async def delete(self) -> None:
        if self.envelope.state is not SessionState.DELETED:
            await self.transition(state=SessionState.DELETED, working=None, completed=None,
                                  handoff_may_have_started=False)

    async def close(self) -> None:
        await self.store.release()


class _PathResultError(OSError):
    """An expected POSIX path result for the SDK, never a storage or ownership failure."""

    def __init__(self, code: int, path: str) -> None:
        super().__init__(code, os.strerror(code), path)


class NativeSessionFs(SessionFsProvider):
    """SDK filesystem callbacks, scoped to the owned working tree."""

    def __init__(
        self,
        owner: NativeSession,
        conventions: PathConventions = "posix",
        workspace_path: str = "",
    ) -> None:
        self.owner = owner
        self.conventions = conventions
        aliases = [owner.envelope.workspace_path, workspace_path]
        self.workspace_aliases = tuple(
            dict.fromkeys(alias for alias in aliases if alias)
        )

    def _fail(self, error: BaseException) -> None:
        if not isinstance(error, _PathResultError):
            self.owner.latch(error)

    def _path(self, path: str) -> str:
        try:
            return normalize_callback_path(path, self.conventions, self.workspace_aliases)
        except BaseException as exc:
            self._fail(exc)
            raise

    async def _read(self, operation: Callable[[WorkingTree], object]) -> object:
        try:
            async with self.owner.lock:
                await self.owner.check()
                tree = self.owner.envelope.working
                if tree is None:
                    raise IncompatibleSessionError("Native session has no working tree.")
                return operation(tree)
        except BaseException as exc:
            self._fail(exc)
            raise

    async def _mutate(self, operation: Callable[[WorkingTree], None]) -> None:
        try:
            async with self.owner.lock:
                await self.owner.check()
                candidate = self.owner.envelope.model_copy(deep=True)
                if candidate.working is None:
                    raise IncompatibleSessionError("Native session has no working tree.")
                operation(candidate.working)
                candidate = StateEnvelope.model_validate(candidate.model_dump())
                await self.owner._save(candidate)
        except BaseException as exc:
            self._fail(exc)
            raise

    async def read_file(self, path: str) -> str:
        name = self._path(path)

        def read(tree: WorkingTree) -> str:
            if name not in tree.files:
                raise _PathResultError(errno.ENOENT, name)
            return tree.files[name].content

        return str(await self._read(read))

    async def write_file(self, path: str, content: str, mode: int | None = None) -> None:
        del mode
        try:
            name = self._path(path)
            if name == "/" or not isinstance(content, str):
                raise ValueError("Invalid native file.")

            def write(tree: WorkingTree) -> None:
                if name in tree.directories:
                    raise _PathResultError(errno.EISDIR, name)
                parent = name.rpartition("/")[0] or "/"
                self._parents(tree, parent)
                now = time.time()
                previous = tree.files.get(name)
                tree.files[name] = FileContent(
                    content=content, size_bytes=len(content.encode("utf-8")),
                    birthtime=previous.birthtime if previous else now, mtime=now,
                )

            await self._mutate(write)
        except BaseException as exc:
            self._fail(exc)
            raise

    async def append_file(self, path: str, content: str, mode: int | None = None) -> None:
        del mode
        try:
            name = self._path(path)
            if name == "/" or not isinstance(content, str):
                raise ValueError("Invalid native file.")

            def append(tree: WorkingTree) -> None:
                if name in tree.directories:
                    raise _PathResultError(errno.EISDIR, name)
                self._parents(tree, name.rpartition("/")[0] or "/")
                now = time.time()
                previous = tree.files.get(name)
                value = (previous.content if previous else "") + content
                tree.files[name] = FileContent(
                    content=value, size_bytes=len(value.encode("utf-8")),
                    birthtime=previous.birthtime if previous else now, mtime=now,
                )

            await self._mutate(append)
        except BaseException as exc:
            self._fail(exc)
            raise

    @staticmethod
    def _parents(tree: WorkingTree, name: str) -> None:
        path = ""
        for part in name.strip("/").split("/") if name != "/" else []:
            path += "/" + part
            if path in tree.files:
                raise _PathResultError(errno.ENOTDIR, path)
            if path not in tree.directories:
                now = time.time()
                tree.directories[path] = FileMetadata(birthtime=now, mtime=now)

    async def exists(self, path: str) -> bool:
        try:
            name = self._path(path)
            return bool(await self._read(
                lambda tree: name in tree.files or name in tree.directories
            ))
        except BaseException as exc:
            self._fail(exc)
            raise

    async def stat(self, path: str) -> SessionFsFileInfo:
        try:
            name = self._path(path)

            def lookup(tree: WorkingTree) -> SessionFsFileInfo:
                file = tree.files.get(name)
                item: FileMetadata | None = file or tree.directories.get(name)
                if item is None:
                    raise _PathResultError(errno.ENOENT, name)
                return SessionFsFileInfo(
                    is_file=file is not None, is_directory=file is None,
                    size=file.size_bytes if file else 0,
                    mtime=datetime.fromtimestamp(item.mtime, UTC),
                    birthtime=datetime.fromtimestamp(item.birthtime, UTC),
                )

            result = await self._read(lookup)
            assert isinstance(result, SessionFsFileInfo)
            return result
        except BaseException as exc:
            self._fail(exc)
            raise

    async def mkdir(self, path: str, recursive: bool, mode: int | None = None) -> None:
        del mode
        try:
            name = self._path(path)

            def create(tree: WorkingTree) -> None:
                if name in tree.files:
                    raise _PathResultError(errno.EEXIST, name)
                parent = name.rpartition("/")[0] or "/"
                if parent not in tree.directories and not recursive:
                    raise _PathResultError(errno.ENOENT, parent)
                self._parents(tree, name)

            await self._mutate(create)
        except BaseException as exc:
            self._fail(exc)
            raise

    async def readdir(self, path: str) -> list[str]:
        try:
            name = self._path(path)

            def listing(tree: WorkingTree) -> list[str]:
                if name not in tree.directories:
                    raise _PathResultError(errno.ENOTDIR if name in tree.files else errno.ENOENT, name)
                return sorted(p.rpartition("/")[2] for p in (*tree.directories, *tree.files)
                              if p != name and (p.rpartition("/")[0] or "/") == name)

            result = await self._read(listing)
            assert isinstance(result, list)
            return result
        except BaseException as exc:
            self._fail(exc)
            raise

    async def readdir_with_types(self, path: str) -> Sequence[SessionFSReaddirWithTypesEntry]:
        try:
            name = self._path(path)

            def listing(tree: WorkingTree) -> list[SessionFSReaddirWithTypesEntry]:
                if name not in tree.directories:
                    raise _PathResultError(errno.ENOTDIR if name in tree.files else errno.ENOENT, name)
                return [
                    SessionFSReaddirWithTypesEntry(
                        name=entry,
                        type=(DebugCollectLogsEntryKind.FILE if
                              (name.rstrip("/") + "/" + entry) in tree.files else
                              DebugCollectLogsEntryKind.DIRECTORY),
                    ) for entry in sorted(p.rpartition("/")[2]
                           for p in (*tree.directories, *tree.files)
                           if p != name and (p.rpartition("/")[0] or "/") == name)
                ]

            result = await self._read(listing)
            assert isinstance(result, list)
            return result
        except BaseException as exc:
            self._fail(exc)
            raise

    async def rm(self, path: str, recursive: bool, force: bool) -> None:
        try:
            name = self._path(path)
            if name == "/":
                raise ValueError("Cannot remove native filesystem root.")

            def remove(tree: WorkingTree) -> None:
                if name in tree.files:
                    del tree.files[name]
                    return
                if name not in tree.directories:
                    if force:
                        return
                    raise _PathResultError(errno.ENOENT, name)
                nested = [p for p in (*tree.directories, *tree.files) if p.startswith(name + "/")]
                if nested and not recursive:
                    raise _PathResultError(errno.ENOTEMPTY, name)
                for path in nested:
                    tree.files.pop(path, None)
                    tree.directories.pop(path, None)
                del tree.directories[name]

            await self._mutate(remove)
        except BaseException as exc:
            self._fail(exc)
            raise

    async def rename(self, src: str, dest: str) -> None:
        try:
            source, target = self._path(src), self._path(dest)
            if source == "/" or target == "/" or target.startswith(source + "/"):
                raise ValueError("Invalid native rename.")
            def move(tree: WorkingTree) -> None:
                if source not in tree.files and source not in tree.directories:
                    raise _PathResultError(errno.ENOENT, source)
                if source == target:
                    return
                parent = target.rpartition("/")[0] or "/"
                if parent not in tree.directories:
                    raise _PathResultError(errno.ENOENT, parent)
                if target in tree.directories:
                    raise _PathResultError(errno.EEXIST, target)
                tree.files.pop(target, None)
                for key in [p for p in tree.directories if p == source or p.startswith(source + "/")]:
                    tree.directories[target + key[len(source):]] = tree.directories.pop(key)
                for key in [p for p in tree.files if p == source or p.startswith(source + "/")]:
                    tree.files[target + key[len(source):]] = tree.files.pop(key)

            await self._mutate(move)
        except BaseException as exc:
            self._fail(exc)
            raise
