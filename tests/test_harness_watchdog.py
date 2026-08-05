from __future__ import annotations

import asyncio
import errno
import json

import pytest

from azure_functions_agents.harness.watchdog import Watchdog, WatchdogTimeoutError


def test_watchdog_atomically_writes_heartbeat_and_process_metadata(tmp_path) -> None:
    watchdog = Watchdog(tmp_path)

    watchdog.write_heartbeat()
    watchdog.write_process_group(42)

    assert "monotonic" in json.loads(watchdog.heartbeat_path.read_text(encoding="utf-8"))
    assert json.loads(watchdog.process_path.read_text(encoding="utf-8")) == {"process_group_id": 42}


@pytest.mark.asyncio
async def test_watchdog_writes_timeout_terminal_record(tmp_path) -> None:
    watchdog = Watchdog(tmp_path, heartbeat_interval_seconds=0.001)

    with pytest.raises(WatchdogTimeoutError):
        await watchdog.supervise(asyncio.sleep(1), timeout_seconds=0.001)

    terminal = json.loads(watchdog.terminal_path.read_text(encoding="utf-8"))
    assert terminal["state"] == "timed_out"


@pytest.mark.asyncio
async def test_watchdog_records_memory_and_disk_failures(tmp_path) -> None:
    async def memory_failure() -> None:
        raise MemoryError

    async def disk_failure() -> None:
        raise OSError(errno.ENOSPC, "full")

    watchdog = Watchdog(tmp_path)
    with pytest.raises(MemoryError):
        await watchdog.supervise(memory_failure(), timeout_seconds=1)
    assert json.loads(watchdog.terminal_path.read_text(encoding="utf-8"))["code"] == "out_of_memory"

    with pytest.raises(OSError):
        await watchdog.supervise(disk_failure(), timeout_seconds=1)
    assert json.loads(watchdog.terminal_path.read_text(encoding="utf-8"))["code"] == "disk_full"


def test_watchdog_maps_supervised_oom_and_disk_exit_codes(tmp_path) -> None:
    watchdog = Watchdog(tmp_path)

    assert watchdog.record_child_exit(137) is not None
    assert watchdog.record_child_exit(28) is not None
    assert watchdog.record_child_exit(0) is None
