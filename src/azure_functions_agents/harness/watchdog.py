"""Stdlib-only watchdog records for sandbox harness process supervision."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

type WatchdogState = Literal["failed", "canceled", "timed_out"]

HEARTBEAT_INTERVAL_SECONDS = 30.0


class WatchdogTimeoutError(TimeoutError):
    """A harness run exceeded its authored deadline."""


@dataclass(frozen=True, slots=True)
class WatchdogTerminal:
    """A controller-readable terminal record written by supervised harness code."""

    state: WatchdogState
    code: str
    message: str

    def render(self) -> bytes:
        """Render a deterministic credential-free terminal document."""
        return (
            json.dumps(
                {
                    "code": self.code,
                    "message": self.message,
                    "state": self.state,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )


class Watchdog:
    """Atomically publish heartbeat, process-group, and supervised terminal records."""

    def __init__(
        self,
        run_directory: Path,
        *,
        clock: Callable[[], float] = time.monotonic,
        heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")
        self._run_directory = run_directory
        self._clock = clock
        self._heartbeat_interval_seconds = heartbeat_interval_seconds

    @property
    def heartbeat_path(self) -> Path:
        """Return the stable heartbeat path."""
        return self._run_directory / "heartbeat.json"

    @property
    def process_path(self) -> Path:
        """Return the stable process metadata path."""
        return self._run_directory / "process.json"

    @property
    def terminal_path(self) -> Path:
        """Return the harness-written terminal outcome path."""
        return self._run_directory / "terminal.json"

    def write_heartbeat(self) -> None:
        """Atomically refresh a heartbeat using a controller-independent monotonic value."""
        self._atomic_write(
            self.heartbeat_path,
            json.dumps({"monotonic": self._clock()}, separators=(",", ":")).encode("utf-8") + b"\n",
        )

    def write_process_group(self, process_group_id: int) -> None:
        """Persist the process group used by controller cancellation/liveness checks."""
        if isinstance(process_group_id, bool) or process_group_id <= 0:
            raise ValueError("process_group_id must be positive")
        self._atomic_write(
            self.process_path,
            json.dumps({"process_group_id": process_group_id}, separators=(",", ":")).encode("utf-8")
            + b"\n",
        )

    def write_terminal(self, terminal: WatchdogTerminal) -> None:
        """Atomically publish a supervised terminal state."""
        self._atomic_write(self.terminal_path, terminal.render())

    async def supervise[T](
        self,
        operation: Awaitable[T],
        *,
        timeout_seconds: float,
    ) -> T:
        """Run one awaitable under heartbeat and authored deadline supervision."""
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        try:
            async with asyncio.timeout(timeout_seconds):
                return await operation
        except TimeoutError:
            terminal = WatchdogTerminal(
                state="timed_out",
                code="run_timed_out",
                message="Run exceeded its authored deadline.",
            )
            self.write_terminal(terminal)
            raise WatchdogTimeoutError(terminal.message) from None
        except asyncio.CancelledError:
            self.write_terminal(
                WatchdogTerminal(
                    state="canceled",
                    code="run_canceled",
                    message="Run was canceled by the controller.",
                )
            )
            raise
        except MemoryError:
            terminal = WatchdogTerminal(
                state="failed",
                code="out_of_memory",
                message="Run failed due to memory exhaustion.",
            )
            self.write_terminal(terminal)
            raise
        except OSError as exc:
            if exc.errno != errno.ENOSPC:
                raise
            terminal = WatchdogTerminal(
                state="failed",
                code="disk_full",
                message="Run failed because sandbox storage was full.",
            )
            self.write_terminal(terminal)
            raise
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

    def record_child_exit(self, exit_code: int) -> WatchdogTerminal | None:
        """Map observed supervised child OOM/disk exits to a durable failure record."""
        if exit_code in {137, -9}:
            terminal = WatchdogTerminal(
                state="failed",
                code="child_out_of_memory",
                message="Supervised child exited due to memory exhaustion.",
            )
        elif exit_code in {122, 28}:
            terminal = WatchdogTerminal(
                state="failed",
                code="child_disk_full",
                message="Supervised child exited because sandbox storage was full.",
            )
        else:
            return None
        self.write_terminal(terminal)
        return terminal

    async def _heartbeat_loop(self) -> None:
        while True:
            self.write_heartbeat()
            await asyncio.sleep(self._heartbeat_interval_seconds)

    def _atomic_write(self, path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with temporary.open("wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
