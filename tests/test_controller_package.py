"""Deterministic capture, digest-gated delivery, and live-manifest capture tests."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import os
import re
import stat
import tempfile
import threading
import time
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from azure_functions_agents.controller import package
from azure_functions_agents.session_state import (
    AppIdentity,
    DurableSessionRecord,
    FunctionAppOwnerContext,
    OwnerPartition,
    owner_partition,
)
from azure_functions_agents.transport.manifest import (
    SESSION_MANIFEST_PATH,
    ExpectedSandboxManifestBinding,
    SandboxManifestMismatchError,
    render_sandbox_manifest_binding,
)
from azure_functions_agents.transport.transport_models import (
    ProvisionedSandboxIdentity,
    SandboxFileOperationError,
    SandboxFileStat,
)
from tests.doubles.fake_sandbox_transport import FakeSandboxTransport

_NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
_GROUP = (
    "/subscriptions/sub-123/resourceGroups/rg-agent/"
    "providers/Microsoft.App/sandboxGroups/session-group"
)
_STATE_STORE_FINGERPRINT = "s1-" + ("f" * 52)

# A byte-exact golden vector: same tree, same digest on both supported
# interpreters (verified directly against this exact fixture). Regenerate
# only if the deterministic ZIP contract changes; avoid an executable file
# here, since POSIX execute bits are not reproducible through this fixture
# on Windows.
_GOLDEN_DIGEST = "sha256:802a8d501bf9701723ca2b0a566000d1ba7076cacf6694f5e5ddce2759b1659f"
_GOLDEN_ARCHIVE_HEX = (
    "504b030414000000000000002100f187b58e0e0000000e000000070000002e6869646465"
    "6e68696464656e2d636f6e74656e74504b03041400000000000000210000000000000000"
    "00000000000a000000656d7074795f6469722f504b030414000000000000002100abdcec"
    "1a1900000019000000070000006d61696e2e7079696d706f7274206f730a7072696e7428"
    "2768656c6c6f27290a504b03041400000000000000210000000000000000000000000011"
    "0000007375622f6e65737465645f656d7074792f504b03041400000000000000210030bf"
    "1c021c0000001c0000000b0000007375622f7574696c2e70796465662068656c70657228"
    "293a0a2020202072657475726e2034320a504b0102140314000000000000002100f187b5"
    "8e0e0000000e000000070000000000000000000000a481000000002e68696464656e504b"
    "01021403140000000000000021000000000000000000000000000a000000000000000000"
    "1000ed4133000000656d7074795f6469722f504b0102140314000000000000002100abdc"
    "ec1a1900000019000000070000000000000000000000a4815b0000006d61696e2e707950"
    "4b0102140314000000000000002100000000000000000000000000110000000000000000"
    "001000ed41990000007375622f6e65737465645f656d7074792f504b0102140314000000"
    "00000000210030bf1c021c0000001c0000000b0000000000000000000000a481c8000000"
    "7375622f7574696c2e7079504b050600000000050005001a0100000d0100000000"
)


def _partition() -> OwnerPartition:
    app = AppIdentity.create(
        subscription_id="11111111-2222-3333-4444-555555555555",
        site_name="agent-app",
    )
    return owner_partition(FunctionAppOwnerContext.create(app, "main"))


def _session_record(
    *,
    sandbox_id: str | None = "sandbox-123",
    generation: int = 1,
    digest_kind: str = package.FUNCS_ZIP_DIGEST_KIND,
    digest: str = "sha256:" + ("a" * 64),
    protocol: str = "maf-session-v1",
) -> DurableSessionRecord:
    return DurableSessionRecord.create(
        owner_partition=_partition(),
        session_id="session-123",
        sandbox_id=sandbox_id,
        generation=generation,
        digest_kind=digest_kind,
        digest=digest,
        protocol=protocol,
        status="ready",
        last_activity_at=_NOW,
        expires_at=_NOW + timedelta(hours=24),
        idle_policy_armed=True,
        active_run_id=None,
        snapshot_ids=(),
        region="westus2",
        state_store_fingerprint=_STATE_STORE_FINGERPRINT,
        quarantine_reason=None,
        tombstone_reason=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _captured_package(archive_bytes: bytes = b"stub-archive-bytes") -> package.CapturedContentPackage:
    digest = f"sha256:{hashlib.sha256(archive_bytes).hexdigest()}"
    return package.CapturedContentPackage.create(
        archive_bytes=archive_bytes, digest_kind=package.FUNCS_ZIP_DIGEST_KIND, digest=digest
    )


def _expected_binding(
    captured: package.CapturedContentPackage,
    *,
    sandbox_id: str | None = "sandbox-123",
    generation: int = 1,
) -> ExpectedSandboxManifestBinding:
    session = _session_record(
        sandbox_id=sandbox_id,
        generation=generation,
        digest_kind=captured.digest_kind,
        digest=captured.digest,
    )
    return package.build_expected_manifest_binding(
        session,
        sandbox_group_resource_id=_GROUP,
        state_store_fingerprint=_STATE_STORE_FINGERPRINT,
    )


def _live_identity(
    *, sandbox_id: str = "sandbox-123", group_resource_id: str = _GROUP
) -> ProvisionedSandboxIdentity:
    return ProvisionedSandboxIdentity.create(
        sandbox_id=sandbox_id, group_resource_id=group_resource_id, region="westus2"
    )


def _write_tree(root: Path, files: dict[str, bytes]) -> None:
    for relative_path, content in files.items():
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _synthetic_entry(*, name: str = "f", size: int = 0, is_directory: bool = False) -> package._ScriptRootEntry:
    return package._ScriptRootEntry(
        archive_name=name,
        is_directory=is_directory,
        size=size,
        mtime_ns=0,
        ctime_ns=0,
        device=0,
        inode=0,
        executable=False,
        locator=None,
    )


def _make_symlink(link_path: Path, target: str, *, target_is_directory: bool = False) -> None:
    try:
        os.symlink(target, link_path, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Platform cannot create symlinks in this environment: {exc}")


def _force_unsupported_capture_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a platform without secure traversal support, regardless of the host."""

    monkeypatch.setattr(package, "_posix_secure_traversal_available", lambda: False)


@contextlib.contextmanager
def tempfile_zip_archive() -> Iterator[zipfile.ZipFile]:
    """A deterministic-settings ZipFile over a temp file, for direct writer-helper tests."""

    with tempfile.TemporaryFile() as temp_archive, zipfile.ZipFile(
        temp_archive, mode="w", compression=zipfile.ZIP_STORED, allowZip64=False
    ) as archive:
        yield archive


@contextlib.contextmanager
def _posix_root_fd(root: Path) -> Iterator[int]:
    """Open (and always close) one root fd for a direct POSIX writer-helper test."""

    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield root_fd
    finally:
        os.close(root_fd)


_posix_secure_traversal_unavailable = not package._posix_secure_traversal_available()
_skip_unless_posix_secure_traversal = pytest.mark.skipif(
    _posix_secure_traversal_unavailable,
    reason="dir_fd/O_NOFOLLOW secure traversal is not available on this platform",
)


# ---------------------------------------------------------------------------
# _capture_script_root: determinism, safety, and mutation detection
# ---------------------------------------------------------------------------


