"""Shared support for explicitly enabled ACA smoke coverage."""

from __future__ import annotations

import asyncio
import hashlib
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
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from tests.aca_smoke_diagnostics import (
    AcaSmokeEnvironmentError,
    classify_aca_smoke_exception,
    is_aca_authorization_failure,
)

from azure_functions_agents.controller.bootstrap_delivery import deliver_content_and_bootstrap
from azure_functions_agents.controller.package import (
    FUNCS_ZIP_DIGEST_KIND,
    CapturedContentPackage,
    get_content_package,
    read_live_manifest_binding,
)
from azure_functions_agents.egress.policy import compile_egress_policy
from azure_functions_agents.journal_paths import (
    JOURNAL_ROOT_PATH,
    SANDBOX_APPLICATION_PATH,
    SESSION_PATH,
)
from azure_functions_agents.transport.aca_sdk import AcaSandboxAdapter
from azure_functions_agents.transport.manifest import ExpectedSandboxManifestBinding
from azure_functions_agents.transport.ports import SandboxSessionHandle
from azure_functions_agents.transport.transport_models import (
    DiskSource,
    SandboxCreateRequest,
    SandboxEgressPolicy,
    SandboxGroupBinding,
    SandboxLifecyclePolicy,
    SandboxProvisioningLabels,
)

_LOGGER = logging.getLogger(__name__)
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_CLOSURE_ARCHIVE_MAX_BYTES = 80 * 1024 * 1024
_MAX_STANDARD_ZIP_ENTRIES = 0xFFFF
_MAX_STANDARD_ZIP_ENTRY_SIZE = 2_000_000_000
_ARCHIVE_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_UNIX_CREATOR_SYSTEM = 3
_ZIP_SPEC_VERSION = 20
_STANDARD_FILE_UNIX_MODE = 0o100644 << 16
_DIRECTORY_UNIX_MODE = 0o40755 << 16
_DELIVERED_SITE_PACKAGES_PREFIX = ".python_packages/lib/site-packages/"
_CREATE_TIMEOUT_SECONDS = 90.0
_COMMAND_TIMEOUT_SECONDS = 60.0
CI_OWNER_KIND = "aca_smoke_ci"
CI_OWNER_HASH = "o1-" + ("c" * 52)
CI_APP_HASH = "a1-" + ("d" * 52)
ACA_SMOKE_RUN_ID_ENV_VAR = "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_RUN_ID"
_MAX_RUN_ID_LENGTH = 16
_JOURNAL_ROOT_PROBE_CONTENT = b"aca-smoke-journal-root"
_LABEL_RECONCILIATION_ATTEMPTS = 3
_LABEL_RECONCILIATION_DELAY_SECONDS = 1.0
_PIP_OUTPUT_TAIL_MAX_CHARS = 4000
_DISK_PYTHON_VERSION_PATTERN = re.compile(r"^python-(\d+)\.(\d+)$")
_REASONING_EFFORTS = frozenset({"none", "low", "medium", "high", "xhigh"})
_LIVE_MODEL_AGENT_PROJECT_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "live_aca_model_turn"
_SMOKE_STATE_STORE_FINGERPRINT = "s1-" + ("e" * 52)
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(?P<name>api[_-]?key|bearer|token|client[_-]?secret|"
    r"password|secret|sig)\b(?P<separator>\s*[:=]\s*)(?P<value>[^\s,;]+)"
)
_AUTHORIZATION_HEADER_PATTERN = re.compile(r"(?i)\bauthorization\s*:\s*[^\r\n]+")
_BEARER_TOKEN_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


@dataclass(frozen=True, slots=True)
class AcaSmokeConfig:
    """Environment-specific inputs needed to create one ACA smoke sandbox."""

    group_resource_id: str
    disk: str


