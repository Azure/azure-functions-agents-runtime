"""Canonical sandbox journal paths."""

from __future__ import annotations

from uuid import UUID

SANDBOX_APPLICATION_PATH = "/app"
SANDBOX_SITE_PACKAGES_PATH = f"{SANDBOX_APPLICATION_PATH}/.python_packages/lib/site-packages"
SANDBOX_PYTHONPATH = f"{SANDBOX_APPLICATION_PATH}:{SANDBOX_SITE_PACKAGES_PATH}"
SANDBOX_ROOT_PATH = "/var/lib/azurefunctions-agents-runtime"
JOURNAL_ROOT_PATH = SANDBOX_ROOT_PATH
SESSION_PATH = f"{SANDBOX_ROOT_PATH}/session"
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
# Marker the harness writes into LAUNCH_STDERR_FILENAME only from its
# pre-acceptance journal-failure handler, before it journals acceptance. The
# controller matches on it to promote an otherwise indeterminate launch to a
# determinate failure. A healthy run never reaches that handler, so it never
# emits this marker.
LAUNCH_DIAGNOSTIC_PREFIX = "azfn-agents-harness-launch-error: "
# Separate, non-promoting marker for controller-driven cancellation, which can
# arrive after acceptance. Kept distinct from LAUNCH_DIAGNOSTIC_PREFIX so a
# canceled but otherwise healthy run is never mistaken for a failed launch.
HARNESS_CANCEL_DIAGNOSTIC_PREFIX = "azfn-agents-harness-canceled: "


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
