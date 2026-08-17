"""Canonical sandbox journal paths."""

from __future__ import annotations

from uuid import UUID

SANDBOX_APPLICATION_PATH = "/app"
SANDBOX_SITE_PACKAGES_PATH = f"{SANDBOX_APPLICATION_PATH}/.python_packages/lib/site-packages"
SANDBOX_PYTHONPATH = f"{SANDBOX_APPLICATION_PATH}:{SANDBOX_SITE_PACKAGES_PATH}"
SANDBOX_ROOT_PATH = "/var/lib/azurefunctions-agents-runtime"
JOURNAL_ROOT_PATH = SANDBOX_ROOT_PATH
SESSION_PATH = f"{SANDBOX_ROOT_PATH}/session"
CHECKPOINTS_PATH = f"{SESSION_PATH}/checkpoints"
CONTENT_PATH = f"{SESSION_PATH}/content"
CONTENT_ARCHIVE_PATH = f"{CONTENT_PATH}/app.zip"
CONTENT_DIGEST_SIDECAR_PATH = f"{CONTENT_PATH}/app.sha256"
CONTENT_MANIFEST_SEED_PATH = f"{CONTENT_PATH}/manifest.seed.json"
SESSION_MANIFEST_PATH = f"{SESSION_PATH}/manifest.json"
BOOTSTRAP_PATH = f"{SESSION_PATH}/bootstrap.py"
BOOTSTRAP_DIGEST_PATH = f"{SESSION_PATH}/bootstrap.sha256"
BOOT_READY_PATH = f"{SESSION_PATH}/.boot-ready"
BOOTSTRAP_ERROR_PATH = f"{SESSION_PATH}/bootstrap.error.json"
INBOX_PATH = f"{JOURNAL_ROOT_PATH}/inbox"
RUNS_PATH = f"{JOURNAL_ROOT_PATH}/runs"
HARNESS_PROTOCOL_PATH = f"{JOURNAL_ROOT_PATH}/protocol.json"
ATOMIC_CHECKPOINT_POINTER_PATH = f"{SESSION_PATH}/current"
CHECKPOINT_NAME_PREFIX = "checkpoint_"
HEARTBEAT_FILENAME = "heartbeat.json"
PROCESS_FILENAME = "process.json"
LAUNCH_STDERR_FILENAME = "launch.stderr"
# Prefix on the only launch-sidecar lines the controller may log verbatim: repo-authored,
# secret-free markers. Everything else in that sidecar is untrusted sandbox output.
LAUNCH_DIAGNOSTIC_PREFIX = "azfn-agents-harness-launch-error: "
# Closed set of repo-authored diagnostics the harness may emit after LAUNCH_DIAGNOSTIC_PREFIX.
# A marker line is logged verbatim only when its stripped remainder is an exact member; the
# prefix alone is not an authorship check, so a forged marker carrying a secret is not one of these.
ALLOWED_LAUNCH_DIAGNOSTICS: frozenset[str] = frozenset(
    {
        "harness canceled.",
        "Sandbox run identifier is invalid.",
        "Sandbox run inbox is missing.",
        "Sandbox run inbox does not match the requested run.",
        "Sandbox run requested an unknown agent.",
        "Sandbox runner emitted an invalid event.",
        "Sandbox runner emitted an invalid completion event.",
        "Sandbox run locking requires POSIX flock.",
        "Sandbox run lock could not be acquired.",
        "Sandbox run inbox exceeds its size limit.",
        "Sandbox run inbox is invalid.",
        "Sandbox run inbox has no agent name.",
        "Sandbox run inbox timeout is invalid.",
        "Sandbox process group is invalid.",
        "Sandbox journal event type is invalid.",
        "Sandbox journal event exceeds its size limit.",
        "Sandbox run result exceeds its size limit.",
        "Successful runs must publish a result.",
        "Existing sandbox run status is invalid.",
        "Existing sandbox events are invalid.",
        "Existing sandbox events are not contiguous.",
        "Sandbox journal events are invalid.",
    }
)


def inbox_path(run_id: str) -> str:
    """Return one run inbox path."""
    return f"{INBOX_PATH}/{run_id}.json"


def run_path(run_id: str) -> str:
    """Return one run directory path."""
    return f"{RUNS_PATH}/{run_id}"


def status_path(run_id: str) -> str:
    """Return one run status path."""
    return f"{run_path(run_id)}/status.json"


def result_path(run_id: str) -> str:
    """Return one run result path."""
    return f"{run_path(run_id)}/result.json"


def process_path(run_id: str) -> str:
    """Return one run process-journal path."""
    return f"{run_path(run_id)}/{PROCESS_FILENAME}"


def heartbeat_path(run_id: str) -> str:
    """Return one run heartbeat path."""
    return f"{run_path(run_id)}/{HEARTBEAT_FILENAME}"


def launch_stderr_path(run_id: str) -> str:
    """Return one run launch-stderr diagnostic sidecar path."""
    return f"{run_path(run_id)}/{LAUNCH_STDERR_FILENAME}"


def checkpoint_name(token: str) -> str:
    """Return the canonical UUID-backed checkpoint name."""
    try:
        parsed = UUID(token)
    except (AttributeError, ValueError) as exc:
        raise ValueError("checkpoint token must be a UUID") from exc
    if parsed.version != 4 or parsed.hex != token:
        raise ValueError("checkpoint token must be canonical UUID4 hex")
    return f"{CHECKPOINT_NAME_PREFIX}{token}"


def validate_checkpoint_name(value: str) -> str:
    """Validate one canonical UUID-backed checkpoint name."""
    if not isinstance(value, str) or not value.startswith(CHECKPOINT_NAME_PREFIX):
        raise ValueError("checkpoint name must use the canonical prefix")
    token = value.removeprefix(CHECKPOINT_NAME_PREFIX)
    if checkpoint_name(token) != value:
        raise ValueError("checkpoint name must be canonical")
    return value


def checkpoint_conversation_path(checkpoint: str) -> str:
    """Return the canonical conversation path for one validated checkpoint."""
    return f"{CHECKPOINTS_PATH}/{validate_checkpoint_name(checkpoint)}/conversation.json"
