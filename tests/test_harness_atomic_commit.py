from __future__ import annotations

import os

import pytest

from azure_functions_agents.harness.atomic_commit import (
    AtomicCommitCorruptError,
    AtomicCommitError,
    AtomicCommitStore,
)


def test_atomic_commit_recovers_only_the_pointer_selected_checkpoint(tmp_path) -> None:
    store = AtomicCommitStore(tmp_path)
    first = store.commit(conversation=b'{"turn":1}', working_files={"notes.txt": b"first"})
    second = store.commit(conversation=b'{"turn":2}', working_files={"notes.txt": b"second"})

    recovered = store.recover()

    assert recovered == second
    assert not first.path.exists()
    assert (recovered.path / "conversation.json").read_bytes() == b'{"turn":2}'
    assert (recovered.path / "notes.txt").read_bytes() == b"second"


def test_recovery_discards_unpointed_checkpoint_after_fault(tmp_path) -> None:
    def fault(stage: str) -> None:
        if stage == "after_checkpoint_rename":
            raise RuntimeError("simulated crash")

    store = AtomicCommitStore(tmp_path, fault_hook=fault)

    with pytest.raises(RuntimeError, match="simulated crash"):
        store.commit(conversation=b"state", working_files={})

    assert store.recover() is None
    assert not any((tmp_path / "checkpoints").iterdir())


def test_recovery_rejects_pointer_traversal(tmp_path) -> None:
    store = AtomicCommitStore(tmp_path)
    (tmp_path / "current").write_text("../outside\n", encoding="ascii")

    with pytest.raises(AtomicCommitCorruptError):
        store.recover()


def test_commit_rejects_working_file_traversal(tmp_path) -> None:
    store = AtomicCommitStore(tmp_path)

    with pytest.raises(AtomicCommitError):
        store.commit(conversation=b"state", working_files={"../escape": b"bad"})


@pytest.mark.skipif(os.name != "posix", reason="directory fsync is a POSIX durability primitive")
def test_commit_uses_directory_fsync_on_posix(tmp_path) -> None:
    calls: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        calls.append(descriptor)
        real_fsync(descriptor)

    store = AtomicCommitStore(tmp_path, fsync_hook=recording_fsync)
    store.commit(conversation=b"state", working_files={})

    assert len(calls) >= 4
