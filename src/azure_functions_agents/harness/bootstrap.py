"""Stdlib-only sandbox bootstrap run by the disk entrypoint supervisor."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import shutil
import site
import stat
import sys
import sysconfig
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

SANDBOX_MARKER_ENV_VAR = "AZURE_FUNCTIONS_AGENTS_SANDBOX"
CONTENT_DIRECTORY_NAME = "content"
CONTENT_ARCHIVE_NAME = "app.zip"
CONTENT_DIGEST_NAME = "app.sha256"
CONTENT_SEED_NAME = "manifest.seed.json"
BOOTSTRAP_DIGEST_NAME = "bootstrap.sha256"
LIVE_MANIFEST_NAME = "manifest.json"
ERROR_REPORT_NAME = "bootstrap.error.json"
CONTENT_DIGEST_MARKER_NAME = ".content_digest"
APPLICATION_DIRECTORY = Path("/app")
EX_CONFIG = 78
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 65_535
SUPPORTED_PROTOCOL_VERSIONS = frozenset({"1"})
MANIFEST_BINDING_FIELDS = (
    "manifest_version",
    "protocol_version",
    "session_id",
    "owner_hash_version",
    "owner_hash",
    "app_hash",
    "sandbox_group_resource_id",
    "sandbox_id",
    "generation",
    "digest_kind",
    "digest",
    "state_store_fingerprint",
)
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMPILED_ABI_PATTERN = re.compile(
    r"\.cpython-(?P<abi>\d{2,3})-(?P<platform>[^/]+)\.so$"
)
_WHEEL_TAG_PATTERN = re.compile(r"^Tag:\s*(\S+)\s*$")
_MANYLINUX_PATTERN = re.compile(r"manylinux_(\d+)_(\d+)_")


@dataclass(frozen=True, slots=True)
class BootstrapContext:
    """The verified content and manifest available to a sandbox harness."""

    session_directory: Path
    journal_directory: Path
    application_directory: Path
    manifest: Mapping[str, object]
    capabilities: Mapping[str, str]


class BootstrapFailureError(Exception):
    """A bootstrap condition that must not publish readiness."""

    def __init__(self, code: str, message: str, *, permanent: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.permanent = permanent


BootstrapFailure = BootstrapFailureError


def main(argv: list[str] | None = None) -> int:
    """Prepare verified content and publish sandbox readiness."""

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--session-root",
        "--session-directory",
        dest="session_directory",
        type=Path,
        required=True,
    )
    parser.add_argument("--journal-root", type=Path)
    parser.add_argument("--application-directory", type=Path, default=APPLICATION_DIRECTORY)
    arguments = parser.parse_args(argv)
    try:
        context = prepare_sandbox(
            arguments.session_directory,
            journal_directory=arguments.journal_root,
            application_directory=arguments.application_directory,
        )
    except BootstrapFailure as exc:
        _withdraw_live_manifest(arguments.session_directory)
        _write_error_report(arguments.session_directory, exc)
        return EX_CONFIG if exc.permanent else 1
    except Exception:
        _withdraw_live_manifest(arguments.session_directory)
        _write_error_report(
            arguments.session_directory,
            BootstrapFailure("bootstrap_failure", "Sandbox bootstrap failed.", permanent=False),
        )
        return 1
    del context
    return 0


def prepare_sandbox(
    session_directory: Path,
    *,
    journal_directory: Path | None = None,
    application_directory: Path = APPLICATION_DIRECTORY,
    bootstrap_path: Path | None = None,
) -> BootstrapContext:
    """Verify, stage, and publish one content epoch for the sandbox harness."""

    session_directory = Path(session_directory)
    journal_directory = session_directory.parent if journal_directory is None else Path(journal_directory)
    application_directory = Path(application_directory)
    bootstrap_file = Path(__file__) if bootstrap_path is None else Path(bootstrap_path)
    seed = _read_seed(session_directory)
    _verify_bootstrap_digest(session_directory, bootstrap_file)
    archive_bytes = _verify_content_digest(session_directory, seed)
    _verify_archive_abi(archive_bytes)
    _stage_content(archive_bytes, str(seed["digest"]), application_directory)
    _configure_import_paths(application_directory)
    _verify_protocol(str(seed["protocol_version"]))
    os.environ[SANDBOX_MARKER_ENV_VAR] = "1"
    os.environ["AZURE_FUNCTIONS_AGENTS_SESSION_DIR"] = str(session_directory)
    capabilities = _freeze_harness_capabilities()
    _publish_protocol(journal_directory, str(seed["protocol_version"]), capabilities)
    manifest = _build_live_manifest(seed, capabilities)
    _publish_live_manifest(session_directory, manifest)
    return BootstrapContext(
        session_directory=session_directory,
        journal_directory=journal_directory,
        application_directory=application_directory,
        manifest=manifest,
        capabilities=capabilities,
    )


def _read_seed(session_directory: Path) -> dict[str, object]:
    content_directory = session_directory / CONTENT_DIRECTORY_NAME
    try:
        payload = (content_directory / CONTENT_SEED_NAME).read_bytes()
    except OSError:
        raise BootstrapFailure("seed_unavailable", "Sandbox content seed is unavailable.") from None
    decoded = _decode_json_object(payload, "seed")
    if set(decoded) != set(MANIFEST_BINDING_FIELDS):
        raise BootstrapFailure("seed_invalid", "Sandbox content seed is invalid.")
    for name in MANIFEST_BINDING_FIELDS:
        value = decoded[name]
        if name in {"manifest_version", "generation"}:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise BootstrapFailure("seed_invalid", "Sandbox content seed is invalid.")
        elif not isinstance(value, str) or not value:
            raise BootstrapFailure("seed_invalid", "Sandbox content seed is invalid.")
    return decoded


def _verify_bootstrap_digest(session_directory: Path, bootstrap_path: Path) -> None:
    try:
        expected = (session_directory / BOOTSTRAP_DIGEST_NAME).read_text(encoding="ascii")
        bootstrap_bytes = bootstrap_path.read_bytes()
    except (OSError, UnicodeError):
        raise BootstrapFailure(
            "bootstrap_integrity_failure",
            "Sandbox bootstrap integrity data is unavailable.",
        ) from None
    digest = _digest_text(bootstrap_bytes)
    if expected != f"{digest}\n":
        raise BootstrapFailure(
            "bootstrap_integrity_failure",
            "Sandbox bootstrap integrity verification failed.",
        )


def _verify_content_digest(session_directory: Path, seed: Mapping[str, object]) -> bytes:
    content_directory = session_directory / CONTENT_DIRECTORY_NAME
    try:
        archive = (content_directory / CONTENT_ARCHIVE_NAME).read_bytes()
        sidecar = (content_directory / CONTENT_DIGEST_NAME).read_text(encoding="ascii")
    except (OSError, UnicodeError):
        raise BootstrapFailure("content_unavailable", "Sandbox content is unavailable.") from None
    expected = str(seed["digest"])
    if _DIGEST_PATTERN.fullmatch(expected) is None:
        raise BootstrapFailure("seed_invalid", "Sandbox content seed is invalid.")
    if sidecar != f"{expected}\n" or _digest_text(archive) != expected:
        raise BootstrapFailure("content_digest_mismatch", "Sandbox content verification failed.")
    return archive


def _verify_archive_abi(archive_bytes: bytes) -> None:
    if len(archive_bytes) > MAX_ARCHIVE_BYTES:
        raise BootstrapFailure("archive_invalid", "Sandbox content archive is invalid.")
    try:
        with zipfile.ZipFile(_BytesReader(archive_bytes)) as archive:
            members = tuple(archive.infolist())
            _validate_archive_members(members)
            required_versions, manylinux_floor = _required_archive_abi(archive, members)
    except (OSError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile):
        raise BootstrapFailure("archive_invalid", "Sandbox content archive is invalid.") from None

    python_abi = f"{sys.version_info.major}{sys.version_info.minor}"
    if any(required != python_abi for required in required_versions):
        raise BootstrapFailure("python_abi_mismatch", "Sandbox Python ABI is incompatible.")
    platform_name = sysconfig.get_platform().replace("-", "_").casefold()
    if "x86_64" not in platform_name and "amd64" not in platform_name:
        raise BootstrapFailure("platform_abi_mismatch", "Sandbox platform is incompatible.")
    if manylinux_floor is not None and _live_glibc_version() < manylinux_floor:
        raise BootstrapFailure("glibc_abi_mismatch", "Sandbox glibc is incompatible.")


def _required_archive_abi(
    archive: zipfile.ZipFile,
    members: Iterable[zipfile.ZipInfo],
) -> tuple[set[str], tuple[int, int] | None]:
    required_versions: set[str] = set()
    manylinux_floor: tuple[int, int] | None = None
    for member in members:
        compiled = _COMPILED_ABI_PATTERN.search(member.filename)
        if compiled is not None:
            _validate_compiled_platform(compiled.group("platform"))
            required_versions.add(compiled.group("abi"))
        if not member.filename.endswith(".dist-info/WHEEL"):
            continue
        try:
            lines = archive.read(member).decode("utf-8").splitlines()
        except (KeyError, UnicodeDecodeError):
            raise BootstrapFailure("archive_invalid", "Sandbox content archive is invalid.") from None
        for line in lines:
            match = _WHEEL_TAG_PATTERN.match(line)
            if match is None:
                continue
            tag = match.group(1)
            interpreter, _, platform_tag = tag.split("-", 2)
            if interpreter.startswith("cp") and interpreter[2:].isdigit():
                required_versions.add(interpreter[2:])
            for platform_component in platform_tag.split("."):
                _validate_wheel_platform(platform_component)
                floor = _manylinux_floor(platform_component)
                if floor is not None and (manylinux_floor is None or floor > manylinux_floor):
                    manylinux_floor = floor
    return required_versions, manylinux_floor


def _validate_compiled_platform(platform_tag: str) -> None:
    normalized = platform_tag.replace("-", "_").casefold()
    if (
        "musl" in normalized
        or "linux" not in normalized
        or ("x86_64" not in normalized and "amd64" not in normalized)
    ):
        raise BootstrapFailure(
            "platform_abi_mismatch",
            "Sandbox platform is incompatible.",
        )


def _validate_wheel_platform(platform_tag: str) -> None:
    normalized = platform_tag.casefold()
    if normalized == "any":
        return
    if "musl" in normalized:
        raise BootstrapFailure(
            "platform_abi_mismatch",
            "Sandbox platform is incompatible.",
        )
    if (
        "linux" not in normalized
        or ("x86_64" not in normalized and "amd64" not in normalized)
    ):
        raise BootstrapFailure(
            "platform_abi_mismatch",
            "Sandbox platform is incompatible.",
        )


def _manylinux_floor(platform_tag: str) -> tuple[int, int] | None:
    match = _MANYLINUX_PATTERN.search(platform_tag)
    if match is not None:
        return int(match.group(1)), int(match.group(2))
    if "manylinux2014" in platform_tag:
        return 2, 17
    if "manylinux2010" in platform_tag:
        return 2, 12
    if "manylinux1" in platform_tag:
        return 2, 5
    return None


def _live_glibc_version() -> tuple[int, int]:
    confstr = getattr(os, "confstr", None)
    if confstr is None:
        raise BootstrapFailure("glibc_abi_mismatch", "Sandbox glibc is incompatible.")
    try:
        value = confstr("CS_GNU_LIBC_VERSION")
    except (AttributeError, OSError, ValueError):
        value = None
    if not value:
        raise BootstrapFailure("glibc_abi_mismatch", "Sandbox glibc is incompatible.")
    match = re.fullmatch(r"glibc\s+(\d+)\.(\d+)", value.strip())
    if match is None:
        raise BootstrapFailure("glibc_abi_mismatch", "Sandbox glibc is incompatible.")
    return int(match.group(1)), int(match.group(2))


def _validate_archive_members(members: Iterable[zipfile.ZipInfo]) -> None:
    seen: set[str] = set()
    total_uncompressed_bytes = 0
    for member in members:
        if member.filename in seen:
            raise BootstrapFailure("archive_path_invalid", "Sandbox content archive is invalid.")
        seen.add(member.filename)
        if len(seen) > MAX_ARCHIVE_MEMBERS:
            raise BootstrapFailure("archive_invalid", "Sandbox content archive is invalid.")
        total_uncompressed_bytes += member.file_size
        if total_uncompressed_bytes > MAX_ARCHIVE_BYTES:
            raise BootstrapFailure("archive_invalid", "Sandbox content archive is invalid.")
        path = Path(member.filename)
        if (
            not member.filename
            or member.filename.startswith(("/", "\\"))
            or "\\" in member.filename
            or any(part in {"", ".", ".."} for part in path.parts)
            or stat.S_ISLNK(member.external_attr >> 16)
        ):
            raise BootstrapFailure("archive_path_invalid", "Sandbox content archive is invalid.")


def _stage_content(archive_bytes: bytes, digest: str, application_directory: Path) -> None:
    marker = application_directory / CONTENT_DIGEST_MARKER_NAME
    if marker.is_file():
        try:
            if marker.read_text(encoding="ascii") == f"{digest}\n":
                return
        except OSError:
            pass
    if application_directory.exists():
        raise BootstrapFailure(
            "content_state_mismatch",
            "Sandbox application content does not match the session.",
        )

    staging_directory = application_directory.with_name(
        f"{application_directory.name}.staging.{digest.removeprefix('sha256:')}"
    )
    shutil.rmtree(staging_directory, ignore_errors=True)
    try:
        staging_directory.mkdir(parents=True)
        with zipfile.ZipFile(_BytesReader(archive_bytes)) as archive:
            members = tuple(archive.infolist())
            _validate_archive_members(members)
            for member in members:
                _extract_member(archive, member, staging_directory)
        marker_path = staging_directory / CONTENT_DIGEST_MARKER_NAME
        marker_path.write_text(f"{digest}\n", encoding="ascii")
        _fsync_file(marker_path)
        _fsync_directory(staging_directory)
        os.replace(staging_directory, application_directory)
        _fsync_directory(application_directory.parent)
    except BootstrapFailure:
        shutil.rmtree(staging_directory, ignore_errors=True)
        raise
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        shutil.rmtree(staging_directory, ignore_errors=True)
        raise BootstrapFailure(
            "archive_extract_failure", "Sandbox content extraction failed."
        ) from exc


def _extract_member(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    staging_directory: Path,
) -> None:
    destination = staging_directory.joinpath(*Path(member.filename).parts)
    if member.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(member) as source, destination.open("wb") as target:
        shutil.copyfileobj(source, target)
        target.flush()
        os.fsync(target.fileno())
    mode = member.external_attr >> 16
    if mode:
        os.chmod(destination, mode & 0o777)


def _configure_import_paths(application_directory: Path) -> None:
    site_packages = application_directory / ".python_packages" / "lib" / "site-packages"
    _reject_stdlib_shadowing(application_directory)
    site.addsitedir(str(application_directory))
    if site_packages.is_dir():
        site.addsitedir(str(site_packages))
    _move_application_paths_after_stdlib(application_directory, site_packages)
    import json as stdlib_json

    module_path = getattr(stdlib_json, "__file__", None)
    if module_path is not None and _is_within(Path(module_path), application_directory):
        raise BootstrapFailure("stdlib_shadowing", "Sandbox application shadows the standard library.")


def _reject_stdlib_shadowing(application_directory: Path) -> None:
    standard_library: frozenset[str] = sys.stdlib_module_names
    for child in application_directory.iterdir():
        if child.name == ".python_packages":
            continue
        module_name = child.stem if child.suffix == ".py" else child.name
        if module_name in standard_library:
            raise BootstrapFailure("stdlib_shadowing", "Sandbox application shadows the standard library.")


def _move_application_paths_after_stdlib(
    application_directory: Path,
    site_packages: Path,
) -> None:
    application_paths = {str(application_directory), str(site_packages)}
    movable = [path for path in sys.path if path in application_paths]
    if not movable:
        return
    sys.path[:] = [path for path in sys.path if path not in application_paths]
    stdlib_path = sysconfig.get_path("stdlib")
    try:
        insert_at = sys.path.index(stdlib_path) + 1 if stdlib_path else len(sys.path)
    except ValueError:
        insert_at = len(sys.path)
    sys.path[insert_at:insert_at] = movable


def _verify_protocol(protocol_version: str) -> None:
    if protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise BootstrapFailure("protocol_mismatch", "Sandbox protocol is unsupported.")


def _build_live_manifest(
    seed: Mapping[str, object],
    capabilities: Mapping[str, str],
) -> dict[str, object]:
    return {
        **{field: seed[field] for field in MANIFEST_BINDING_FIELDS},
        "abi": {
            "glibc": ".".join(str(component) for component in _live_glibc_version()),
            "platform": sysconfig.get_platform(),
            "python": f"cp{sys.version_info.major}{sys.version_info.minor}",
        },
        "capabilities": dict(capabilities),
        "harness": {"name": "maf"},
    }


def _freeze_harness_capabilities() -> Mapping[str, str]:
    capability_module = importlib.import_module(
        "azure_functions_agents.harness.sandbox_capabilities"
    )
    capability_module.register_sandbox_capabilities()
    harness_module = importlib.import_module("azure_functions_agents.harness")
    snapshot = harness_module.freeze_harness_capabilities()
    if not isinstance(snapshot, Mapping):
        raise BootstrapFailure("capability_mismatch", "Sandbox capabilities are invalid.")
    return {str(feature): str(capability) for feature, capability in snapshot.items()}


def _publish_protocol(
    journal_directory: Path,
    protocol_version: str,
    capabilities: Mapping[str, str],
) -> None:
    _publish_json(
        journal_directory / "protocol.json",
        {
            "protocol_version": protocol_version,
            "capabilities": dict(capabilities),
        },
        "protocol_publish_failure",
    )


def _publish_live_manifest(session_directory: Path, manifest: Mapping[str, object]) -> None:
    _publish_json(session_directory / LIVE_MANIFEST_NAME, manifest, "manifest_publish_failure")


def _publish_json(
    destination: Path,
    payload: Mapping[str, object],
    failure_code: str,
) -> None:
    temporary = destination.with_name(f"{destination.name}.tmp")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("wb") as output:
            output.write(f"{json.dumps(payload, sort_keys=True, separators=(',', ':'))}\n".encode())
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise BootstrapFailure(failure_code, "Sandbox artifact publication failed.") from None


def _write_error_report(session_directory: Path, failure: BootstrapFailure) -> None:
    payload = {
        "code": failure.code,
        "message": failure.message,
        "permanent": failure.permanent,
    }
    destination = session_directory / ERROR_REPORT_NAME
    temporary = destination.with_name(f"{destination.name}.tmp")
    try:
        session_directory.mkdir(parents=True, exist_ok=True)
        with temporary.open("wb") as output:
            output.write(f"{json.dumps(payload, sort_keys=True, separators=(',', ':'))}\n".encode())
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        _fsync_directory(session_directory)
    except OSError:
        temporary.unlink(missing_ok=True)


def _withdraw_live_manifest(session_directory: Path) -> None:
    try:
        (session_directory / LIVE_MANIFEST_NAME).unlink(missing_ok=True)
        _fsync_directory(session_directory)
    except OSError:
        return


def _decode_json_object(payload: bytes, kind: str) -> dict[str, object]:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        decoded = json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise BootstrapFailure(f"{kind}_invalid", "Sandbox content seed is invalid.") from None
    if not isinstance(decoded, dict):
        raise BootstrapFailure(f"{kind}_invalid", "Sandbox content seed is invalid.")
    return decoded


def _digest_text(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _fsync_file(path: Path) -> None:
    with path.open("rb") as source:
        try:
            os.fsync(source.fileno())
        except OSError:
            if os.name == "posix":
                raise


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        return
    finally:
        os.close(descriptor)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except OSError:
        return False
    except ValueError:
        return False
    return True


class _BytesReader:
    """A small seekable bytes reader accepted by :class:`zipfile.ZipFile`."""

    def __init__(self, value: bytes) -> None:
        import io

        self._buffer = io.BytesIO(value)

    def __enter__(self) -> _BytesReader:
        return self

    def __exit__(self, *arguments: object) -> None:
        self._buffer.close()

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._buffer.seek(offset, whence)

    def tell(self) -> int:
        return self._buffer.tell()

    def seekable(self) -> bool:
        return True