@_skip_unless_posix_secure_traversal
def test_capture_is_invariant_to_enumeration_order_and_mtime(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    _write_tree(first_root, {"a.py": b"a", "b/c.py": b"c", "d.py": b"d"})
    _write_tree(second_root, {"d.py": b"d", "b/c.py": b"c", "a.py": b"a"})
    os.utime(second_root / "a.py", (1_000_000, 1_000_000))

    first = package._capture_script_root(first_root)
    second = package._capture_script_root(second_root)

    assert first.digest == second.digest
    assert first.archive_bytes == second.archive_bytes


@_skip_unless_posix_secure_traversal
def test_capture_digest_changes_when_one_byte_changes(tmp_path: Path) -> None:
    root = tmp_path / "app"
    _write_tree(root, {"main.py": b"print(1)\n"})
    before = package._capture_script_root(root)

    (root / "main.py").write_bytes(b"print(2)\n")
    after = package._capture_script_root(root)

    assert before.digest != after.digest


@_skip_unless_posix_secure_traversal
def test_capture_digest_changes_when_a_path_changes(tmp_path: Path) -> None:
    root = tmp_path / "app"
    _write_tree(root, {"main.py": b"print(1)\n"})
    before = package._capture_script_root(root)

    (root / "main.py").rename(root / "renamed.py")
    after = package._capture_script_root(root)

    assert before.digest != after.digest


@_skip_unless_posix_secure_traversal
def test_capture_digest_changes_when_an_empty_directory_is_added(tmp_path: Path) -> None:
    root = tmp_path / "app"
    _write_tree(root, {"main.py": b"print(1)\n"})
    before = package._capture_script_root(root)

    (root / "empty").mkdir()
    after = package._capture_script_root(root)

    assert before.digest != after.digest


@_skip_unless_posix_secure_traversal
def test_capture_fails_closed_for_a_completely_empty_script_root(tmp_path: Path) -> None:
    root = tmp_path / "app"
    root.mkdir()

    with pytest.raises(package.ScriptRootUnavailableError):
        package._capture_script_root(root)


def test_archive_metadata_differs_between_executable_and_standard_files() -> None:
    executable_entry = _synthetic_entry(name="run.sh")
    executable_entry = replace(executable_entry, executable=True)
    standard_entry = replace(executable_entry, executable=False)

    executable_info = package._archive_member_info(executable_entry)
    standard_info = package._archive_member_info(standard_entry)

    assert executable_info.external_attr != standard_info.external_attr
    assert (executable_info.external_attr >> 16) & 0o777 == 0o755
    assert (standard_info.external_attr >> 16) & 0o777 == 0o644


@_skip_unless_posix_secure_traversal
def test_capture_digest_changes_when_the_execute_bit_changes_on_posix(tmp_path: Path) -> None:
    root = tmp_path / "app"
    _write_tree(root, {"run.sh": b"#!/bin/sh\necho hi\n"})
    (root / "run.sh").chmod(0o644)
    before = package._capture_script_root(root)

    (root / "run.sh").chmod(0o755)
    after = package._capture_script_root(root)

    assert before.digest != after.digest


@_skip_unless_posix_secure_traversal
def test_capture_includes_python_packages_hidden_and_nested_files(tmp_path: Path) -> None:
    root = tmp_path / "app"
    _write_tree(
        root,
        {
            "main.py": b"print('hi')\n",
            ".env": b"SECRET=1\n",
            ".python_packages/lib/site-packages/pkg/__init__.py": b"",
            "nested/deep/path/module.py": b"x = 1\n",
        },
    )

    captured = package._capture_script_root(root)

    with zipfile.ZipFile(io.BytesIO(captured.archive_bytes)) as archive:
        names = set(archive.namelist())
        assert ".env" in names
        assert ".python_packages/lib/site-packages/pkg/__init__.py" in names
        assert "nested/deep/path/module.py" in names
        assert archive.read("nested/deep/path/module.py") == b"x = 1\n"


@_skip_unless_posix_secure_traversal
def test_capture_produces_a_byte_exact_golden_archive_across_interpreters(tmp_path: Path) -> None:
    root = tmp_path / "app"
    _write_tree(
        root,
        {
            ".hidden": b"hidden-content",
            "main.py": b"import os\nprint('hello')\n",
            "sub/util.py": b"def helper():\n    return 42\n",
        },
    )
    (root / "empty_dir").mkdir()
    (root / "sub" / "nested_empty").mkdir()

    captured = package._capture_script_root(root)

    assert captured.digest == _GOLDEN_DIGEST
    assert captured.archive_bytes == bytes.fromhex(_GOLDEN_ARCHIVE_HEX)


@_skip_unless_posix_secure_traversal
def test_captured_package_digest_kind_and_shape_are_fixed(tmp_path: Path) -> None:
    root = tmp_path / "app"
    _write_tree(root, {"main.py": b"print(1)\n"})

    captured = package._capture_script_root(root)

    assert captured.digest_kind == "funcs_zip"
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", captured.digest)


def test_captured_content_package_rejects_a_malformed_digest_kind() -> None:
    with pytest.raises(package.ContentPackagingError):
        package.CapturedContentPackage.create(
            archive_bytes=b"stub", digest_kind="not_funcs_zip", digest="sha256:" + "a" * 64
        )


def test_captured_content_package_rejects_a_malformed_digest_shape() -> None:
    with pytest.raises(package.ContentPackagingError):
        package.CapturedContentPackage.create(
            archive_bytes=b"stub", digest_kind="funcs_zip", digest="SHA256:" + "A" * 64
        )


def test_captured_content_package_accepts_bytes_whose_digest_genuinely_matches() -> None:
    archive_bytes = b"a-genuine-deterministic-archive"
    digest = f"sha256:{hashlib.sha256(archive_bytes).hexdigest()}"

    package_ = package.CapturedContentPackage.create(
        archive_bytes=archive_bytes, digest_kind=package.FUNCS_ZIP_DIGEST_KIND, digest=digest
    )

    assert package_.digest == digest
    assert package_.archive_bytes == archive_bytes


def test_captured_content_package_rejects_a_digest_that_does_not_match_the_bytes() -> None:
    """Manual construction cannot claim a digest that disagrees with the actual archive bytes."""
    archive_bytes = b"real-bytes"
    wrong_digest = f"sha256:{hashlib.sha256(b'different-bytes').hexdigest()}"

    with pytest.raises(package.ContentPackagingError):
        package.CapturedContentPackage.create(
            archive_bytes=archive_bytes,
            digest_kind=package.FUNCS_ZIP_DIGEST_KIND,
            digest=wrong_digest,
        )


def test_captured_content_package_accepts_bytes_at_exactly_the_operational_bound() -> None:
    archive_bytes = b"x" * package._MAX_ARCHIVE_OPERATIONAL_SIZE
    digest = f"sha256:{hashlib.sha256(archive_bytes).hexdigest()}"

    package_ = package.CapturedContentPackage.create(
        archive_bytes=archive_bytes, digest_kind=package.FUNCS_ZIP_DIGEST_KIND, digest=digest
    )

    assert package_.size == package._MAX_ARCHIVE_OPERATIONAL_SIZE


def test_captured_content_package_rejects_bytes_over_the_operational_bound() -> None:
    """Manual construction cannot bypass the 256 MiB v1 operational cap either."""
    archive_bytes = b"x" * (package._MAX_ARCHIVE_OPERATIONAL_SIZE + 1)
    digest = f"sha256:{hashlib.sha256(archive_bytes).hexdigest()}"

    with pytest.raises(package.ContentArchiveTooLargeError):
        package.CapturedContentPackage.create(
            archive_bytes=archive_bytes, digest_kind=package.FUNCS_ZIP_DIGEST_KIND, digest=digest
        )


# ---------------------------------------------------------------------------
# get_content_package: once-per-worker cache and async API
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_content_package_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test its own cache so captures never leak across test cases."""

    monkeypatch.setattr(package, "_content_package_cache", package._ContentPackageCache())


@_skip_unless_posix_secure_traversal
@pytest.mark.asyncio
async def test_get_content_package_returns_the_same_object_to_every_caller(tmp_path: Path) -> None:
    root = tmp_path / "app"
    _write_tree(root, {"main.py": b"print(1)\n"})

    first = await package.get_content_package(root)
    second = await package.get_content_package(root)

    assert second is first


@_skip_unless_posix_secure_traversal
@pytest.mark.asyncio
async def test_get_content_package_invokes_capture_only_once_for_concurrent_callers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "app"
    _write_tree(root, {"main.py": b"print(1)\n"})
    real_capture = package._capture_script_root
    call_count = 0

    def _counting_capture(script_root: Path) -> package.CapturedContentPackage:
        nonlocal call_count
        call_count += 1
        time.sleep(0.05)
        return real_capture(script_root)

    monkeypatch.setattr(package, "_capture_script_root", _counting_capture)

    results = await asyncio.gather(*(package.get_content_package(root) for _ in range(8)))

    assert call_count == 1
    assert all(result is results[0] for result in results)


@_skip_unless_posix_secure_traversal
@pytest.mark.asyncio
async def test_get_content_package_does_not_recapture_after_the_root_changes(
    tmp_path: Path,
) -> None:
    """The mounted script root is treated as immutable for the worker's lifetime."""
    root = tmp_path / "app"
    _write_tree(root, {"main.py": b"print(1)\n"})

    before = await package.get_content_package(root)
    (root / "main.py").write_bytes(b"print(2)\n")
    after = await package.get_content_package(root)

    assert after is before


@_skip_unless_posix_secure_traversal
@pytest.mark.asyncio
async def test_get_content_package_does_not_cache_a_failed_capture(tmp_path: Path) -> None:
    root = tmp_path / "app"
    root.mkdir()

    with pytest.raises(package.ScriptRootUnavailableError):
        await package.get_content_package(root)

    _write_tree(root, {"main.py": b"print(1)\n"})
    captured = await package.get_content_package(root)

    assert captured.digest_kind == "funcs_zip"


@_skip_unless_posix_secure_traversal
@pytest.mark.asyncio
async def test_get_content_package_uses_independent_entries_per_root(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    _write_tree(first_root, {"main.py": b"print('first')\n"})
    _write_tree(second_root, {"main.py": b"print('second')\n"})

    first = await package.get_content_package(first_root)
    second = await package.get_content_package(second_root)

    assert first.digest != second.digest


@_skip_unless_posix_secure_traversal
@pytest.mark.asyncio
async def test_get_content_package_offloads_capture_off_the_event_loop_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "app"
    _write_tree(root, {"main.py": b"print(1)\n"})
    event_loop_thread = threading.current_thread()
    real_capture = package._capture_script_root
    capture_thread: threading.Thread | None = None

    def _recording_capture(script_root: Path) -> package.CapturedContentPackage:
        nonlocal capture_thread
        capture_thread = threading.current_thread()
        return real_capture(script_root)

    monkeypatch.setattr(package, "_capture_script_root", _recording_capture)

    await package.get_content_package(root)

    assert capture_thread is not None
    assert capture_thread is not event_loop_thread


@_skip_unless_posix_secure_traversal
def test_get_content_package_is_single_flight_across_independent_event_loops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two OS threads, each running its own event loop, must still single-flight.

    A process-global ``asyncio.Lock`` would be unsafe here (bound to whichever
    loop first awaits it); this is exactly why the cache's internal locking
    uses only ``threading.Lock``, never an event-loop-bound primitive.
    """
    root = tmp_path / "app"
    _write_tree(root, {"main.py": b"print(1)\n"})
    real_capture = package._capture_script_root
    call_count = 0
    count_lock = threading.Lock()

    def _counting_capture(script_root: Path) -> package.CapturedContentPackage:
        nonlocal call_count
        with count_lock:
            call_count += 1
        time.sleep(0.05)
        return real_capture(script_root)

    monkeypatch.setattr(package, "_capture_script_root", _counting_capture)

    results: list[package.CapturedContentPackage] = []
    errors: list[BaseException] = []
    results_lock = threading.Lock()

    def _run_in_a_fresh_event_loop() -> None:
        try:
            result = asyncio.run(package.get_content_package(root))
        except BaseException as exc:
            with results_lock:
                errors.append(exc)
            return
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=_run_in_a_fresh_event_loop) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)

    assert not any(thread.is_alive() for thread in threads), "a thread deadlocked or hung"
    assert not errors
    assert call_count == 1
    assert len(results) == 2
    assert results[0] is results[1]


@pytest.mark.asyncio
async def test_get_content_package_fails_before_any_filesystem_access_on_an_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_unsupported_capture_platform(monkeypatch)

    def _fail_if_called(_script_root: Path) -> Path:
        raise AssertionError("path resolution must not run once the platform check fails")

    monkeypatch.setattr(package, "_resolve_script_root_path", _fail_if_called)

    with pytest.raises(package.UnsupportedCapturePlatformError):
        await package.get_content_package(Path("/does/not/matter"))


# ---------------------------------------------------------------------------
# Safe symlink handling
# ---------------------------------------------------------------------------


@_skip_unless_posix_secure_traversal
def test_capture_dereferences_a_contained_regular_file_symlink_deterministically(
    tmp_path: Path,
) -> None:
    root = tmp_path / "app"
    _write_tree(root, {"real.py": b"print('real')\n"})
    _make_symlink(root / "link.py", "real.py")

    first = package._capture_script_root(root)
    second = package._capture_script_root(root)

    assert first.digest == second.digest
    with zipfile.ZipFile(io.BytesIO(first.archive_bytes)) as archive:
        assert archive.read("link.py") == b"print('real')\n"
        assert archive.read("real.py") == b"print('real')\n"


@_skip_unless_posix_secure_traversal
def test_capture_dereferences_a_multi_hop_relative_symlink_chain(tmp_path: Path) -> None:
    root = tmp_path / "app"
    _write_tree(root, {"c.txt": b"final-content"})
    _make_symlink(root / "b.txt", "c.txt")
    _make_symlink(root / "a.txt", "b.txt")

    captured = package._capture_script_root(root)

    with zipfile.ZipFile(io.BytesIO(captured.archive_bytes)) as archive:
        assert archive.read("a.txt") == b"final-content"


@_skip_unless_posix_secure_traversal
def test_capture_rejects_a_symlink_with_an_absolute_target_escaping_the_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "app"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside\n")
    _make_symlink(root / "escape.py", str(outside))

    with pytest.raises(package.UnsafeScriptRootEntryError):
        package._capture_script_root(root)


@_skip_unless_posix_secure_traversal
def test_capture_rejects_an_absolute_symlink_target_even_when_it_would_resolve_inside_the_root(
    tmp_path: Path,
) -> None:
    """An absolute target is rejected outright, independent of where it points.

    A symlink target that is spelled as an absolute path is never treated as
    root-relative, even when that absolute path happens to name a file that
    is genuinely inside the script root -- reinterpreting it would blur the
    line between "escaping" and "contained" in a way an attacker could abuse.
    """
    root = tmp_path / "app"
    _write_tree(root, {"real.txt": b"real-content"})
    _make_symlink(root / "abslink.txt", str(root / "real.txt"))

    with pytest.raises(package.UnsafeScriptRootEntryError):
        package._capture_script_root(root)


@_skip_unless_posix_secure_traversal
def test_capture_rejects_a_symlink_with_a_relative_target_escaping_the_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "app"
    root.mkdir()
    (tmp_path / "outside.txt").write_bytes(b"outside\n")
    _make_symlink(root / "escape.py", "../outside.txt")

    with pytest.raises(package.UnsafeScriptRootEntryError):
        package._capture_script_root(root)


@_skip_unless_posix_secure_traversal
def test_capture_rejects_a_broken_symlink(tmp_path: Path) -> None:
    root = tmp_path / "app"
    root.mkdir()
    _make_symlink(root / "broken.py", "does-not-exist.py")

    with pytest.raises(package.UnsafeScriptRootEntryError):
        package._capture_script_root(root)


@_skip_unless_posix_secure_traversal
def test_capture_rejects_a_directory_symlink(tmp_path: Path) -> None:
    root = tmp_path / "app"
    (root / "realdir").mkdir(parents=True)
    _make_symlink(root / "dirlink", "realdir", target_is_directory=True)

    with pytest.raises(package.UnsafeScriptRootEntryError):
        package._capture_script_root(root)


@_skip_unless_posix_secure_traversal
def test_capture_rejects_a_cyclic_symlink(tmp_path: Path) -> None:
    root = tmp_path / "app"
    root.mkdir()
    _make_symlink(root / "a.link", "b.link")
    _make_symlink(root / "b.link", "a.link")

    with pytest.raises(package.UnsafeScriptRootEntryError):
        package._capture_script_root(root)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
def test_capture_rejects_a_symlink_targeting_a_fifo(tmp_path: Path) -> None:
    root = tmp_path / "app"
    root.mkdir()
    os.mkfifo(root / "pipe")  # type: ignore[attr-defined]
    _make_symlink(root / "link", "pipe")

    with pytest.raises(package.UnsafeScriptRootEntryError):
        package._capture_script_root(root)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
def test_capture_rejects_a_special_file_fifo(tmp_path: Path) -> None:
    root = tmp_path / "app"
    root.mkdir()
    os.mkfifo(root / "pipe")  # type: ignore[attr-defined]

    with pytest.raises(package.UnsafeScriptRootEntryError):
        package._capture_script_root(root)


@_skip_unless_posix_secure_traversal
def test_capture_handles_a_python_packages_tree_with_a_contained_shared_library_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "app"
    package_dir = root / ".python_packages" / "lib" / "site-packages" / "native_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "libfoo.so.1.2.3").write_bytes(b"\x7fELF-fake-shared-object")
    _make_symlink(package_dir / "libfoo.so", "libfoo.so.1.2.3")
    _make_symlink(package_dir / "escape.so", "../../../../outside.so")
    (tmp_path / "outside.so").write_bytes(b"should never be reachable")

    with pytest.raises(package.UnsafeScriptRootEntryError):
        package._capture_script_root(root)

    (package_dir / "escape.so").unlink()
    captured = package._capture_script_root(root)
    with zipfile.ZipFile(io.BytesIO(captured.archive_bytes)) as archive:
        member = ".python_packages/lib/site-packages/native_pkg/libfoo.so"
        assert archive.read(member) == b"\x7fELF-fake-shared-object"


# ---------------------------------------------------------------------------
# Invalid roots
# ---------------------------------------------------------------------------


def test_capture_fails_closed_for_a_missing_script_root(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"

    with pytest.raises(package.ScriptRootUnavailableError):
        package._capture_script_root(missing)


def test_capture_fails_closed_when_the_script_root_is_a_file(tmp_path: Path) -> None:
    not_a_directory = tmp_path / "file.txt"
    not_a_directory.write_bytes(b"not a directory")

    with pytest.raises(package.ScriptRootUnavailableError):
        package._capture_script_root(not_a_directory)


# ---------------------------------------------------------------------------
# Secure traversal: no unchecked reopen-by-path (TOCTOU)
# ---------------------------------------------------------------------------


@_skip_unless_posix_secure_traversal
def test_posix_write_phase_fails_closed_when_a_scanned_file_is_replaced_by_an_escaping_symlink(
    tmp_path: Path,
) -> None:
    """Proves the write phase never reopens a captured path unchecked.

    If the write phase simply reopened ``a.py`` by path, this would silently
    archive the outside secret's bytes under ``a.py``'s name. Every hop is
    re-walked with ``O_NOFOLLOW`` from the root anchor, so the swapped-in
    symlink fails closed instead.
    """
    root = tmp_path / "app"
    _write_tree(root, {"a.py": b"original-content"})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"must-never-be-archived")

    entries = package._scan_posix_secure(root)
    (root / "a.py").unlink()
    _make_symlink(root / "a.py", str(outside / "secret.txt"))

    with _posix_root_fd(root) as root_fd, pytest.raises(package.ContentCaptureRaceError):
        package._write_deterministic_archive_posix(root_fd, entries)


@_skip_unless_posix_secure_traversal
def test_posix_write_phase_fails_closed_when_a_scanned_files_content_changes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "app"
    _write_tree(root, {"a.py": b"original"})

    entries = package._scan_posix_secure(root)
    (root / "a.py").write_bytes(b"mutated-content-of-a-different-length")

    with _posix_root_fd(root) as root_fd, pytest.raises(package.ContentCaptureRaceError):
        package._write_deterministic_archive_posix(root_fd, entries)


@_skip_unless_posix_secure_traversal
def test_posix_write_phase_fails_closed_when_a_scanned_file_disappears(tmp_path: Path) -> None:
    root = tmp_path / "app"
    _write_tree(root, {"a.py": b"original"})

    entries = package._scan_posix_secure(root)
    (root / "a.py").unlink()

    with _posix_root_fd(root) as root_fd, pytest.raises(package.ContentCaptureRaceError):
        package._write_deterministic_archive_posix(root_fd, entries)


@_skip_unless_posix_secure_traversal
def test_capture_fails_closed_when_content_changes_between_scan_and_write_via_public_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An end-to-end proof through the module's capture entry point."""
    root = tmp_path / "app"
    _write_tree(root, {"a.py": b"original"})
    real_entries = package._scan_posix_secure(root)

    def _stale_posix_scan(_root_fd: int) -> tuple[package._ScriptRootEntry, ...]:
        return real_entries

    monkeypatch.setattr(package, "_scan_posix_tree", _stale_posix_scan)
    (root / "a.py").write_bytes(b"mutated-content-of-a-different-length")

    with pytest.raises(package.ContentCaptureRaceError):
        package._capture_script_root(root)


@_skip_unless_posix_secure_traversal
def test_posix_capture_ignores_a_root_path_replacement_via_the_persistent_root_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root fd opened before the scan is immune to the root *path* being
    repointed at a different directory inode mid-capture.

    Renaming a different, unrelated inode onto ``root``'s path swaps which
    directory that name resolves to, without touching the original
    directory's own inode or contents (unlike deleting entries from within
    it, which is a real, correctly-detected mutation). The write phase --
    anchored to the fd opened before the swap -- must keep seeing the
    originally scanned directory, never whatever the path now names.
    """
    root = tmp_path / "app"
    _write_tree(root, {"a.py": b"original-content"})
    real_preflight = package._preflight_aggregate_archive_size

    def _repoint_root_path_then_preflight(entries: Sequence[package._ScriptRootEntry]) -> None:
        real_preflight(entries)
        replacement_root = tmp_path / "app-replacement"
        replacement_root.mkdir()
        (replacement_root / "a.py").write_bytes(b"REPLACEMENT-CONTENT-OF-A-DIFFERENT-LENGTH")
        moved_aside = tmp_path / "app-original-moved-aside"
        root.rename(moved_aside)
        replacement_root.rename(root)

    monkeypatch.setattr(
        package, "_preflight_aggregate_archive_size", _repoint_root_path_then_preflight
    )

    captured = package._capture_script_root(root)

    with zipfile.ZipFile(io.BytesIO(captured.archive_bytes)) as archive:
        assert archive.read("a.py") == b"original-content"


@_skip_unless_posix_secure_traversal
def test_posix_write_phase_fails_closed_on_an_inode_swap_with_matching_size_and_mtime(
    tmp_path: Path,
) -> None:
    """A same-size, same-mtime inode swap (a spoofed replacement) still fails closed.

    The replacement is written under a shadow name and renamed into place, so it
    coexists with the original file briefly and is guaranteed a distinct inode
    regardless of a given filesystem's reuse policy for a freed inode number.
    """
    root = tmp_path / "app"
    original_content = b"original-content"
    _write_tree(root, {"a.py": original_content})
    entries = package._scan_posix_secure(root)
    original_entry = next(entry for entry in entries if entry.archive_name == "a.py")

    time.sleep(0.02)
    shadow = root / "a.py.shadow"
    shadow.write_bytes(b"x" * len(original_content))
    os.utime(shadow, ns=(original_entry.mtime_ns, original_entry.mtime_ns))
    shadow.rename(root / "a.py")

    with _posix_root_fd(root) as root_fd, pytest.raises(package.ContentCaptureRaceError):
        package._write_deterministic_archive_posix(root_fd, entries)


@_skip_unless_posix_secure_traversal
def test_posix_write_phase_fails_closed_on_a_same_size_mutation_with_a_restored_mtime(
    tmp_path: Path,
) -> None:
    """Proves ctime, not just size/mtime, is checked: a same-size in-place
    rewrite with mtime forced back to its original value must still fail
    closed, since even that forced restore itself bumps ctime. A short sleep
    guarantees real wall-clock time passes, since a sub-tick-resolution
    rewrite could otherwise land on an unchanged ctime by coincidence."""
    root = tmp_path / "app"
    original_content = b"original-content"
    _write_tree(root, {"a.py": original_content})
    entries = package._scan_posix_secure(root)
    original_entry = next(entry for entry in entries if entry.archive_name == "a.py")

    time.sleep(0.02)
    (root / "a.py").write_bytes(b"y" * len(original_content))
    os.utime(root / "a.py", ns=(original_entry.mtime_ns, original_entry.mtime_ns))

    with _posix_root_fd(root) as root_fd, pytest.raises(package.ContentCaptureRaceError):
        package._write_deterministic_archive_posix(root_fd, entries)


@_skip_unless_posix_secure_traversal
def test_posix_capture_fails_closed_when_a_new_file_appears_before_the_final_rescan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "app"
    _write_tree(root, {"a.py": b"original"})
    real_write = package._write_deterministic_archive_posix

    def _write_then_add_file(root_fd: int, entries: Sequence[package._ScriptRootEntry]) -> bytes:
        result = real_write(root_fd, entries)
        (root / "b.py").write_bytes(b"appeared-after-the-scan")
        return result

    monkeypatch.setattr(package, "_write_deterministic_archive_posix", _write_then_add_file)

    with pytest.raises(package.ContentCaptureRaceError):
        package._capture_script_root(root)


@_skip_unless_posix_secure_traversal
def test_posix_capture_fails_closed_when_a_file_disappears_before_the_final_rescan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "app"
    _write_tree(root, {"a.py": b"original", "b.py": b"other"})
    real_write = package._write_deterministic_archive_posix

    def _write_then_remove_file(
        root_fd: int, entries: Sequence[package._ScriptRootEntry]
    ) -> bytes:
        result = real_write(root_fd, entries)
        (root / "b.py").unlink()
        return result

    monkeypatch.setattr(package, "_write_deterministic_archive_posix", _write_then_remove_file)

    with pytest.raises(package.ContentCaptureRaceError):
        package._capture_script_root(root)


@_skip_unless_posix_secure_traversal
def test_posix_capture_fails_closed_when_a_file_is_retyped_before_the_final_rescan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "app"
    _write_tree(root, {"a.py": b"original", "b.py": b"other"})
    real_write = package._write_deterministic_archive_posix

    def _write_then_retype(root_fd: int, entries: Sequence[package._ScriptRootEntry]) -> bytes:
        result = real_write(root_fd, entries)
        (root / "b.py").unlink()
        (root / "b.py").mkdir()
        return result

    monkeypatch.setattr(package, "_write_deterministic_archive_posix", _write_then_retype)

    with pytest.raises(package.ContentCaptureRaceError):
        package._capture_script_root(root)


def test_require_matching_entry_sets_accepts_identical_shapes() -> None:
    expected = (_synthetic_entry(name="x"), _synthetic_entry(name="y", is_directory=True))
    rescanned = (_synthetic_entry(name="x"), _synthetic_entry(name="y", is_directory=True))

    package._require_matching_entry_sets(expected, rescanned)


def test_require_matching_entry_sets_rejects_an_added_entry() -> None:
    expected = (_synthetic_entry(name="x"),)
    rescanned = (_synthetic_entry(name="x"), _synthetic_entry(name="y"))

    with pytest.raises(package.ContentCaptureRaceError):
        package._require_matching_entry_sets(expected, rescanned)


def test_require_matching_entry_sets_rejects_a_removed_entry() -> None:
    expected = (_synthetic_entry(name="x"), _synthetic_entry(name="y"))
    rescanned = (_synthetic_entry(name="x"),)

    with pytest.raises(package.ContentCaptureRaceError):
        package._require_matching_entry_sets(expected, rescanned)


def test_require_matching_entry_sets_rejects_a_retyped_entry() -> None:
    expected = (_synthetic_entry(name="x", is_directory=False),)
    rescanned = (_synthetic_entry(name="x", is_directory=True),)

    with pytest.raises(package.ContentCaptureRaceError):
        package._require_matching_entry_sets(expected, rescanned)


# ---------------------------------------------------------------------------
# Value leakage: paths and rejected targets must never appear in messages
# ---------------------------------------------------------------------------


@_skip_unless_posix_secure_traversal
def test_escaping_symlink_error_never_leaks_the_target_path(tmp_path: Path) -> None:
    root = tmp_path / "app"
    root.mkdir()
    secret_marker = "MUST-NOT-LEAK-THIS-PATH-SEGMENT"
    outside = tmp_path / secret_marker
    outside.write_bytes(b"outside\n")
    _make_symlink(root / "escape.py", str(outside))

    with pytest.raises(package.UnsafeScriptRootEntryError) as exc_info:
        package._capture_script_root(root)

    assert secret_marker not in str(exc_info.value)
    assert str(root) not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


@_skip_unless_posix_secure_traversal
def test_broken_symlink_error_never_leaks_the_root_path(tmp_path: Path) -> None:
    root = tmp_path / "app"
    root.mkdir()
    _make_symlink(root / "broken.py", "does-not-exist.py")

    with pytest.raises(package.UnsafeScriptRootEntryError) as exc_info:
        package._capture_script_root(root)

    assert str(root) not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


@_skip_unless_posix_secure_traversal
def test_race_error_never_leaks_the_root_path(tmp_path: Path) -> None:
    root = tmp_path / "app"
    _write_tree(root, {"a.py": b"original"})
    entries = package._scan_posix_secure(root)
    (root / "a.py").unlink()

    with _posix_root_fd(root) as root_fd, pytest.raises(package.ContentCaptureRaceError) as exc_info:
        package._write_deterministic_archive_posix(root_fd, entries)

    assert str(root) not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_missing_script_root_error_never_leaks_the_path(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist-anywhere"

    with pytest.raises(package.ScriptRootUnavailableError) as exc_info:
        package._capture_script_root(missing)

    assert str(missing) not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


# ---------------------------------------------------------------------------
# Narrow internal error-translation branches, exercised directly
# ---------------------------------------------------------------------------


def test_resolve_script_root_path_translates_an_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raising_resolve(self: Path, strict: bool = False) -> Path:
        raise OSError("simulated resolve failure")

    monkeypatch.setattr(Path, "resolve", _raising_resolve)

    with pytest.raises(package.ScriptRootUnavailableError) as exc_info:
        package._resolve_script_root_path(tmp_path)

    assert str(tmp_path) not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_script_root_is_directory_translates_an_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raising_is_dir(self: Path) -> bool:
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "is_dir", _raising_is_dir)

    with pytest.raises(package.ScriptRootUnavailableError) as exc_info:
        package._script_root_is_directory(tmp_path)

    assert str(tmp_path) not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


@_skip_unless_posix_secure_traversal
def test_posix_open_root_directory_translates_a_non_directory_path(tmp_path: Path) -> None:
    not_a_directory = tmp_path / "file.txt"
    not_a_directory.write_bytes(b"x")

    with pytest.raises(package.ScriptRootUnavailableError) as exc_info:
        package._open_root_directory(not_a_directory)

    assert str(not_a_directory) not in str(exc_info.value)


@_skip_unless_posix_secure_traversal
def test_posix_list_directory_names_translates_a_non_directory_fd(tmp_path: Path) -> None:
    a_file = tmp_path / "file.txt"
    a_file.write_bytes(b"x")
    fd = os.open(a_file, os.O_RDONLY)
    try:
        with pytest.raises(package.ContentCaptureRaceError):
            package._list_directory_names(fd)
    finally:
        os.close(fd)


@_skip_unless_posix_secure_traversal
def test_posix_lstat_child_translates_a_missing_name(tmp_path: Path) -> None:
    root = tmp_path / "app"
    root.mkdir()
    dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(package.ContentCaptureRaceError):
            package._lstat_child(dir_fd, "does-not-exist")
    finally:
        os.close(dir_fd)


@_skip_unless_posix_secure_traversal
def test_posix_open_directory_chain_checked_translates_a_missing_component(tmp_path: Path) -> None:
    root = tmp_path / "app"
    root.mkdir()
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(package.ContentCaptureRaceError):
            package._open_directory_chain_checked(root_fd, ("does-not-exist",))
    finally:
        os.close(root_fd)


@_skip_unless_posix_secure_traversal
def test_posix_read_symlink_target_translates_a_non_symlink(tmp_path: Path) -> None:
    root = tmp_path / "app"
    root.mkdir()
    (root / "regular.txt").write_bytes(b"x")
    dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(package.UnsafeScriptRootEntryError):
            package._read_symlink_target(dir_fd, "regular.txt")
    finally:
        os.close(dir_fd)


def test_resolve_symlink_target_rejects_a_target_that_resolves_to_nothing() -> None:
    assert package._resolve_symlink_target((), ".") is None
    assert package._resolve_symlink_target(("a",), "..") is None


def test_apply_symlink_target_segment_passes_dot_segments_through_unchanged() -> None:
    assert package._apply_symlink_target_segment(["a"], ".") == ["a"]
    assert package._apply_symlink_target_segment(["a"], "") == ["a"]


@_skip_unless_posix_secure_traversal
def test_capture_dereferences_a_symlink_whose_target_contains_a_dot_segment(
    tmp_path: Path,
) -> None:
    root = tmp_path / "app"
    _write_tree(root, {"real.txt": b"real-content"})
    _make_symlink(root / "link.txt", "./real.txt")

    captured = package._capture_script_root(root)

    with zipfile.ZipFile(io.BytesIO(captured.archive_bytes)) as archive:
        assert archive.read("link.txt") == b"real-content"


def test_require_posix_locator_rejects_an_entry_without_a_posix_locator() -> None:
    entry = _synthetic_entry(name="f")

    with pytest.raises(package.ContentPackagingError):
        package._require_posix_locator(entry)


def _fake_stat_result(
    *, is_directory: bool = False, device: int = 1, inode: int = 1, size: int = 100,
    mtime_ns: int = 5_000, ctime_ns: int = 5_000,
) -> os.stat_result:
    mode = (stat.S_IFDIR if is_directory else stat.S_IFREG) | 0o644
    sequence = (mode, inode, device, 1, 0, 0, size, 0, 0, 0)
    return os.stat_result(sequence, {"st_mtime_ns": mtime_ns, "st_ctime_ns": ctime_ns})


def test_require_matching_fd_stat_rejects_a_size_that_changed_during_the_copy() -> None:
    entry = _synthetic_entry(size=100)
    mismatched = _fake_stat_result(device=0, inode=0, size=99, mtime_ns=0, ctime_ns=0)

    with pytest.raises(package.ContentCaptureRaceError):
        package._require_matching_fd_stat(mismatched, entry)


def test_require_matching_fd_stat_rejects_a_ctime_change_with_an_unchanged_size_and_mtime() -> None:
    """A same-size, same-mtime rewrite (e.g. a forged utime()) still bumps ctime."""
    entry = package._ScriptRootEntry(
        archive_name="f",
        is_directory=False,
        size=100,
        mtime_ns=5_000,
        ctime_ns=5_000,
        device=1,
        inode=1,
        executable=False,
        locator=None,
    )
    observed = _fake_stat_result(device=1, inode=1, size=100, mtime_ns=5_000, ctime_ns=6_000)

    with pytest.raises(package.ContentCaptureRaceError):
        package._require_matching_fd_stat(observed, entry)


def test_require_matching_fd_stat_rejects_an_inode_swap_with_the_same_size_and_mtime() -> None:
    entry = package._ScriptRootEntry(
        archive_name="f",
        is_directory=False,
        size=100,
        mtime_ns=5_000,
        ctime_ns=5_000,
        device=1,
        inode=1,
        executable=False,
        locator=None,
    )
    observed = _fake_stat_result(device=1, inode=2, size=100, mtime_ns=5_000, ctime_ns=5_000)

    with pytest.raises(package.ContentCaptureRaceError):
        package._require_matching_fd_stat(observed, entry)


def test_require_matching_fd_stat_accepts_a_fully_matching_observation() -> None:
    entry = package._ScriptRootEntry(
        archive_name="f",
        is_directory=False,
        size=100,
        mtime_ns=5_000,
        ctime_ns=5_000,
        device=1,
        inode=1,
        executable=False,
        locator=None,
    )
    observed = _fake_stat_result(device=1, inode=1, size=100, mtime_ns=5_000, ctime_ns=5_000)

    package._require_matching_fd_stat(observed, entry)


@_skip_unless_posix_secure_traversal
def test_copy_posix_fd_into_archive_translates_an_oserror_during_the_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "app"
    _write_tree(root, {"a.py": b"content"})
    entry = package._scan_posix_secure(root)[0]
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = package._open_posix_regular_file_for_read(root_fd, entry.locator.components)  # type: ignore[union-attr]
        info = package._archive_member_info(entry)

        def _raise_during_copy(_source: object, _dest: object) -> None:
            raise OSError("simulated mid-copy failure")

        monkeypatch.setattr(package, "_copy_file_into_archive_entry", _raise_during_copy)

        with tempfile_zip_archive() as archive, pytest.raises(package.ContentCaptureRaceError):
            package._copy_posix_fd_into_archive(fd, entry, archive, info)
    finally:
        os.close(root_fd)


@pytest.mark.asyncio
async def test_content_archive_landed_propagates_an_operational_stat_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stat failure right after a successful write is inconclusive, not a
    mismatch: it propagates so the caller can classify it, rather than being
    folded into a false "not landed" verdict."""
    transport = FakeSandboxTransport()
    fault = SandboxFileOperationError("transient stat failure")

    async def _fail_stat(self: FakeSandboxTransport, path: str) -> SandboxFileStat:
        del self, path
        raise fault

    monkeypatch.setattr(FakeSandboxTransport, "stat_file", _fail_stat)

    with pytest.raises(SandboxFileOperationError) as exc_info:
        await package._content_archive_landed(transport, expected_size=100)

    assert exc_info.value is fault


@pytest.mark.asyncio
async def test_content_archive_landed_treats_a_missing_file_as_not_landed() -> None:
    transport = FakeSandboxTransport()

    landed = await package._content_archive_landed(transport, expected_size=100)

    assert landed is False


# ---------------------------------------------------------------------------
# Standard-ZIP (non-ZIP64) and aggregate-size preflight boundaries
# ---------------------------------------------------------------------------


def test_preflight_rejects_more_entries_than_the_standard_zip_format_allows() -> None:
    entries = tuple(
        _synthetic_entry(name=f"f{i}") for i in range(package._MAX_STANDARD_ZIP_ENTRIES + 1)
    )

    with pytest.raises(package.ContentArchiveTooLargeError):
        package._preflight_standard_zip_limits(entries)


def test_preflight_accepts_exactly_the_maximum_standard_zip_entry_count() -> None:
    entries = tuple(_synthetic_entry(name=f"f{i}") for i in range(package._MAX_STANDARD_ZIP_ENTRIES))

    package._preflight_standard_zip_limits(entries)


def test_preflight_rejects_a_single_entry_larger_than_the_standard_zip_format_allows() -> None:
    entries = (_synthetic_entry(size=package._MAX_STANDARD_ZIP_ENTRY_SIZE + 1),)

    with pytest.raises(package.ContentArchiveTooLargeError):
        package._preflight_standard_zip_limits(entries)


def test_preflight_accepts_a_single_entry_at_exactly_the_standard_zip_size_limit() -> None:
    entries = (_synthetic_entry(size=package._MAX_STANDARD_ZIP_ENTRY_SIZE),)

    package._preflight_standard_zip_limits(entries)


def _entry_zip_overhead(name: str) -> int:
    name_bytes = len(name.encode("utf-8"))
    return (
        package._ZIP_LOCAL_HEADER_FIXED_SIZE
        + name_bytes
        + package._ZIP_CENTRAL_DIRECTORY_FIXED_SIZE
        + name_bytes
    )


def test_aggregate_preflight_accepts_entries_at_exactly_the_operational_bound() -> None:
    overhead = package._ZIP_END_OF_CENTRAL_DIRECTORY_SIZE + _entry_zip_overhead("f")
    size_at_bound = package._MAX_ARCHIVE_OPERATIONAL_SIZE - overhead
    entries = (_synthetic_entry(name="f", size=size_at_bound),)

    package._preflight_aggregate_archive_size(entries)


def test_aggregate_preflight_rejects_entries_one_byte_over_the_operational_bound() -> None:
    overhead = package._ZIP_END_OF_CENTRAL_DIRECTORY_SIZE + _entry_zip_overhead("f")
    size_at_bound = package._MAX_ARCHIVE_OPERATIONAL_SIZE - overhead
    entries = (_synthetic_entry(name="f", size=size_at_bound + 1),)

    with pytest.raises(package.ContentArchiveTooLargeError):
        package._preflight_aggregate_archive_size(entries)


def test_aggregate_preflight_rejects_many_entries_that_individually_pass_per_entry_limits() -> None:
    """Two entries can each pass the per-entry/count checks yet blow the aggregate bound."""
    half = package._MAX_STANDARD_ZIP_ENTRY_SIZE
    entries = (
        _synthetic_entry(name="a", size=half),
        _synthetic_entry(name="b", size=half),
    )

    with pytest.raises(package.ContentArchiveTooLargeError):
        package._preflight_aggregate_archive_size(entries)


@_skip_unless_posix_secure_traversal
def test_preflight_runs_before_the_archive_is_ever_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "app"
    root.mkdir()
    oversized = (_synthetic_entry(size=package._MAX_STANDARD_ZIP_ENTRY_SIZE + 1),)
    monkeypatch.setattr(package, "_scan_posix_tree", lambda _root_fd: oversized)

    def _fail_if_called(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("the archive must never be written once preflight has rejected it")

    monkeypatch.setattr(package, "_write_deterministic_archive_posix", _fail_if_called)

    with pytest.raises(package.ContentArchiveTooLargeError):
        package._capture_script_root(root)


@_skip_unless_posix_secure_traversal
def test_aggregate_preflight_runs_before_the_archive_is_ever_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "app"
    root.mkdir()
    overhead = package._ZIP_END_OF_CENTRAL_DIRECTORY_SIZE + _entry_zip_overhead("f")
    oversized = (
        _synthetic_entry(name="f", size=package._MAX_ARCHIVE_OPERATIONAL_SIZE - overhead + 1),
    )
    monkeypatch.setattr(package, "_scan_posix_tree", lambda _root_fd: oversized)

    def _fail_if_called(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("the archive must never be written once preflight has rejected it")

    monkeypatch.setattr(package, "_write_deterministic_archive_posix", _fail_if_called)

    with pytest.raises(package.ContentArchiveTooLargeError):
        package._capture_script_root(root)


def test_write_deterministic_archive_translates_a_late_large_zip_file_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stdlib zipfile applies its own internal margin below our preflight's raw
    size bound (verified empirically against the installed zipfile module), so
    the writer itself must also translate a late ``LargeZipFile`` failure.
    """
    directory_entry = _synthetic_entry(name="huge/", is_directory=True)

    def _raise_large_zip_file(*_args: object, **_kwargs: object) -> None:
        raise zipfile.LargeZipFile("Filesize would require ZIP64 extensions")

    monkeypatch.setattr(zipfile.ZipFile, "open", _raise_large_zip_file)

    def write_entry(archive: zipfile.ZipFile, entry: package._ScriptRootEntry) -> None:
        package._write_entry_posix(archive, -1, entry)

    with pytest.raises(package.ContentArchiveTooLargeError):
        package._stream_entries_into_archive((directory_entry,), write_entry)


@_skip_unless_posix_secure_traversal
def test_capture_translates_a_late_large_zip_file_error_into_a_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "app"
    _write_tree(root, {"main.py": b"print(1)\n"})

    def _raise_large_zip_file(*_args: object, **_kwargs: object) -> None:
        raise zipfile.LargeZipFile("Filesize would require ZIP64 extensions")

    monkeypatch.setattr(zipfile.ZipFile, "open", _raise_large_zip_file)

    with pytest.raises(package.ContentArchiveTooLargeError):
        package._capture_script_root(root)


# ---------------------------------------------------------------------------
# build_expected_manifest_binding
# ---------------------------------------------------------------------------


def test_build_expected_manifest_binding_stamps_all_fields_from_the_session_row() -> None:
    session = _session_record()
    caller_fingerprint = "s1-" + ("e" * 52)
    assert caller_fingerprint != session.state_store_fingerprint

    expected = package.build_expected_manifest_binding(
        session,
        sandbox_group_resource_id=_GROUP,
        state_store_fingerprint=caller_fingerprint,
    )

    assert expected.manifest_version == package.MANIFEST_VERSION
    assert expected.protocol_version == session.protocol
    assert expected.session_id == session.session_id
    assert expected.owner_hash_version == session.owner_partition.owner_hash_version
    assert expected.owner_hash == session.owner_partition.owner_hash
    assert expected.app_hash == session.owner_partition.app_hash
    assert expected.sandbox_group_resource_id == _GROUP
    assert expected.sandbox_id == session.sandbox_id
    assert expected.generation == session.generation
    assert expected.digest_kind == session.digest_kind
    assert expected.digest == session.digest
    # The caller's explicit argument is stamped, not whatever value already sits
    # on the session row: this function never compares current vs. stored state,
    # it only stamps the value it is given.
    assert expected.state_store_fingerprint == caller_fingerprint


def test_build_expected_manifest_binding_requires_a_non_null_sandbox_id() -> None:
    session = _session_record(sandbox_id=None)

    with pytest.raises(package.SessionSandboxIdentityRequiredError):
        package.build_expected_manifest_binding(
            session,
            sandbox_group_resource_id=_GROUP,
            state_store_fingerprint=_STATE_STORE_FINGERPRINT,
        )


# ---------------------------------------------------------------------------
# deliver_content_package: order, preconditions, verification, and retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delivery_writes_content_then_sidecar_then_seed_and_never_touches_the_live_manifest() -> (
    None
):
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)

    delivered = await package.deliver_content_package(
        transport, captured, expected, _live_identity()
    )

    assert delivered.package is captured
    assert delivered.expected_binding == expected
    assert [call.operation for call in transport.calls] == [
        "write_file",
        "stat_file",
        "write_file",
        "read_file",
        "write_file",
        "read_file",
    ]
    written_paths = [call.path for call in transport.calls if call.operation == "write_file"]
    assert written_paths == [
        package.CONTENT_ARCHIVE_PATH,
        package.CONTENT_DIGEST_SIDECAR_PATH,
        package.CONTENT_MANIFEST_SEED_PATH,
    ]
    assert all(call.path != SESSION_MANIFEST_PATH for call in transport.calls)
    assert all(call.operation != "exec" for call in transport.calls)


@pytest.mark.asyncio
async def test_delivery_rejects_a_captured_digest_that_disagrees_with_the_authoritative_binding() -> (
    None
):
    transport = FakeSandboxTransport()
    captured = _captured_package(b"one-bytes")
    other_captured = _captured_package(b"different-bytes")
    expected = _expected_binding(other_captured)

    with pytest.raises(package.ContentBindingMismatchError):
        await package.deliver_content_package(transport, captured, expected, _live_identity())

    assert transport.calls == []


@pytest.mark.asyncio
async def test_delivery_rejects_when_live_sandbox_identity_disagrees_with_the_expected_binding() -> (
    None
):
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)
    repointed_identity = _live_identity(sandbox_id="repointed-sandbox")

    with pytest.raises(package.ContentBindingMismatchError):
        await package.deliver_content_package(
            transport, captured, expected, repointed_identity
        )

    assert transport.calls == []


@pytest.mark.asyncio
async def test_delivery_detects_a_content_size_mismatch_after_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeSandboxTransport()
    captured = _captured_package(b"x" * 4096)
    expected = _expected_binding(captured)
    real_write_file = FakeSandboxTransport.write_file

    async def _write_truncated(
        self: FakeSandboxTransport, path: str, content: bytes, *, create_dirs: bool = False
    ) -> None:
        if path == package.CONTENT_ARCHIVE_PATH:
            content = content[:-1]
        await real_write_file(self, path, content, create_dirs=create_dirs)

    monkeypatch.setattr(FakeSandboxTransport, "write_file", _write_truncated)

    with pytest.raises(package.ContentDeliveryVerificationError):
        await package.deliver_content_package(transport, captured, expected, _live_identity())


@pytest.mark.asyncio
async def test_delivery_detects_a_corrupted_digest_sidecar(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)
    real_write_file = FakeSandboxTransport.write_file

    async def _write_corrupted_sidecar(
        self: FakeSandboxTransport, path: str, content: bytes, *, create_dirs: bool = False
    ) -> None:
        if path == package.CONTENT_DIGEST_SIDECAR_PATH:
            content = b"sha256:not-the-digest\n"
        await real_write_file(self, path, content, create_dirs=create_dirs)

    monkeypatch.setattr(FakeSandboxTransport, "write_file", _write_corrupted_sidecar)

    with pytest.raises(package.ContentDeliveryVerificationError):
        await package.deliver_content_package(transport, captured, expected, _live_identity())


@pytest.mark.asyncio
async def test_delivery_detects_a_truncated_manifest_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)
    real_write_file = FakeSandboxTransport.write_file

    async def _write_truncated_seed(
        self: FakeSandboxTransport, path: str, content: bytes, *, create_dirs: bool = False
    ) -> None:
        if path == package.CONTENT_MANIFEST_SEED_PATH:
            content = content[: len(content) // 2]
        await real_write_file(self, path, content, create_dirs=create_dirs)

    monkeypatch.setattr(FakeSandboxTransport, "write_file", _write_truncated_seed)

    with pytest.raises(package.ContentDeliveryVerificationError):
        await package.deliver_content_package(transport, captured, expected, _live_identity())


@pytest.mark.asyncio
async def test_delivery_detects_a_malformed_manifest_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)
    real_write_file = FakeSandboxTransport.write_file

    async def _write_malformed_seed(
        self: FakeSandboxTransport, path: str, content: bytes, *, create_dirs: bool = False
    ) -> None:
        if path == package.CONTENT_MANIFEST_SEED_PATH:
            content = b"not-json-at-all"
        await real_write_file(self, path, content, create_dirs=create_dirs)

    monkeypatch.setattr(FakeSandboxTransport, "write_file", _write_malformed_seed)

    with pytest.raises(package.ContentDeliveryVerificationError):
        await package.deliver_content_package(transport, captured, expected, _live_identity())


@pytest.mark.asyncio
async def test_delivery_detects_a_duplicate_key_manifest_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)
    real_write_file = FakeSandboxTransport.write_file
    duplicated = b'{"manifest_version":1,"manifest_version":2,"session_id":"s"}'

    async def _write_duplicated_seed(
        self: FakeSandboxTransport, path: str, content: bytes, *, create_dirs: bool = False
    ) -> None:
        if path == package.CONTENT_MANIFEST_SEED_PATH:
            content = duplicated
        await real_write_file(self, path, content, create_dirs=create_dirs)

    monkeypatch.setattr(FakeSandboxTransport, "write_file", _write_duplicated_seed)

    with pytest.raises(package.ContentDeliveryVerificationError):
        await package.deliver_content_package(transport, captured, expected, _live_identity())


@pytest.mark.asyncio
async def test_delivery_retries_idempotently_with_the_same_digest() -> None:
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)

    first = await package.deliver_content_package(transport, captured, expected, _live_identity())
    second = await package.deliver_content_package(
        transport, captured, expected, _live_identity()
    )

    assert first == second


@pytest.mark.asyncio
async def test_delivery_retry_replaces_a_previously_incomplete_content_archive() -> None:
    transport = FakeSandboxTransport()
    captured = _captured_package(b"x" * 4096)
    expected = _expected_binding(captured)
    transport.seed_file(package.CONTENT_ARCHIVE_PATH, captured.archive_bytes[:-1])

    delivered = await package.deliver_content_package(
        transport, captured, expected, _live_identity()
    )

    assert await transport.read_file(package.CONTENT_ARCHIVE_PATH) == captured.archive_bytes
    assert delivered.package is captured


@pytest.mark.asyncio
async def test_delivery_treats_a_raised_sidecar_write_as_success_when_the_bytes_actually_landed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)
    real_write_file = FakeSandboxTransport.write_file

    async def _write_then_raise(
        self: FakeSandboxTransport, path: str, content: bytes, *, create_dirs: bool = False
    ) -> None:
        await real_write_file(self, path, content, create_dirs=create_dirs)
        if path == package.CONTENT_DIGEST_SIDECAR_PATH:
            raise SandboxFileOperationError("ack lost after commit")

    monkeypatch.setattr(FakeSandboxTransport, "write_file", _write_then_raise)

    delivered = await package.deliver_content_package(
        transport, captured, expected, _live_identity()
    )

    assert delivered.package is captured


@pytest.mark.asyncio
async def test_delivery_propagates_the_original_error_when_an_uncertain_sidecar_write_never_landed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)
    fault = SandboxFileOperationError("write never reached the sandbox")
    real_write_file = FakeSandboxTransport.write_file

    async def _raise_without_writing(
        self: FakeSandboxTransport, path: str, content: bytes, *, create_dirs: bool = False
    ) -> None:
        if path == package.CONTENT_DIGEST_SIDECAR_PATH:
            raise fault
        await real_write_file(self, path, content, create_dirs=create_dirs)

    monkeypatch.setattr(FakeSandboxTransport, "write_file", _raise_without_writing)

    with pytest.raises(SandboxFileOperationError) as exc_info:
        await package.deliver_content_package(transport, captured, expected, _live_identity())

    assert exc_info.value is fault


@pytest.mark.asyncio
async def test_delivery_never_reclassifies_a_failed_content_write_even_with_a_same_sized_stale_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A coincidentally same-sized stale file must never masquerade as a landed write.

    Unlike the sidecar/seed (verified byte-for-byte and by strict re-parse),
    the archive is verified by size only, so an ambiguous write outcome is
    never reclassified as success here: the raised exception always
    propagates as-is, even when a same-sized file already sits at the path.
    """
    transport = FakeSandboxTransport()
    captured = _captured_package(b"y" * 4096)
    expected = _expected_binding(captured)
    stale_but_same_size = b"z" * len(captured.archive_bytes)
    transport.seed_file(package.CONTENT_ARCHIVE_PATH, stale_but_same_size)
    fault = SandboxFileOperationError("write never reached the sandbox")

    async def _raise_without_writing(
        self: FakeSandboxTransport, path: str, content: bytes, *, create_dirs: bool = False
    ) -> None:
        del content, create_dirs
        if path == package.CONTENT_ARCHIVE_PATH:
            raise fault

    monkeypatch.setattr(FakeSandboxTransport, "write_file", _raise_without_writing)

    with pytest.raises(SandboxFileOperationError) as exc_info:
        await package.deliver_content_package(transport, captured, expected, _live_identity())

    assert exc_info.value is fault
    assert await transport.read_file(package.CONTENT_ARCHIVE_PATH) == stale_but_same_size


@pytest.mark.asyncio
async def test_delivery_propagates_the_original_error_when_the_content_write_never_landed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)
    fault = SandboxFileOperationError("content write never reached the sandbox")

    async def _raise_without_writing(
        self: FakeSandboxTransport, path: str, content: bytes, *, create_dirs: bool = False
    ) -> None:
        del content, create_dirs
        if path == package.CONTENT_ARCHIVE_PATH:
            raise fault

    monkeypatch.setattr(FakeSandboxTransport, "write_file", _raise_without_writing)

    with pytest.raises(SandboxFileOperationError) as exc_info:
        await package.deliver_content_package(transport, captured, expected, _live_identity())

    assert exc_info.value is fault


@pytest.mark.asyncio
async def test_delivery_treats_a_raised_seed_write_as_success_when_it_actually_landed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)
    real_write_file = FakeSandboxTransport.write_file

    async def _write_then_raise(
        self: FakeSandboxTransport, path: str, content: bytes, *, create_dirs: bool = False
    ) -> None:
        await real_write_file(self, path, content, create_dirs=create_dirs)
        if path == package.CONTENT_MANIFEST_SEED_PATH:
            raise SandboxFileOperationError("ack lost after commit")

    monkeypatch.setattr(FakeSandboxTransport, "write_file", _write_then_raise)

    delivered = await package.deliver_content_package(
        transport, captured, expected, _live_identity()
    )

    assert delivered.expected_binding == expected


@pytest.mark.asyncio
async def test_delivery_propagates_the_original_error_when_the_seed_write_never_landed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)
    fault = SandboxFileOperationError("seed write never reached the sandbox")
    real_write_file = FakeSandboxTransport.write_file

    async def _raise_without_writing(
        self: FakeSandboxTransport, path: str, content: bytes, *, create_dirs: bool = False
    ) -> None:
        if path == package.CONTENT_MANIFEST_SEED_PATH:
            raise fault
        await real_write_file(self, path, content, create_dirs=create_dirs)

    monkeypatch.setattr(FakeSandboxTransport, "write_file", _raise_without_writing)

    with pytest.raises(SandboxFileOperationError) as exc_info:
        await package.deliver_content_package(transport, captured, expected, _live_identity())

    assert exc_info.value is fault


