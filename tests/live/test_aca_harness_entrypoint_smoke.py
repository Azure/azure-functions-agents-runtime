"""Opt-in live coverage for the ACA harness module entrypoint."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import platform
import shlex
import subprocess
import sys
import tempfile
import uuid
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio
from tests.aca_smoke_diagnostics import (
    AcaSmokeEnvironmentError,
    classify_aca_smoke_exception,
)

from azure_functions_agents.execution.run_control import _JOURNAL_ENTRYPOINT
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
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_CLOSURE_ARCHIVE_MAX_BYTES = 80 * 1024 * 1024
_CREATE_TIMEOUT_SECONDS = 90.0
_COMMAND_TIMEOUT_SECONDS = 60.0
_CI_OWNER_HASH = "o1-" + ("c" * 52)
_CI_APP_HASH = "a1-" + ("d" * 52)

if os.environ.get("AZURE_FUNCTIONS_AGENTS_RUN_ACA_SMOKE") != "1":
    pytest.skip(
        "Set AZURE_FUNCTIONS_AGENTS_RUN_ACA_SMOKE=1 after human authorization to run live ACA.",
        allow_module_level=True,
    )


@dataclass(frozen=True, slots=True)
class _AcaSmokeConfig:
    group_resource_id: str
    disk: str
    region: str


@dataclass(frozen=True, slots=True)
class _DependencyClosureArchive:
    payload: bytes
    entry_count: int


def _require_sandbox_compatible_host() -> None:
    machine = platform.machine().casefold()
    python_version = sys.version_info[:2]
    if (
        sys.platform != "linux"
        or machine not in {"x86_64", "amd64"}
        or sys.implementation.name != "cpython"
        or python_version not in {(3, 13), (3, 14)}
    ):
        raise AcaSmokeEnvironmentError(
            "dependency closure must be built on Linux x86_64 CPython 3.13 or 3.14 "
            "to match the Linux sandbox ABI; building on "
            f"{sys.platform}/{machine or 'unknown'} with "
            f"{sys.implementation.name} {python_version[0]}.{python_version[1]} "
            "would deliver incompatible binaries."
        )


def _required_environment_value(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise AcaSmokeEnvironmentError(f"{name} must be set to a non-blank value.")
    return value.strip()


@pytest.fixture
def aca_smoke_config() -> _AcaSmokeConfig:
    _require_sandbox_compatible_host()
    return _AcaSmokeConfig(
        group_resource_id=_required_environment_value(
            "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID"
        ),
        disk=_required_environment_value("AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_DISK"),
        region=_required_environment_value("AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_REGION").casefold(),
    )


def _build_dependency_closure(temporary_directory: Path) -> _DependencyClosureArchive:
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
            str(_REPOSITORY_ROOT),
        ],
        check=False,
        cwd=_REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"local dependency closure build exited with code {result.returncode}.")

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
    return _DependencyClosureArchive(payload=archive.getvalue(), entry_count=entry_count)


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
) -> None:
    for sandbox in await adapter.list_sandboxes(labels=labels):
        await _force_delete_by_id(adapter, sandbox.sandbox_id)


async def _cleanup_sandbox(
    *,
    adapter: AcaSandboxAdapter | None,
    handle: SandboxSessionHandle | None,
    sandbox_id: str | None,
    labels: dict[str, str],
) -> None:
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

    if not deleted:
        required_label_keys = {"owner_kind", "owner_hash", "app_hash", "session_id"}
        if required_label_keys.issubset(labels):
            try:
                await _delete_labelled_sandboxes(adapter, labels)
                deleted = True
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

    if not deleted:
        cleanup_errors.append(RuntimeError("ACA smoke sandbox deletion could not be confirmed."))
    if cleanup_errors:
        message = "; ".join(str(error) for error in cleanup_errors)
        raise RuntimeError(f"ACA smoke cleanup failed: {message}")


@pytest_asyncio.fixture
async def aca_harness_smoke_handle(
    aca_smoke_config: _AcaSmokeConfig,
) -> AsyncIterator[SandboxSessionHandle]:
    adapter: AcaSandboxAdapter | None = None
    handle: SandboxSessionHandle | None = None
    sandbox_id: str | None = None
    labels: dict[str, str] = {}

    try:
        with tempfile.TemporaryDirectory(prefix=".aca-smoke-", dir=_REPOSITORY_ROOT) as temporary:
            closure = await asyncio.to_thread(_build_dependency_closure, Path(temporary))
        archive_size = len(closure.payload)
        _LOGGER.info(
            "ACA smoke dependency closure archive: %d bytes (%.1f MiB), %d ZIP entries.",
            archive_size,
            archive_size / (1024 * 1024),
            closure.entry_count,
        )
        # 80 MiB is the largest incompressible single write verified against ACA; keep a hard
        # budget at that measured-safe point rather than using compression for extra headroom.
        _enforce_archive_budget(archive_size)

        group_binding = SandboxGroupBinding.create(
            resource_id=aca_smoke_config.group_resource_id,
            region=aca_smoke_config.region,
        )
        adapter = await AcaSandboxAdapter.open(
            aca_smoke_config.group_resource_id,
            persisted_group=group_binding,
        )
        session_id = f"aca-harness-smoke-{uuid.uuid4().hex}"
        labels_model = SandboxProvisioningLabels.create(
            owner_hash_version="o1",
            owner_kind="aca_smoke_ci",
            owner_hash=_CI_OWNER_HASH,
            app_hash=_CI_APP_HASH,
            session_id=session_id,
        )
        labels = labels_model.to_provider_labels()
        sandbox_root = f"/tmp/{session_id}"
        closure_archive_path = f"{sandbox_root}/dependency-closure.zip"
        closure_directory = f"{sandbox_root}/site-packages"
        request = SandboxCreateRequest.create(
            source=DiskSource.create(aca_smoke_config.disk),
            labels=labels_model,
            remaining_setup_budget_seconds=30.0,
            environment={
                "AZURE_FUNCTIONS_AGENTS_SANDBOX": "1",
                "PYTHONPATH": closure_directory,
            },
        )
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
    except Exception as error:
        setup_error = _setup_error("ACA smoke setup failed", error)
        try:
            await _cleanup_sandbox(
                adapter=adapter,
                handle=handle,
                sandbox_id=sandbox_id,
                labels=labels,
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
            await _cleanup_sandbox(
                adapter=adapter,
                handle=handle,
                sandbox_id=sandbox_id,
                labels=labels,
            )
        except Exception as error:
            raise _setup_error("ACA smoke cleanup failed", error) from error


@pytest.mark.live_aca
@pytest.mark.asyncio
async def test_live_aca_harness_entrypoint_smoke(
    aca_harness_smoke_handle: SandboxSessionHandle,
) -> None:
    command = _JOURNAL_ENTRYPOINT.removeprefix("setsid nohup ")
    result = await aca_harness_smoke_handle.exec(f"{command} --help", timeout_seconds=60)
    assert result.exit_code == 0, result.stderr