@dataclass(frozen=True, slots=True)
class AcaSmokeModelConfig:
    """Credential-free Azure OpenAI inputs forwarded only to the sandbox guest."""

    provider: str
    endpoint: str
    deployment: str
    managed_identity_client_id: str
    reasoning_effort: str | None

    def sandbox_environment(self) -> dict[str, str]:
        """Return the narrow model configuration available to the sandbox process."""

        environment = {
            "AZURE_FUNCTIONS_AGENTS_PROVIDER": self.provider,
            "AZURE_OPENAI_ENDPOINT": self.endpoint,
            "AZURE_OPENAI_DEPLOYMENT": self.deployment,
            "AZURE_CLIENT_ID": self.managed_identity_client_id,
        }
        if self.reasoning_effort is not None:
            environment["AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT"] = self.reasoning_effort
        return environment

    def sandbox_egress_policy(self) -> SandboxEgressPolicy:
        """Allow only the configured model host in addition to hard control-plane denies."""

        return compile_egress_policy(
            web_request_allowed_hosts=[],
            model_endpoint=self.endpoint,
        )


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


def aca_smoke_run_id() -> str:
    """Return a per-run token shared by the smoke test process and its reaper.

    In CI both read ``$(Build.BuildId)`` from the environment and agree; locally
    the variable is unset and each caller falls back to a distinct random token,
    so a reaper only ever deletes sandboxes minted by its own run.
    """

    raw = os.environ.get(ACA_SMOKE_RUN_ID_ENV_VAR, "")
    sanitized = re.sub(r"[^a-z0-9-]", "", raw.lower()).strip("-")[:_MAX_RUN_ID_LENGTH]
    return sanitized or uuid.uuid4().hex[:12]


def session_belongs_to_run(labels: Mapping[str, str], run_id: str) -> bool:
    """Return whether a sandbox's session label was minted by the given run."""

    return labels.get("session_id", "").startswith(f"{run_id}-")


@dataclass(frozen=True, slots=True)
class _ArchiveMember:
    """One normalized ZIP member used to compose captured smoke-test content."""

    name: str
    payload: bytes
    external_attr: int


def aca_smoke_config_from_environment() -> AcaSmokeConfig:
    """Read host-safe ACA smoke configuration from the operator environment."""

    group_resource_id = _required_environment_value(
        "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID"
    )
    disk = _required_environment_value("AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_DISK")
    require_sandbox_compatible_host(disk)
    return AcaSmokeConfig(group_resource_id=group_resource_id, disk=disk)


def aca_smoke_model_config_from_environment() -> AcaSmokeModelConfig:
    """Read and validate the credential-free model inputs for the real-turn smoke."""

    provider = _required_environment_value(
        "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_MODEL_PROVIDER"
    ).casefold()
    if provider != "azure_openai":
        raise AcaSmokeEnvironmentError(
            "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_MODEL_PROVIDER must be azure_openai."
        )
    endpoint = _required_https_endpoint(
        "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_AZURE_OPENAI_ENDPOINT"
    )
    deployment = _required_environment_value(
        "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_AZURE_OPENAI_DEPLOYMENT"
    )
    managed_identity_client_id = _required_managed_identity_client_id(
        "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_MODEL_UAMI_CLIENT_ID"
    )
    reasoning_effort = _optional_reasoning_effort(
        "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_REASONING_EFFORT"
    )
    return AcaSmokeModelConfig(
        provider=provider,
        endpoint=endpoint,
        deployment=deployment,
        managed_identity_client_id=managed_identity_client_id,
        reasoning_effort=reasoning_effort,
    )


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


def _required_https_endpoint(name: str) -> str:
    value = _required_environment_value(name)
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AcaSmokeEnvironmentError(f"{name} must be an HTTPS endpoint without credentials.")
    return value


def _required_managed_identity_client_id(name: str) -> str:
    value = _required_environment_value(name)
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise AcaSmokeEnvironmentError(f"{name} must be a managed identity client ID.") from None