@pytest.mark.asyncio
async def test_delivery_does_not_mask_an_uncertain_seed_write_with_an_unrelated_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read-back's own failure must not overwrite the original write fault's identity."""
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)
    write_fault = SandboxFileOperationError("seed write never reached the sandbox")
    read_fault = SandboxFileOperationError("read-back also failed")
    real_write_file = FakeSandboxTransport.write_file
    real_read_file = FakeSandboxTransport.read_file

    async def _raise_without_writing(
        self: FakeSandboxTransport, path: str, content: bytes, *, create_dirs: bool = False
    ) -> None:
        if path == package.CONTENT_MANIFEST_SEED_PATH:
            raise write_fault
        await real_write_file(self, path, content, create_dirs=create_dirs)

    async def _fail_seed_read_back(self: FakeSandboxTransport, path: str) -> bytes:
        if path == package.CONTENT_MANIFEST_SEED_PATH:
            raise read_fault
        return await real_read_file(self, path)

    monkeypatch.setattr(FakeSandboxTransport, "write_file", _raise_without_writing)
    monkeypatch.setattr(FakeSandboxTransport, "read_file", _fail_seed_read_back)

    with pytest.raises(SandboxFileOperationError) as exc_info:
        await package.deliver_content_package(transport, captured, expected, _live_identity())

    assert exc_info.value is write_fault


@pytest.mark.asyncio
async def test_delivery_does_not_mask_an_uncertain_sidecar_write_with_an_unrelated_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors the seed case: the read-back's own failure must not overwrite
    the original write fault's identity for the sidecar path either."""
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)
    write_fault = SandboxFileOperationError("sidecar write never reached the sandbox")
    read_fault = SandboxFileOperationError("read-back also failed")
    real_write_file = FakeSandboxTransport.write_file
    real_read_file = FakeSandboxTransport.read_file

    async def _raise_without_writing(
        self: FakeSandboxTransport, path: str, content: bytes, *, create_dirs: bool = False
    ) -> None:
        if path == package.CONTENT_DIGEST_SIDECAR_PATH:
            raise write_fault
        await real_write_file(self, path, content, create_dirs=create_dirs)

    async def _fail_sidecar_read_back(self: FakeSandboxTransport, path: str) -> bytes:
        if path == package.CONTENT_DIGEST_SIDECAR_PATH:
            raise read_fault
        return await real_read_file(self, path)

    monkeypatch.setattr(FakeSandboxTransport, "write_file", _raise_without_writing)
    monkeypatch.setattr(FakeSandboxTransport, "read_file", _fail_sidecar_read_back)

    with pytest.raises(SandboxFileOperationError) as exc_info:
        await package.deliver_content_package(transport, captured, expected, _live_identity())

    assert exc_info.value is write_fault


