"""Shared support for explicitly enabled ACA smoke coverage."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import platform
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from tests.aca_smoke_diagnostics import (
    AcaSmokeEnvironmentError,
    classify_aca_smoke_exception,
)

from azure_functions_agents.journal_paths import JOURNAL_ROOT_PATH
from azure_functions_agents.transport.aca_sdk import AcaSandboxAdapter
from azure_functions_agents.transport.ports import SandboxSessionHandle
from azure_functions_agents.transport.transport_models import (
    DiskSource,
    SandboxCreateRequest,
    SandboxGroupBinding,
    SandboxLifecyclePolicy,
    SandboxProvisioningLabels,
)

_LOGGER = logging.getLogger(__name__)
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_CLOSURE_ARCHIVE_MAX_BYTES = 80 * 1024 * 1024
_CREATE_TIMEOUT_SECONDS = 90.0
_COMMAND_TIMEOUT_SECONDS = 60.0
CI_OWNER_KIND = "aca_smoke_ci"
CI_OWNER_HASH = "o1-" + ("c" * 52)
CI_APP_HASH = "a1-" + ("d" * 52)
_JOURNAL_ROOT_PROBE_CONTENT = b"aca-smoke-journal-root"
_LABEL_RECONCILIATION_ATTEMPTS = 3
_LABEL_RECONCILIATION_DELAY_SECONDS = 1.0
_PIP_OUTPUT_TAIL_MAX_CHARS = 4000
_DISK_PYTHON_VERSION_PATTERN = re.compile(r"^python-(\d+)\.(\d+)$")


@dataclass(frozen=True, slots=True)
class AcaSmokeConfig:
    """Environment-specific inputs needed to create one ACA smoke sandbox."""

    group_resource_id: str
    disk: str


@dataclass(frozen=True, slots=True)
class DependencyClosureArchive:
    """Deterministic local package closure supplied to a sandbox."""

    payload: bytes
    entry_count: int


def ci_smoke_reaper_labels() -> dict[str, str]:
    """Return the label selector that scopes CI smoke sandboxes for reaping."""

    return {
        "owner_kind": CI_OWNER_KIND,
        "owner_hash": CI_OWNER_HASH,
        "app_hash": CI_APP_HASH,
    }


def aca_smoke_config_from_environment() -> AcaSmokeConfig:
    """Read host-safe ACA smoke configuration from the operator environment."""

    group_resource_id = _required_environment_value(
        "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID"
    )
    disk = _required_environment_value("AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_DISK")
    require_sandbox_compatible_host(disk)
    return AcaSmokeConfig(group_resource_id=group_resource_id, disk=disk)


def require_sandbox_compatible_host(disk: str) -> None:
    """Reject closure builds whose compiled wheels cannot import in the sandbox.

    The closure is built with the host interpreter, so its compiled wheels carry
    the host ABI tag; the host CPython minor must equal the target disk's minor.
    """

    machine = platform.machine().casefold()
    if (
        sys.platform != "linux"
        or machine not in {"x86_64", "amd64"}
        or sys.implementation.name != "cpython"
    ):
        raise AcaSmokeEnvironmentError(
            "dependency closure must be built on Linux x86_64 CPython to match the "
            f"Linux sandbox ABI; building on {sys.platform}/{machine or 'unknown'} with "
            f"{sys.implementation.name} would deliver incompatible binaries."
        )

    host_version = sys.version_info[:2]
    target_version = _target_disk_python_version(disk)
    if target_version is None:
        if host_version not in {(3, 13), (3, 14)}:
            raise AcaSmokeEnvironmentError(
                "dependency closure must be built on CPython 3.13 or 3.14 to match a "
                "supported Linux sandbox ABI; this host is CPython "
                f"{host_version[0]}.{host_version[1]}."
            )
        return
    if host_version != target_version:
        raise AcaSmokeEnvironmentError(
            "dependency closure must be built on CPython "
            f"{target_version[0]}.{target_version[1]} to match the {disk!r} sandbox disk; "
            f"this host is CPython {host_version[0]}.{host_version[1]}, whose compiled "
            f"wheels are tagged cp{host_version[0]}{host_version[1]} and will not import "
            f"on the sandbox interpreter. Run the smoke on CPython "
            f"{target_version[0]}.{target_version[1]} or target the matching "
            f"python-{host_version[0]}.{host_version[1]} disk."
        )


def _target_disk_python_version(disk: str) -> tuple[int, int] | None:
    """Return the (major, minor) CPython version a ``python-X.Y`` disk boots."""

    match = _DISK_PYTHON_VERSION_PATTERN.fullmatch(disk.strip().casefold())
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)))


def _required_environment_value(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise AcaSmokeEnvironmentError(f"{name} must be set to a non-blank value.")
    return value.strip()


def build_dependency_closure(temporary_directory: Path) -> DependencyClosureArchive:
    """Build the exact local dependency closure consumed by the sandbox Python."""

    target_directory = temporary_directory / "site-packages"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-compile",
            "--target",
            str(target_directory),
            str(REPOSITORY_ROOT),
        ],
        check=False,
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        # Generous ceiling so a slow WSL mount does not spuriously time out while still
        # bounding a genuine hang.
        timeout=900,
    )
    if result.returncode != 0:
        raise RuntimeError(_format_pip_failure(result.returncode, result.stdout, result.stderr))

    archive = io.BytesIO()
    entry_count = 0
    with zipfile.ZipFile(archive, mode="w", compression=zipfile.ZIP_STORED) as output:
        for path in sorted(
            target_directory.rglob("*"),
            key=lambda candidate: candidate.relative_to(target_directory).as_posix(),
        ):
            if not path.is_file():
                continue
            entry = zipfile.ZipInfo(
                path.relative_to(target_directory).as_posix(),
                date_time=(1980, 1, 1, 0, 0, 0),
            )
            entry.compress_type = zipfile.ZIP_STORED
            entry.external_attr = 0o100644 << 16
            output.writestr(entry, path.read_bytes())
            entry_count += 1
    return DependencyClosureArchive(payload=archive.getvalue(), entry_count=entry_count)


def _format_pip_failure(returncode: int, stdout: str, stderr: str) -> str:
    """Build a diagnosable error from a failed local closure-build pip run.

    This pip runs on the operator's own machine, so its output is trusted local
    output and is surfaced in full-tail, unlike untrusted in-sandbox stderr.
    """

    sections = [f"local dependency closure build exited with code {returncode}."]
    stderr_tail = _pip_output_tail(stderr)
    sections.append(f"pip stderr:\n{stderr_tail}" if stderr_tail else "pip stderr was empty.")
    stdout_tail = _pip_output_tail(stdout)
    if stdout_tail:
        sections.append(f"pip stdout (tail):\n{stdout_tail}")
    return "\n".join(sections)


def _pip_output_tail(text: str) -> str:
    stripped = text.strip()
    if len(stripped) <= _PIP_OUTPUT_TAIL_MAX_CHARS:
        return stripped
    return (
        f"[truncated to last {_PIP_OUTPUT_TAIL_MAX_CHARS} chars]\n"
        f"{stripped[-_PIP_OUTPUT_TAIL_MAX_CHARS:]}"
    )


def _setup_error(context: str, error: Exception) -> AcaSmokeEnvironmentError:
    if isinstance(error, AcaSmokeEnvironmentError):
        return AcaSmokeEnvironmentError(str(error).removeprefix("ACA-SMOKE-ENV: "))
    bucket = classify_aca_smoke_exception(error)
    if bucket == "environment":
        return AcaSmokeEnvironmentError(f"{context}: {error}")
    return AcaSmokeEnvironmentError(f"{context}: unexpected setup failure: {error}")


def _python_command(source: str) -> str:
    return f"python -c {shlex.quote(source)}"


def _enforce_archive_budget(archive_size: int) -> None:
    if archive_size > _CLOSURE_ARCHIVE_MAX_BYTES:
        raise AcaSmokeEnvironmentError(
            "dependency closure archive is "
            f"{archive_size} bytes, above the {_CLOSURE_ARCHIVE_MAX_BYTES}-byte budget."
        )


def _require_successful_setup_command(
    *,
    description: str,
    exit_code: int,
    stdout: str,
    stderr: str,
) -> None:
    if exit_code != 0:
        raise AcaSmokeEnvironmentError(f"{description}: {stderr or stdout}")


async def _force_delete_by_id(adapter: AcaSandboxAdapter, sandbox_id: str) -> None:
    poller = await adapter._group_client.begin_delete_sandbox(sandbox_id)
    await poller.result()


async def _delete_labelled_sandboxes(
    adapter: AcaSandboxAdapter,
    labels: dict[str, str],
) -> int:
    for attempt in range(_LABEL_RECONCILIATION_ATTEMPTS):
        sandboxes = await adapter.list_sandboxes(labels=labels)
        if sandboxes:
            break
        if attempt + 1 < _LABEL_RECONCILIATION_ATTEMPTS:
            await asyncio.sleep(_LABEL_RECONCILIATION_DELAY_SECONDS)
    else:
        return 0
    for sandbox in sandboxes:
        await _force_delete_by_id(adapter, sandbox.sandbox_id)
    return len(sandboxes)


async def cleanup_sandbox(
    *,
    adapter: AcaSandboxAdapter | None,
    handle: SandboxSessionHandle | None,
    sandbox_id: str | None,
    labels: dict[str, str],
    creation_attempted: bool,
) -> None:
    """Delete one smoke sandbox through every available ownership path."""

    if adapter is None:
        return

    deleted = False
    cleanup_errors: list[Exception] = []
    if handle is not None:
        try:
            await handle.delete()
            deleted = True
        except Exception as error:
            _LOGGER.warning("ACA smoke handle deletion failed; reconciling by sandbox ID.", exc_info=error)
        try:
            await handle.close()
        except Exception as error:
            if deleted:
                _LOGGER.warning("ACA smoke handle close failed after deletion.", exc_info=error)
            else:
                cleanup_errors.append(error)

    if not deleted and sandbox_id is not None:
        try:
            await _force_delete_by_id(adapter, sandbox_id)
            deleted = True
        except Exception as error:
            _LOGGER.warning("ACA smoke ID deletion failed; reconciling by labels.", exc_info=error)

    if not deleted and creation_attempted:
        required_label_keys = {"owner_kind", "owner_hash", "app_hash", "session_id"}
        if required_label_keys.issubset(labels):
            try:
                deleted = await _delete_labelled_sandboxes(adapter, labels) > 0
                if not deleted:
                    cleanup_errors.append(
                        RuntimeError("ACA smoke label cleanup did not find a created sandbox.")
                    )
            except Exception as error:
                cleanup_errors.append(error)
        else:
            cleanup_errors.append(
                RuntimeError("ACA smoke cleanup had no complete CI label selector.")
            )

    try:
        await adapter.close()
    except Exception as error:
        if deleted:
            _LOGGER.warning("ACA smoke adapter close failed after deletion.", exc_info=error)
        else:
            cleanup_errors.append(error)

    if creation_attempted and not deleted:
        cleanup_errors.append(RuntimeError("ACA smoke sandbox deletion could not be confirmed."))
    if cleanup_errors:
        message = "; ".join(str(error) for error in cleanup_errors)
        raise RuntimeError(f"ACA smoke cleanup failed: {message}")


async def _cleanup_sandbox_shielded(
    *,
    adapter: AcaSandboxAdapter | None,
    handle: SandboxSessionHandle | None,
    sandbox_id: str | None,
    labels: dict[str, str],
    creation_attempted: bool,
) -> None:
    """Finish paid-resource cleanup even when the calling task is cancelled."""

    cleanup_task = asyncio.create_task(
        cleanup_sandbox(
            adapter=adapter,
            handle=handle,
            sandbox_id=sandbox_id,
            labels=labels,
            creation_attempted=creation_attempted,
        )
    )
    cancellation_received = False
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            cancellation_received = True
    cleanup_task.result()
    if cancellation_received:
        raise asyncio.CancelledError


async def prepare_journal_root(handle: SandboxSessionHandle) -> None:
    """Create and verify a writable journal root before the run-control test."""

    probe_path = f"{JOURNAL_ROOT_PATH}/.aca-smoke-journal-probe-{uuid.uuid4().hex}"
    try:
        await handle.write_file(probe_path, _JOURNAL_ROOT_PROBE_CONTENT, create_dirs=True)
        _require_journal_root_probe_content(await handle.read_file(probe_path))
        await handle.delete_file(probe_path)
    except Exception as error:
        raise AcaSmokeEnvironmentError("Sandbox journal root could not be prepared.") from error


def _require_journal_root_probe_content(content: bytes) -> None:
    if content != _JOURNAL_ROOT_PROBE_CONTENT:
        raise RuntimeError("Sandbox journal root probe content did not round-trip.")


@asynccontextmanager
async def provision_aca_smoke_sandbox(
    config: AcaSmokeConfig,
    *,
    session_prefix: str,
    before_yield: Callable[[SandboxSessionHandle], Awaitable[None]] | None = None,
) -> AsyncIterator[SandboxSessionHandle]:
    """Provision, verify, and always delete one fully prepared smoke sandbox."""

    adapter: AcaSandboxAdapter | None = None
    handle: SandboxSessionHandle | None = None
    sandbox_id: str | None = None
    labels: dict[str, str] = {}
    creation_attempted = False

    try:
        with tempfile.TemporaryDirectory(prefix=".aca-smoke-", dir=REPOSITORY_ROOT) as temporary:
            closure = await asyncio.to_thread(build_dependency_closure, Path(temporary))
        archive_size = len(closure.payload)
        _LOGGER.info(
            "ACA smoke dependency closure archive: %d bytes (%.1f MiB), %d ZIP entries.",
            archive_size,
            archive_size / (1024 * 1024),
            closure.entry_count,
        )
        # 80 MiB is the largest incompressible single write verified against ACA.
        _enforce_archive_budget(archive_size)

        adapter = await AcaSandboxAdapter.open(config.group_resource_id)
        group_binding = SandboxGroupBinding.create(
            resource_id=adapter.group.resource_id,
            region=adapter.group.region,
        )
        session_id = f"{session_prefix}-{uuid.uuid4().hex}"
        labels_model = SandboxProvisioningLabels.create(
            owner_hash_version="o1",
            owner_kind=CI_OWNER_KIND,
            owner_hash=CI_OWNER_HASH,
            app_hash=CI_APP_HASH,
            session_id=session_id,
        )
        labels = labels_model.to_provider_labels()
        sandbox_root = f"/tmp/{session_id}"
        closure_archive_path = f"{sandbox_root}/dependency-closure.zip"
        closure_directory = f"{sandbox_root}/site-packages"
        request = SandboxCreateRequest.create(
            source=DiskSource.create(config.disk),
            labels=labels_model,
            remaining_setup_budget_seconds=30.0,
            environment={
                "AZURE_FUNCTIONS_AGENTS_SANDBOX": "1",
                "PYTHONPATH": closure_directory,
            },
        )
        creation_attempted = True
        async with asyncio.timeout(_CREATE_TIMEOUT_SECONDS):
            handle = await adapter.create(request, persisted_group=group_binding)
        sandbox_id = handle.identity.sandbox_id

        await handle.set_lifecycle_policy(
            SandboxLifecyclePolicy.create(
                auto_suspend_seconds=60,
                auto_delete_seconds=3_960,
            )
        )
        await handle.write_file(closure_archive_path, closure.payload, create_dirs=True)

        extraction = await handle.exec(
            _python_command(
                f"import zipfile; zipfile.ZipFile({closure_archive_path!r}).extractall("
                f"{closure_directory!r})"
            ),
            timeout_seconds=_COMMAND_TIMEOUT_SECONDS,
        )
        _require_successful_setup_command(
            description="dependency closure extraction failed",
            exit_code=extraction.exit_code,
            stdout=extraction.stdout,
            stderr=extraction.stderr,
        )

        agent_framework_import = await handle.exec(
            'python -c "import agent_framework"',
            timeout_seconds=_COMMAND_TIMEOUT_SECONDS,
        )
        _require_successful_setup_command(
            description="dependency closure verification could not import agent_framework",
            exit_code=agent_framework_import.exit_code,
            stdout=agent_framework_import.stdout,
            stderr=agent_framework_import.stderr,
        )
        runtime_import = await handle.exec(
            'python -c "import azure_functions_agents"',
            timeout_seconds=_COMMAND_TIMEOUT_SECONDS,
        )
        _require_successful_setup_command(
            description="dependency closure verification could not import azure_functions_agents",
            exit_code=runtime_import.exit_code,
            stdout=runtime_import.stdout,
            stderr=runtime_import.stderr,
        )
        if before_yield is not None:
            await before_yield(handle)
    except asyncio.CancelledError:
        await _cleanup_sandbox_shielded(
            adapter=adapter,
            handle=handle,
            sandbox_id=sandbox_id,
            labels=labels,
            creation_attempted=creation_attempted,
        )
        raise
    except Exception as error:
        setup_error = _setup_error("ACA smoke setup failed", error)
        try:
            await _cleanup_sandbox_shielded(
                adapter=adapter,
                handle=handle,
                sandbox_id=sandbox_id,
                labels=labels,
                creation_attempted=creation_attempted,
            )
        except Exception as cleanup_error:
            cleanup_detail = str(
                _setup_error("ACA smoke cleanup failed", cleanup_error)
            ).removeprefix("ACA-SMOKE-ENV: ")
            raise AcaSmokeEnvironmentError(
                f"{str(setup_error).removeprefix('ACA-SMOKE-ENV: ')}; "
                f"cleanup could not be confirmed: {cleanup_detail}"
            ) from cleanup_error
        raise setup_error from error

    if handle is None:
        raise AcaSmokeEnvironmentError("ACA smoke setup completed without a sandbox handle.")

    try:
        yield handle
    finally:
        try:
            await _cleanup_sandbox_shielded(
                adapter=adapter,
                handle=handle,
                sandbox_id=sandbox_id,
                labels=labels,
                creation_attempted=creation_attempted,
            )
        except Exception as error:
            raise _setup_error("ACA smoke cleanup failed", error) from error