def _optional_reasoning_effort(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    effort = value.strip().casefold()
    if effort not in _REASONING_EFFORTS:
        allowed = ", ".join(sorted(_REASONING_EFFORTS))
        raise AcaSmokeEnvironmentError(f"{name} must be one of: {allowed}.")
    return effort


def redact_aca_smoke_evidence(text: str) -> str:
    """Remove credential-shaped values before surfacing sandbox setup diagnostics."""

    redacted = _AUTHORIZATION_HEADER_PATTERN.sub("Authorization: [redacted]", text)
    redacted = _SECRET_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group('name')}{match.group('separator')}[redacted]",
        redacted,
    )
    return _BEARER_TOKEN_PATTERN.sub("Bearer [redacted]", redacted)


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


def compose_real_agent_project_package(
    agent_project: CapturedContentPackage,
    dependency_closure: DependencyClosureArchive,
) -> CapturedContentPackage:
    """Embed the already-built sandbox closure in a captured agent application.

    The sandbox's initial process still uses the separately extracted closure for
    its pre-content verification. The captured copy is deliberately separate:
    production runs bootstrap with ``-E -S``, so that process cannot rely on the
    sandbox-level ``PYTHONPATH``.
    """

    members = _read_archive_members(agent_project.archive_bytes, source="agent project")
    closure_members = _read_archive_members(dependency_closure.payload, source="dependency closure")
    names = {member.name for member in members}
    for member in closure_members:
        if member.name.endswith("/"):
            continue
        delivered_name = f"{_DELIVERED_SITE_PACKAGES_PREFIX}{member.name}"
        if delivered_name in names:
            raise AcaSmokeEnvironmentError(
                "captured agent project has a duplicate agent or dependency path."
            )
        names.add(delivered_name)
        members.append(
            _ArchiveMember(
                name=delivered_name,
                payload=member.payload,
                external_attr=_STANDARD_FILE_UNIX_MODE,
            )
        )
    if len(members) > _MAX_STANDARD_ZIP_ENTRIES:
        raise AcaSmokeEnvironmentError("captured agent project exceeds the standard ZIP entry cap.")

    members.sort(key=lambda member: member.name)
    for member in members:
        if len(member.payload) > _MAX_STANDARD_ZIP_ENTRY_SIZE:
            raise AcaSmokeEnvironmentError(
                "captured agent project exceeds the standard ZIP entry size cap."
            )

    archive = io.BytesIO()
    try:
        with zipfile.ZipFile(
            archive,
            mode="w",
            compression=zipfile.ZIP_STORED,
            allowZip64=False,
        ) as output:
            for member in members:
                entry = zipfile.ZipInfo(member.name, date_time=_ARCHIVE_TIMESTAMP)
                entry.compress_type = zipfile.ZIP_STORED
                entry.create_system = _UNIX_CREATOR_SYSTEM
                entry.create_version = _ZIP_SPEC_VERSION
                entry.extract_version = _ZIP_SPEC_VERSION
                entry.external_attr = member.external_attr
                output.writestr(entry, member.payload)
    except (OSError, ValueError, zipfile.LargeZipFile) as error:
        raise AcaSmokeEnvironmentError(
            "captured agent project could not be represented as a standard ZIP archive."
        ) from error

    archive_bytes = archive.getvalue()
    _enforce_archive_budget(len(archive_bytes))
    return CapturedContentPackage.create(
        archive_bytes=archive_bytes,
        digest_kind=FUNCS_ZIP_DIGEST_KIND,
        digest=f"sha256:{hashlib.sha256(archive_bytes).hexdigest()}",
    )