@pytest.mark.asyncio
async def test_delivery_propagates_an_operational_failure_verifying_a_clean_archive_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean (non-raising) write's own verification stat failing operationally
    (auth, throttling, network) is not a mismatch and must not be swallowed into
    ``ContentDeliveryVerificationError``; the caller needs to see and classify it."""
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)
    fault = SandboxFileOperationError("forbidden", status_code=403)
    real_stat_file = FakeSandboxTransport.stat_file

    async def _fail_archive_stat(self: FakeSandboxTransport, path: str) -> SandboxFileStat:
        if path == package.CONTENT_ARCHIVE_PATH:
            raise fault
        return await real_stat_file(self, path)

    monkeypatch.setattr(FakeSandboxTransport, "stat_file", _fail_archive_stat)

    with pytest.raises(SandboxFileOperationError) as exc_info:
        await package.deliver_content_package(transport, captured, expected, _live_identity())

    assert exc_info.value is fault
    assert await transport.read_file(package.CONTENT_ARCHIVE_PATH) == captured.archive_bytes


@pytest.mark.asyncio
async def test_delivery_propagates_an_operational_failure_verifying_a_clean_sidecar_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)
    fault = SandboxFileOperationError("transient failure", status_code=503)
    real_read_file = FakeSandboxTransport.read_file

    async def _fail_sidecar_read(self: FakeSandboxTransport, path: str) -> bytes:
        if path == package.CONTENT_DIGEST_SIDECAR_PATH:
            raise fault
        return await real_read_file(self, path)

    monkeypatch.setattr(FakeSandboxTransport, "read_file", _fail_sidecar_read)

    with pytest.raises(SandboxFileOperationError) as exc_info:
        await package.deliver_content_package(transport, captured, expected, _live_identity())

    assert exc_info.value is fault


@pytest.mark.asyncio
async def test_delivery_propagates_an_operational_failure_verifying_a_clean_seed_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)
    fault = SandboxFileOperationError("network error")
    real_read_file = FakeSandboxTransport.read_file

    async def _fail_seed_read(self: FakeSandboxTransport, path: str) -> bytes:
        if path == package.CONTENT_MANIFEST_SEED_PATH:
            raise fault
        return await real_read_file(self, path)

    monkeypatch.setattr(FakeSandboxTransport, "read_file", _fail_seed_read)

    with pytest.raises(SandboxFileOperationError) as exc_info:
        await package.deliver_content_package(transport, captured, expected, _live_identity())

    assert exc_info.value is fault


@pytest.mark.asyncio
async def test_delivery_never_writes_or_disturbs_an_existing_live_manifest() -> None:
    transport = FakeSandboxTransport()
    existing_manifest = b'{"session_id": "already-published"}'
    transport.seed_file(SESSION_MANIFEST_PATH, existing_manifest)
    captured = _captured_package()
    expected = _expected_binding(captured)

    await package.deliver_content_package(transport, captured, expected, _live_identity())

    assert await transport.read_file(SESSION_MANIFEST_PATH) == existing_manifest


@pytest.mark.asyncio
@_skip_unless_posix_secure_traversal
async def test_delivery_succeeds_for_a_payload_over_four_mebibytes(tmp_path: Path) -> None:
    root = tmp_path / "app"
    root.mkdir()
    (root / "large.bin").write_bytes(b"A" * (5 * 1024 * 1024))
    captured = package._capture_script_root(root)
    expected = _expected_binding(captured)
    transport = FakeSandboxTransport()

    delivered = await package.deliver_content_package(
        transport, captured, expected, _live_identity()
    )

    assert delivered.package.size > 4 * 1024 * 1024
    assert await transport.read_file(package.CONTENT_ARCHIVE_PATH) == captured.archive_bytes


# ---------------------------------------------------------------------------
# read_live_manifest_binding: harness-authored live manifest capture
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_live_manifest_binding_succeeds_for_an_exact_harness_published_manifest() -> (
    None
):
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)
    transport.seed_file(SESSION_MANIFEST_PATH, render_sandbox_manifest_binding(expected))

    observed = await package.read_live_manifest_binding(transport, expected, _live_identity())

    assert observed.session_id == expected.session_id
    assert observed.digest == expected.digest
    assert observed.state_store_fingerprint == expected.state_store_fingerprint


@pytest.mark.asyncio
async def test_read_live_manifest_binding_reports_not_ready_when_missing() -> None:
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)

    with pytest.raises(package.LiveManifestNotReadyError):
        await package.read_live_manifest_binding(transport, expected, _live_identity())


@pytest.mark.asyncio
async def test_read_live_manifest_binding_propagates_a_transient_operational_read_failure() -> (
    None
):
    """A non-missing operational failure is not folded into "not ready": the
    caller needs to see and classify it (retryable vs. permanent), not have
    it silently treated the same as a simple absence."""
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)
    fault = SandboxFileOperationError("transient failure", status_code=503)
    transport.read_errors.append(fault)

    with pytest.raises(SandboxFileOperationError) as exc_info:
        await package.read_live_manifest_binding(transport, expected, _live_identity())

    assert exc_info.value is fault


@pytest.mark.asyncio
async def test_read_live_manifest_binding_propagates_a_permanent_auth_read_failure() -> None:
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)
    fault = SandboxFileOperationError("forbidden", status_code=403)
    transport.read_errors.append(fault)

    with pytest.raises(SandboxFileOperationError) as exc_info:
        await package.read_live_manifest_binding(transport, expected, _live_identity())

    assert exc_info.value is fault


@pytest.mark.asyncio
async def test_read_live_manifest_binding_rejects_repointed_sandbox_with_forged_digest() -> None:
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)
    forged = replace(expected, sandbox_id="repointed-sandbox", digest="sha256:" + ("9" * 64))
    transport.seed_file(SESSION_MANIFEST_PATH, render_sandbox_manifest_binding(forged))

    with pytest.raises(SandboxManifestMismatchError) as exc_info:
        await package.read_live_manifest_binding(transport, expected, _live_identity())

    assert {"sandbox_id", "digest"} <= exc_info.value.fields
    assert expected.digest not in str(exc_info.value)
    assert expected.owner_hash not in str(exc_info.value)


@pytest.mark.asyncio
async def test_read_live_manifest_binding_rejects_a_repointed_sandbox_group() -> None:
    """Even a manifest that matches ``expected`` exactly must fail when the
    live ACA identity itself resolves to a different Sandbox Group."""
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)
    transport.seed_file(SESSION_MANIFEST_PATH, render_sandbox_manifest_binding(expected))
    repointed_group = (
        "/subscriptions/sub-123/resourceGroups/rg-agent/"
        "providers/Microsoft.App/sandboxGroups/repointed-group"
    )
    repointed_identity = _live_identity(group_resource_id=repointed_group)

    with pytest.raises(SandboxManifestMismatchError) as exc_info:
        await package.read_live_manifest_binding(transport, expected, repointed_identity)

    assert exc_info.value.fields == frozenset({"sandbox_group_resource_id"})


@pytest.mark.asyncio
async def test_read_live_manifest_binding_rejects_a_rolled_back_generation() -> None:
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured, generation=5)
    stale = replace(expected, generation=4)
    transport.seed_file(SESSION_MANIFEST_PATH, render_sandbox_manifest_binding(stale))

    with pytest.raises(SandboxManifestMismatchError) as exc_info:
        await package.read_live_manifest_binding(transport, expected, _live_identity())

    assert exc_info.value.fields == frozenset({"generation"})


@pytest.mark.asyncio
async def test_read_live_manifest_binding_rejects_a_stale_state_store_fingerprint() -> None:
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)
    stale = replace(expected, state_store_fingerprint="s1-" + ("0" * 52))
    transport.seed_file(SESSION_MANIFEST_PATH, render_sandbox_manifest_binding(stale))

    with pytest.raises(SandboxManifestMismatchError) as exc_info:
        await package.read_live_manifest_binding(transport, expected, _live_identity())

    assert exc_info.value.fields == frozenset({"state_store_fingerprint"})


@pytest.mark.asyncio
async def test_read_live_manifest_binding_rejects_malformed_json_without_echoing_values() -> None:
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)
    transport.seed_file(SESSION_MANIFEST_PATH, b"{not-json")

    with pytest.raises(SandboxManifestMismatchError) as exc_info:
        await package.read_live_manifest_binding(transport, expected, _live_identity())

    assert exc_info.value.fields == frozenset({"manifest"})


@pytest.mark.asyncio
async def test_read_live_manifest_binding_rejects_duplicate_json_keys() -> None:
    transport = FakeSandboxTransport()
    captured = _captured_package()
    expected = _expected_binding(captured)
    duplicated = (
        b'{"manifest_version":1,"manifest_version":2,"protocol_version":"p",'
        b'"session_id":"s","owner_hash_version":"o1","owner_hash":"o",'
        b'"app_hash":"a","sandbox_group_resource_id":"' + _GROUP.encode() + b'",'
        b'"sandbox_id":"sandbox-123","generation":1,"digest_kind":"k","digest":"d",'
        b'"state_store_fingerprint":"' + _STATE_STORE_FINGERPRINT.encode() + b'"}'
    )
    transport.seed_file(SESSION_MANIFEST_PATH, duplicated)

    with pytest.raises(SandboxManifestMismatchError) as exc_info:
        await package.read_live_manifest_binding(transport, expected, _live_identity())

    assert exc_info.value.fields == frozenset({"manifest"})
