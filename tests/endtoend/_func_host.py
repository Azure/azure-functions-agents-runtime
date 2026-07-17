"""Helpers for the sample end-to-end tests.

The single behavior these helpers support today is: launch ``func start`` for a
sample Function App, confirm the host started and indexed its functions without
errors, then shut the host back down. Additional E2E assertions (invoking
triggers, checking responses, etc.) can build on top of :func:`start_and_verify`
later.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# Substrings (matched case-insensitively) that indicate the host finished
# starting up and the Python worker successfully initialized.
READY_MARKERS: tuple[str, ...] = (
    "worker process started and initialized",
    "host started",
    "application started. press ctrl+c to shut down",
)

# Substrings (matched case-insensitively) that indicate the host failed to come
# up cleanly. "No job functions found" is treated as a failure because every
# sample is expected to expose at least one trigger or built-in endpoint.
FAILURE_MARKERS: tuple[str, ...] = (
    "worker failed to index functions",
    "failed to index functions",
    "no job functions found",
    "a host error has occurred",
    "traceback (most recent call last)",
    "exception has been thrown",
    "unhandled exception",
)


@dataclass
class FuncStartResult:
    """Outcome of a single ``func start`` attempt."""

    started: bool
    reason: str
    output: str


def ensure_local_settings(app_dir: Path) -> None:
    """Make sure a ``local.settings.json`` exists so ``func start`` can run.

    Prefers the sample's committed ``local.settings.template.json`` (empty
    provider values are fine because agent clients are constructed lazily at
    execution time, not at index time). Falls back to a minimal settings file
    that just points storage at Azurite.
    """
    settings = app_dir / "local.settings.json"
    if settings.exists():
        return

    template = app_dir / "local.settings.template.json"
    if template.exists():
        shutil.copyfile(template, settings)
        return

    settings.write_text(
        json.dumps(
            {
                "IsEncrypted": False,
                "Values": {
                    "FUNCTIONS_WORKER_RUNTIME": "python",
                    "AzureWebJobsStorage": "UseDevelopmentStorage=true",
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _terminate(proc: subprocess.Popen[str]) -> None:
    """Stop ``func`` and its worker child process(es)."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass

    try:
        proc.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)


def start_and_verify(
    app_dir: Path,
    *,
    timeout: float = 150.0,
    ready_grace: float = 5.0,
    func_path: str | None = None,
) -> FuncStartResult:
    """Run ``func start`` in ``app_dir`` and verify a clean startup.

    Returns a :class:`FuncStartResult`. ``started`` is ``True`` only when a
    readiness marker was observed and no failure marker appeared within a short
    grace window afterwards. The host is always shut down before returning.
    """
    ensure_local_settings(app_dir)

    func_exe = func_path or shutil.which("func")
    if func_exe is None:
        return FuncStartResult(False, "`func` executable not found on PATH", "")

    creationflags = 0
    preexec = None
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        preexec = os.setsid

    proc = subprocess.Popen(
        [func_exe, "start"],
        cwd=str(app_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        creationflags=creationflags,
        preexec_fn=preexec,  # type: ignore[arg-type]
    )

    lines: list[str] = []
    line_queue: queue.Queue[str | None] = queue.Queue()

    def _reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line_queue.put(line)
        line_queue.put(None)

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    deadline = time.monotonic() + timeout
    ready = False
    failed = False
    ready_deadline: float | None = None
    reason = f"timed out after {timeout:.0f}s waiting for the host to start"

    try:
        while True:
            now = time.monotonic()
            if now > deadline:
                break
            if ready and ready_deadline is not None and now > ready_deadline:
                reason = "host started"
                break

            try:
                item = line_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if item is None:
                # Output stream closed => the process exited on its own.
                reason = "host started" if ready else "func exited before the host started"
                break

            lines.append(item)
            lowered = item.lower()

            if any(marker in lowered for marker in FAILURE_MARKERS):
                failed = True
                reason = f"detected failure marker: {item.strip()}"
                break

            if not ready and any(marker in lowered for marker in READY_MARKERS):
                ready = True
                ready_deadline = time.monotonic() + ready_grace
    finally:
        _terminate(proc)
        reader.join(timeout=5)

    # Drain anything the reader queued between the last read and shutdown so the
    # captured output is complete for diagnostics.
    while True:
        try:
            item = line_queue.get_nowait()
        except queue.Empty:
            break
        if item is not None:
            lines.append(item)

    return FuncStartResult(started=ready and not failed, reason=reason, output="".join(lines))
