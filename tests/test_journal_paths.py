from __future__ import annotations

from pathlib import PurePosixPath
from uuid import uuid4

import pytest

from azure_functions_agents.journal_paths import (
    ATOMIC_CHECKPOINT_POINTER_PATH,
    CHECKPOINT_NAME_PREFIX,
    CONTENT_ARCHIVE_PATH,
    CONTENT_DIGEST_SIDECAR_PATH,
    CONTENT_MANIFEST_SEED_PATH,
    CONTENT_PATH,
    HARNESS_PROTOCOL_PATH,
    HEARTBEAT_FILENAME,
    INBOX_PATH,
    JOURNAL_ROOT_PATH,
    PROCESS_FILENAME,
    RUNS_PATH,
    SANDBOX_ROOT_PATH,
    SESSION_MANIFEST_PATH,
    SESSION_PATH,
    checkpoint_name,
    heartbeat_path,
    inbox_path,
    process_path,
    result_path,
    run_path,
    status_path,
    validate_checkpoint_name,
)


def test_journal_paths_share_one_canonical_root() -> None:
    assert PurePosixPath(SANDBOX_ROOT_PATH).parts == (
        "/",
        "var",
        "lib",
        "azurefunctions-agents-runtime",
    )
    assert JOURNAL_ROOT_PATH == SANDBOX_ROOT_PATH
    assert f"{JOURNAL_ROOT_PATH}/inbox" == INBOX_PATH
    assert f"{JOURNAL_ROOT_PATH}/runs" == RUNS_PATH
    assert f"{JOURNAL_ROOT_PATH}/protocol.json" == HARNESS_PROTOCOL_PATH
    assert f"{SESSION_PATH}/current" == ATOMIC_CHECKPOINT_POINTER_PATH
    assert f"{SESSION_PATH}/content" == CONTENT_PATH
    assert f"{CONTENT_PATH}/app.zip" == CONTENT_ARCHIVE_PATH
    assert f"{CONTENT_PATH}/app.sha256" == CONTENT_DIGEST_SIDECAR_PATH
    assert f"{CONTENT_PATH}/manifest.seed.json" == CONTENT_MANIFEST_SEED_PATH
    assert f"{SESSION_PATH}/manifest.json" == SESSION_MANIFEST_PATH
    assert run_path("run-1") == f"{RUNS_PATH}/run-1"
    assert inbox_path("run-1") == f"{INBOX_PATH}/run-1.json"
    assert status_path("run-1") == f"{RUNS_PATH}/run-1/status.json"
    assert result_path("run-1") == f"{RUNS_PATH}/run-1/result.json"
    assert process_path("run-1") == f"{RUNS_PATH}/run-1/process.json"
    assert heartbeat_path("run-1") == f"{RUNS_PATH}/run-1/heartbeat.json"
    assert PROCESS_FILENAME == "process.json"
    assert HEARTBEAT_FILENAME == "heartbeat.json"


def test_checkpoint_names_are_canonical_uuid4_values() -> None:
    token = uuid4().hex
    name = checkpoint_name(token)

    assert name == f"{CHECKPOINT_NAME_PREFIX}{token}"
    assert validate_checkpoint_name(name) == name


@pytest.mark.parametrize(
    "value",
    [
        "checkpoint-not-a-uuid",
        "checkpoint-00000000-0000-0000-0000-000000000000",
        "checkpoint-../outside",
        "../checkpoint-" + uuid4().hex,
        "checkpoint-" + str(uuid4()),
    ],
)
def test_checkpoint_names_reject_noncanonical_or_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError):
        validate_checkpoint_name(value)
