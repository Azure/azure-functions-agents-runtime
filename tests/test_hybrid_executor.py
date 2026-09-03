from __future__ import annotations

import json
import subprocess
import sys
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from azure_functions_agents.controller import package as controller_package
from azure_functions_agents.experimental import hybrid_executor
from azure_functions_agents.experimental.hybrid_protocol import (
    HYBRID_TOOL_MANIFEST_FILENAME,
    HYBRID_TOOL_PID_FILENAME,
    HYBRID_TOOL_READINESS_FILENAME,
    HYBRID_TOOL_REQUEST_DIRECTORY,
    HYBRID_TOOL_RESULT_DIRECTORY,
    HYBRID_TOOL_SHUTDOWN_FILENAME,
    HybridToolInvocationResult,
    parse_hybrid_tool_manifest,
    parse_hybrid_tool_result,
)

_EXECUTOR_SOURCE = Path(hybrid_executor.__file__).resolve()


def _write_archive(path: Path, files: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            archive.writestr(info, content)


def _wait_for(path: Path, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path.name}")


class _RunningExecutor:
    def __init__(self, process: subprocess.Popen[str], journal: Path) -> None:
        self.process = process
        self.journal = journal

    def invoke(
        self,
        call_id: str,
        tool_name: str,
        arguments: dict[str, object],
        *,
        deadline_seconds: float = 5.0,
    ) -> tuple[bytes, HybridToolInvocationResult]:
        request = {
            "arguments": arguments,
            "call_id": call_id,
            "deadline_unix_seconds": time.time() + deadline_seconds,
            "operation_id": "test-operation",
            "protocol_version": "1",
            "tool_name": tool_name,
            "traceparent": None,
        }
        request_path = self.journal / HYBRID_TOOL_REQUEST_DIRECTORY / f"{call_id}.json"
        request_path.write_text(
            json.dumps(request, allow_nan=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        result_path = self.journal / HYBRID_TOOL_RESULT_DIRECTORY / f"{call_id}.json"
        _wait_for(result_path)
        payload = result_path.read_bytes()
        return payload, parse_hybrid_tool_result(payload)

    def stop(self) -> None:
        (self.journal / HYBRID_TOOL_SHUTDOWN_FILENAME).touch()
        self.process.wait(timeout=10)
        assert self.process.returncode == 0, self.process.stderr.read()


@pytest.fixture
def running_executor(tmp_path: Path) -> Iterator[_RunningExecutor]:
    archive = tmp_path / "app.zip"
    import_marker = tmp_path / "customer-import.txt"
    _write_archive(
        archive,
        {
            "tools/async_tool.py": (
                "import asyncio\n"
                "async def async_echo(text: str) -> dict[str, str]:\n"
                '    """Echo asynchronously."""\n'
                "    await asyncio.sleep(0)\n"
                "    return {'text': text}\n"
            ),
            "tools/failure.py": (
                "def fail_tool() -> None:\n"
                '    """Fail deliberately."""\n'
                "    print('before failure')\n"
                "    raise ValueError('deliberate')\n"
            ),
            "tools/imported.py": (
                "from pathlib import Path\n"
                f"_marker = Path({str(import_marker)!r})\n"
                "_count = int(_marker.read_text().split(':')[-1]) + 1 if _marker.exists() else 1\n"
                "_marker.write_text(f\"{__import__('os').getpid()}:{_count}\")\n"
                "def pid_tool() -> int:\n"
                '    """Return the importing process."""\n'
                "    return __import__('os').getpid()\n"
            ),
            "tools/framework.py": (
                "from agent_framework import FunctionTool\n"
                "def underlying(value: int) -> int:\n"
                "    return value + 1\n"
                "renamed = FunctionTool(name='framework_exact', "
                "description='Exact framework metadata.', func=underlying)\n"
                "def a_plain_function() -> str:\n"
                "    return 'must not win'\n"
            ),
            "tools/json_value.py": (
                "from dataclasses import dataclass\n"
                "@dataclass\n"
                "class Payload:\n"
                "    text: str\n"
                "    values: tuple[int, ...]\n"
                "def json_value() -> Payload:\n"
                '    """Return a dataclass."""\n'
                "    return Payload('safe', (1, 2))\n"
            ),
            "tools/oversize.py": (
                "def oversized_value() -> str:\n"
                '    """Return more than the result cap."""\n'
                "    return 'x' * 600_000\n"
            ),
            "tools/slow.py": (
                "import time\n"
                "def slow_tool(seconds: float) -> str:\n"
                '    """Sleep for a bounded test interval."""\n'
                "    time.sleep(seconds)\n"
                "    return 'done'\n"
            ),
        },
    )
    journal = tmp_path / "journal"
    process = subprocess.Popen(
        [
            sys.executable,
            str(_EXECUTOR_SOURCE),
            "--app-zip",
            str(archive),
            "--extraction-root",
            str(tmp_path / "app"),
            "--journal-root",
            str(journal),
            "--workspace-root",
            str(tmp_path / "workspace"),
            "--poll-interval",
            "0.01",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    executor = _RunningExecutor(process, journal)
    try:
        _wait_for(journal / HYBRID_TOOL_READINESS_FILENAME)
        assert import_marker.exists()
        pid_metadata = json.loads((journal / HYBRID_TOOL_PID_FILENAME).read_text())
        imported_pid, import_count = import_marker.read_text().split(":")
        assert int(imported_pid) == pid_metadata["pid"]
        assert import_count == "1"
        yield executor
    finally:
        if process.poll() is None:
            executor.stop()


def test_secure_archive_extraction_rejects_traversal_links_and_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    traversal = tmp_path / "traversal.zip"
    _write_archive(traversal, {"../escape.py": "bad"})

    with pytest.raises(hybrid_executor.ExecutorProtocolError):
        hybrid_executor.securely_extract_application_archive(traversal, tmp_path / "traversal")

    link = tmp_path / "link.zip"
    with zipfile.ZipFile(link, "w") as archive:
        info = zipfile.ZipInfo("tools/link.py")
        info.create_system = 3
        info.external_attr = (0o120777 << 16) | 0xA000
        archive.writestr(info, "target")
    with pytest.raises(hybrid_executor.ExecutorProtocolError):
        hybrid_executor.securely_extract_application_archive(link, tmp_path / "link")

    bounded = tmp_path / "bounded.zip"
    _write_archive(bounded, {"tool.py": "four"})
    monkeypatch.setattr(hybrid_executor, "MAX_EXTRACTED_MEMBER_BYTES", 3)
    with pytest.raises(hybrid_executor.ExecutorProtocolError):
        hybrid_executor.securely_extract_application_archive(bounded, tmp_path / "bounded")


def test_archive_member_limit_matches_controller_and_accepts_large_dependency_closure(
    tmp_path: Path,
) -> None:
    assert (
        hybrid_executor.MAX_ARCHIVE_MEMBERS
        == controller_package._MAX_STANDARD_ZIP_ENTRIES
    )
    assert (
        hybrid_executor.MAX_EXTRACTED_TOTAL_BYTES
        == hybrid_executor.MAX_EXTRACTED_MEMBER_BYTES
        == controller_package._MAX_ARCHIVE_OPERATIONAL_SIZE
    )
    archive_path = tmp_path / "large-member-count.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for index in range(4097):
            archive.writestr(f"packages/module_{index}.py", b"")

    hybrid_executor.securely_extract_application_archive(
        archive_path,
        tmp_path / "large-member-count",
    )


def test_subprocess_discovers_exact_manifest_and_async_customer_tool(
    running_executor: _RunningExecutor,
) -> None:
    manifest = parse_hybrid_tool_manifest(
        (running_executor.journal / HYBRID_TOOL_MANIFEST_FILENAME).read_bytes()
    )

    assert [tool.name for tool in manifest.tools] == [
        "async_echo",
        "fail_tool",
        "framework_exact",
        "pid_tool",
        "json_value",
        "oversized_value",
        "slow_tool",
        "run_shell",
        "read_file",
        "write_file",
        "search_files",
    ]
    async_tool = manifest.tools[0]
    assert async_tool.description == "Echo asynchronously."
    assert async_tool.parameters["required"] == ["text"]
    assert all(tool.provenance == "generic" for tool in manifest.tools[-4:])

    _, result = running_executor.invoke("async-1", "async_echo", {"text": "hello"})
    _, framework_result = running_executor.invoke(
        "framework-1",
        "framework_exact",
        {"value": 2},
    )

    assert result.status == "success"
    assert result.value == {"text": "hello"}
    assert framework_result.value == 3


def test_generic_write_read_and_search_are_workspace_confined(
    running_executor: _RunningExecutor,
) -> None:
    _, written = running_executor.invoke(
        "write-1",
        "write_file",
        {"path": "notes/example.txt", "content": "first\nneedle\n"},
    )
    _, read = running_executor.invoke("read-1", "read_file", {"path": "notes/example.txt"})
    _, searched = running_executor.invoke(
        "search-1",
        "search_files",
        {"glob": "**/*.txt", "text": "needle", "max_results": 10},
    )
    _, escaped = running_executor.invoke("read-escape", "read_file", {"path": "../outside"})

    assert written.value == {"bytes_written": 13, "path": "notes/example.txt"}
    assert read.value == {"content": "first\nneedle\n", "path": "notes/example.txt"}
    assert searched.value == {
        "matches": [{"line_numbers": [2], "path": "notes/example.txt"}],
        "truncated": False,
    }
    assert escaped.status == "error"
    assert escaped.error is not None
    assert escaped.error.code == "invalid_tool_result"


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="requires the sandbox shell")
def test_generic_shell_captures_process_fields(running_executor: _RunningExecutor) -> None:
    _, result = running_executor.invoke(
        "shell-1",
        "run_shell",
        {"command": "printf out; printf err >&2; exit 7"},
    )

    assert result.status == "success"
    assert result.exit_code == 7
    assert result.stdout == "out"
    assert result.stderr == "err"

    _, path_result = running_executor.invoke(
        "shell-path",
        "run_shell",
        {"command": "command -v python3"},
    )
    assert path_result.status == "success"
    assert path_result.exit_code == 0
    assert path_result.stdout.strip()


def test_duplicate_call_id_preserves_first_result(running_executor: _RunningExecutor) -> None:
    first_payload, first = running_executor.invoke(
        "same-call",
        "write_file",
        {"path": "value.txt", "content": "first"},
    )
    second_payload, second = running_executor.invoke(
        "same-call",
        "write_file",
        {"path": "value.txt", "content": "second"},
    )
    _, read = running_executor.invoke("read-value", "read_file", {"path": "value.txt"})

    assert first.status == "success"
    assert second_payload == first_payload
    assert second == first
    assert read.value == {"content": "first", "path": "value.txt"}


def test_timeout_error_and_json_safe_results(running_executor: _RunningExecutor) -> None:
    _, timed_out = running_executor.invoke(
        "slow-1",
        "slow_tool",
        {"seconds": 0.5},
        deadline_seconds=0.05,
    )
    _, failed = running_executor.invoke("fail-1", "fail_tool", {})
    _, safe = running_executor.invoke("json-1", "json_value", {})
    _, oversized = running_executor.invoke("oversized-1", "oversized_value", {})

    assert timed_out.status == "error"
    assert timed_out.error is not None
    assert timed_out.error.code == "tool_timeout"
    assert failed.status == "error"
    assert failed.error is not None
    assert failed.error.code == "tool_error"
    assert "deliberate" in failed.error.message
    assert failed.stdout == "before failure\n"
    assert safe.value == {"text": "safe", "values": [1, 2]}
    assert oversized.status == "error"
    assert oversized.error is not None
    assert oversized.error.code == "invalid_tool_result"


def test_executor_rejects_duplicate_request_keys(running_executor: _RunningExecutor) -> None:
    request_path = (
        running_executor.journal / HYBRID_TOOL_REQUEST_DIRECTORY / "duplicate-request.json"
    )
    request_path.write_text(
        (
            '{"arguments":{},"call_id":"duplicate-request","call_id":"other",'
            '"deadline_unix_seconds":9999999999.0,"operation_id":"test",'
            '"protocol_version":"1","tool_name":"pid_tool","traceparent":null}'
        ),
        encoding="utf-8",
    )
    result_path = running_executor.journal / HYBRID_TOOL_RESULT_DIRECTORY / "duplicate-request.json"
    _wait_for(result_path)

    result = parse_hybrid_tool_result(result_path.read_bytes())

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "invalid_request"