def _read_archive_members(archive_bytes: bytes, *, source: str) -> list[_ArchiveMember]:
    """Read a ZIP's members while retaining only deterministic archive metadata."""

    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            if len(archive.infolist()) > _MAX_STANDARD_ZIP_ENTRIES:
                raise AcaSmokeEnvironmentError(f"{source} exceeds the standard ZIP entry cap.")
            members: list[_ArchiveMember] = []
            names: set[str] = set()
            for entry in archive.infolist():
                _validate_composite_member_name(entry.filename, source=source)
                if entry.filename in names:
                    raise AcaSmokeEnvironmentError(
                        f"{source} has duplicate archive member paths."
                    )
                names.add(entry.filename)
                if entry.file_size > _MAX_STANDARD_ZIP_ENTRY_SIZE:
                    raise AcaSmokeEnvironmentError(
                        f"{source} exceeds the standard ZIP entry size cap."
                    )
                payload = b"" if entry.is_dir() else archive.read(entry)
                members.append(
                    _ArchiveMember(
                        name=entry.filename,
                        payload=payload,
                        external_attr=(
                            _DIRECTORY_UNIX_MODE if entry.is_dir() else entry.external_attr
                        )
                        or _STANDARD_FILE_UNIX_MODE,
                    )
                )
    except (OSError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise AcaSmokeEnvironmentError(f"{source} is not a readable standard ZIP archive.") from error

    return members


def _validate_composite_member_name(name: str, *, source: str) -> None:
    relative_name = name.removesuffix("/")
    if (
        not relative_name
        or name.startswith("/")
        or any(part in {"", ".", ".."} for part in relative_name.split("/"))
    ):
        raise AcaSmokeEnvironmentError(f"{source} has an unsafe archive member path.")


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


def _error_reason(error: BaseException) -> str:
    """Render a non-empty cause, falling back to the exception type name."""

    text = str(error).strip()
    if isinstance(error, AcaSmokeEnvironmentError):
        text = text.removeprefix("ACA-SMOKE-ENV: ").strip()
    return text or type(error).__name__


def _setup_error(context: str, error: Exception) -> AcaSmokeEnvironmentError:
    reason = _error_reason(error)
    if isinstance(error, AcaSmokeEnvironmentError):
        return AcaSmokeEnvironmentError(reason)
    reason = reason.removeprefix(f"{context}: ")
    if classify_aca_smoke_exception(error) != "environment":
        context = f"{context}: unexpected setup failure"
    return AcaSmokeEnvironmentError(f"{context}: {reason}")


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
        evidence = redact_aca_smoke_evidence(stderr or stdout)
        raise AcaSmokeEnvironmentError(f"{description}: {evidence}")


def _require_successful_model_preflight(*, description: str, exit_code: int) -> None:
    """Fail setup without preserving guest output that could contain turn content."""

    if exit_code != 0:
        raise AcaSmokeEnvironmentError(f"{description}; guest diagnostics were redacted.")


async def prepare_real_agent_project(
    handle: SandboxSessionHandle,
    *,
    config: AcaSmokeConfig,
    dependency_closure: DependencyClosureArchive,
) -> None:
    """Deliver, bootstrap, and preflight the captured no-tools model-turn project."""

    agent_project = await get_content_package(_LIVE_MODEL_AGENT_PROJECT_ROOT)
    package = await asyncio.to_thread(
        compose_real_agent_project_package,
        agent_project,
        dependency_closure,
    )
    session_id = f"aca-real-agent-{uuid.uuid4().hex}"
    expected = ExpectedSandboxManifestBinding.create(
        manifest_version=1,
        protocol_version="1",
        session_id=session_id,
        owner_hash_version="o1",
        owner_hash=_CI_OWNER_HASH,
        app_hash=_CI_APP_HASH,
        sandbox_group_resource_id=config.group_resource_id,
        sandbox_id=handle.identity.sandbox_id,
        generation=1,
        digest_kind=package.digest_kind,
        digest=package.digest,
        state_store_fingerprint=_SMOKE_STATE_STORE_FINGERPRINT,
    )
    await deliver_content_and_bootstrap(handle, package, expected, handle.identity)
    bootstrap = await handle.exec(
        " ".join(
            shlex.quote(part)
            for part in (
                "python3",
                "-E",
                "-S",
                f"{SESSION_PATH}/bootstrap.py",
                "--session-root",
                SESSION_PATH,
                "--journal-root",
                JOURNAL_ROOT_PATH,
                "--application-directory",
                SANDBOX_APPLICATION_PATH,
            )
        ),
        timeout_seconds=_COMMAND_TIMEOUT_SECONDS,
    )
    _require_successful_setup_command(
        description="captured agent project bootstrap failed",
        exit_code=bootstrap.exit_code,
        stdout=bootstrap.stdout,
        stderr=bootstrap.stderr,
    )
    await read_live_manifest_binding(handle, expected, handle.identity)
    await _preflight_sandbox_model_access(handle)


async def _preflight_sandbox_model_access(handle: SandboxSessionHandle) -> None:
    """Classify guest credential, role, quota, and deployment reachability as setup."""

    source = "\n".join(
        (
            "import asyncio",
            "import os",
            "from azure.identity.aio import ManagedIdentityCredential",
            "from azure_functions_agents.runner import run_agent",
            f"os.environ['AZURE_FUNCTIONS_AGENTS_APP_ROOT'] = {SANDBOX_APPLICATION_PATH!r}",
            f"os.environ['AZURE_FUNCTIONS_AGENTS_SESSION_DIR'] = {SESSION_PATH!r}",
            "async def main():",
            "    credential = ManagedIdentityCredential(client_id=os.environ['AZURE_CLIENT_ID'])",
            "    try:",
            "        await credential.get_token('https://cognitiveservices.azure.com/.default')",
            "    finally:",
            "        await credential.close()",
            "    result = await run_agent(",
            "        'Reply with a short acknowledgement.',",
            "        instructions='Return a short acknowledgement and use no tools.',",
            "        timeout=90.0,",
            "        tools=[],",
            "        mcp_tools=[],",
            "        skill_paths=[],",
            "        web_request_tools=[],",
            "        session_id='aca-model-preflight',",
            "    )",
            "    if not result.content.strip():",
            "        raise RuntimeError('Model preflight returned no content.')",
            "asyncio.run(main())",
        )
    )
    result = await handle.exec(_python_command(source), timeout_seconds=120.0)
    _require_successful_model_preflight(
        description="sandbox managed identity could not reach the configured Azure OpenAI deployment",
        exit_code=result.exit_code,
    )


async def _force_delete_by_id(adapter: AcaSandboxAdapter, sandbox_id: str) -> None:
    poller = await adapter._group_client.begin_delete_sandbox(sandbox_id)
    await poller.result()


async def _delete_labelled_sandboxes(
    adapter: AcaSandboxAdapter,
    labels: dict[str, str],
) -> int:
    for attempt in range(_LABEL_RECONCILIATION_ATTEMPTS):
        try:
            sandboxes = await adapter.list_sandboxes(labels=labels)
        except Exception as error:
            # The SDK already retried this 403 for minutes; looping here would multiply it.
            if is_aca_authorization_failure(error):
                raise AcaSmokeEnvironmentError(
                    "ACA smoke cleanup cannot list sandboxes: data-plane authorization "
                    "was denied. A 403 is permanent, so reconciliation stops here rather "
                    "than repeating the SDK's own retries."
                ) from error
            raise
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
    before_yield_with_closure: (
        Callable[[SandboxSessionHandle, DependencyClosureArchive], Awaitable[None]] | None
    ) = None,
    model_config: AcaSmokeModelConfig | None = None,
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
        session_id = f"{aca_smoke_run_id()}-{session_prefix}-{uuid.uuid4().hex[:16]}"
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
        environment = {
            "AZURE_FUNCTIONS_AGENTS_SANDBOX": "1",
            "PYTHONPATH": closure_directory,
        }
        egress_policy: SandboxEgressPolicy | None = None
        if model_config is not None:
            environment.update(model_config.sandbox_environment())
            egress_policy = model_config.sandbox_egress_policy()
        request = SandboxCreateRequest.create(
            source=DiskSource.create(config.disk),
            labels=labels_model,
            remaining_setup_budget_seconds=30.0,
            environment=environment,
            egress_policy=egress_policy,
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
        if before_yield_with_closure is not None:
            await before_yield_with_closure(handle, closure)
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
