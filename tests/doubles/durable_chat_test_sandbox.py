"""File/process transport double beneath the real durable sandbox execution lane."""

from __future__ import annotations

import hashlib
import io
import json
import shlex
import zipfile
from pathlib import Path
from uuid import uuid4

from azure_functions_agents.controller.package import CapturedContentPackage
from azure_functions_agents.experimental.hybrid_protocol import (
    HYBRID_TOOL_MANIFEST_FILENAME,
    HYBRID_TOOL_PACKAGE_VERIFICATION_FILENAME,
    HYBRID_TOOL_PID_FILENAME,
    HYBRID_TOOL_READINESS_FILENAME,
    HYBRID_TOOL_REQUEST_DIRECTORY,
    HYBRID_TOOL_RESULT_DIRECTORY,
    HybridInvocationStatus,
    HybridToolDescriptor,
    HybridToolInternalTimings,
    HybridToolInvocationResult,
    HybridToolManifest,
    HybridToolProvenance,
    canonical_hybrid_json_bytes,
    parse_hybrid_tool_request,
)
from azure_functions_agents.experimental.hybrid_tools import (
    _APP_ZIP_PATH,
    _JOURNAL_PATH,
    _WORKSPACE_EXPORT_PATH,
    _WORKSPACE_IMPORT_PATH,
)
from azure_functions_agents.transport.manifest import ExpectedSandboxManifestBinding
from azure_functions_agents.transport.transport_models import (
    PersistedSandboxBinding,
    SandboxExecResult,
    SandboxSummary,
)
from tests.doubles.fake_session_runtime import (
    FakeSandboxSessionHandle,
    FakeSandboxSessionProvider,
)

TEST_SANDBOX_GROUP = (
    "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/durable-chat-e2e/"
    "providers/Microsoft.App/sandboxGroups/local-transport-double"
)


def _archive(payload: object) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("workspace.json", json.dumps(payload))
    return buffer.getvalue()


class DurableChatTestSandboxWorld:
    def __init__(self) -> None:
        self.handles: list[_TestSandboxHandle] = []
        self.live: dict[str, FakeSandboxSessionHandle] = {}
        payload = _archive({"fixture": "durable-chat-e2e"})
        self._package = CapturedContentPackage.create(
            archive_bytes=payload,
            digest_kind="funcs_zip",
            digest="sha256:" + hashlib.sha256(payload).hexdigest(),
        )

    async def package(self, _root: Path) -> CapturedContentPackage:
        return self._package

    async def open_provider(self) -> FakeSandboxSessionProvider:
        handle = _TestSandboxHandle(f"e2e-sandbox-{uuid4().hex}")
        self.handles.append(handle)
        return _TestSandboxProvider(handle, self.live)

    def summaries(self) -> list[dict[str, object]]:
        return [
            {
                "sandbox_id": handle.identity.sandbox_id,
                "sandbox_group_resource_id": handle.identity.group_resource_id,
                "delete_calls": handle.delete_calls,
                "invocations": handle.invocations,
            }
            for handle in self.handles
        ]


class _TestSandboxProvider(FakeSandboxSessionProvider):
    def __init__(
        self,
        handle: _TestSandboxHandle,
        live: dict[str, FakeSandboxSessionHandle],
    ) -> None:
        super().__init__(handle, group_resource_id=TEST_SANDBOX_GROUP)
        self.sandboxes = live

    async def get_sandbox_summary(self, sandbox_id: str) -> SandboxSummary | None:
        handle = self.sandboxes.get(sandbox_id)
        if handle is None or handle.delete_calls:
            return None
        return SandboxSummary.create(
            sandbox_id=sandbox_id,
            labels=handle.labels,
            state="Running",
        )

    async def attach(
        self,
        persisted: PersistedSandboxBinding,
        expected_manifest: ExpectedSandboxManifestBinding,
        *,
        readiness_timeout_seconds: float,
    ) -> FakeSandboxSessionHandle:
        del readiness_timeout_seconds
        self.attach_calls += 1
        handle = self.sandboxes[persisted.sandbox_id]
        assert handle.delete_calls == 0
        assert expected_manifest.sandbox_id == handle.identity.sandbox_id
        assert persisted.group.resource_id == handle.identity.group_resource_id
        return handle

    async def resume(
        self,
        persisted: PersistedSandboxBinding,
        expected_manifest: ExpectedSandboxManifestBinding,
        *,
        readiness_timeout_seconds: float,
    ) -> FakeSandboxSessionHandle:
        self.resume_calls += 1
        return await self.attach(
            persisted,
            expected_manifest,
            readiness_timeout_seconds=readiness_timeout_seconds,
        )


class _TestSandboxHandle(FakeSandboxSessionHandle):
    def __init__(self, sandbox_id: str) -> None:
        super().__init__(sandbox_id, group_resource_id=TEST_SANDBOX_GROUP)
        self.invocations: list[dict[str, object]] = []
        self.workspace: list[dict[str, object]] = []

    async def exec(
        self, command: str, *, timeout_seconds: float | None = None
    ) -> SandboxExecResult:
        await super().exec(command, timeout_seconds=timeout_seconds)
        arguments = shlex.split(command)
        if arguments[0] == "nohup":
            self._start_executor(arguments)
            return SandboxExecResult(exit_code=0, stdout="12345\n", stderr="")
        if "--workspace-operation" in arguments:
            operation = arguments[arguments.index("--workspace-operation") + 1]
            if operation == "export":
                payload = _archive(self.workspace)
                self.seed_file(_WORKSPACE_EXPORT_PATH, payload)
                return SandboxExecResult(
                    exit_code=0,
                    stdout="sha256:" + hashlib.sha256(payload).hexdigest(),
                    stderr="",
                )
            if operation == "restore":
                payload = await self.read_file(_WORKSPACE_IMPORT_PATH)
                expected_digest = arguments[arguments.index("--workspace-digest") + 1]
                assert expected_digest == "sha256:" + hashlib.sha256(payload).hexdigest()
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    self.workspace = json.loads(archive.read("workspace.json"))
                return SandboxExecResult(exit_code=0, stdout="", stderr="")
        raise AssertionError(f"Unexpected native-test sandbox command: {arguments[0]}")

    def _start_executor(self, arguments: list[str]) -> None:
        payload = self._files[_APP_ZIP_PATH]
        digest = arguments[arguments.index("--app-digest") + 1]
        assert digest == "sha256:" + hashlib.sha256(payload).hexdigest()
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            assert archive.testzip() is None
        manifest = HybridToolManifest(
            protocol_version="1",
            tools=(
                HybridToolDescriptor(
                    name="append_note",
                    description="Append a note in the isolated E2E workspace.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "nonce": {"type": "string"},
                            "step": {"type": "integer"},
                        },
                        "required": ["nonce", "step"],
                        "additionalProperties": False,
                    },
                    provenance=HybridToolProvenance.LOCAL,
                ),
            ),
        )
        documents = {
            HYBRID_TOOL_MANIFEST_FILENAME: manifest,
            HYBRID_TOOL_PACKAGE_VERIFICATION_FILENAME: {
                "protocol_version": "1", "verified": True, "duration_ms": 1,
            },
            HYBRID_TOOL_READINESS_FILENAME: {
                "protocol_version": "1", "ready": True, "pid": 12345,
            },
            HYBRID_TOOL_PID_FILENAME: {"protocol_version": "1", "pid": 12345},
        }
        for name, document in documents.items():
            self.seed_file(f"{_JOURNAL_PATH}/{name}", canonical_hybrid_json_bytes(document))

    async def write_file(self, path: str, content: bytes, *, create_dirs: bool = False) -> None:
        await super().write_file(path, content, create_dirs=create_dirs)
        if not path.startswith(f"{_JOURNAL_PATH}/{HYBRID_TOOL_REQUEST_DIRECTORY}/"):
            return
        request = parse_hybrid_tool_request(content)
        assert request.tool_name == "append_note"
        invocation = {
            "call_key": request.call_id,
            "nonce": request.arguments["nonce"],
            "step": request.arguments["step"],
        }
        self.invocations.append(invocation)
        self.workspace.append(invocation)
        result = HybridToolInvocationResult(
            protocol_version="1",
            call_id=request.call_id,
            tool_name=request.tool_name,
            request_hash=request.request_hash,
            status=HybridInvocationStatus.SUCCESS,
            value={"note_count": len(self.workspace)},
            stdout="",
            stderr="",
            exit_code=0,
            error=None,
            timings=HybridToolInternalTimings(
                queue_wait_ms=0, execution_ms=1, serialization_ms=0,
            ),
        )
        self.seed_file(
            f"{_JOURNAL_PATH}/{HYBRID_TOOL_RESULT_DIRECTORY}/{request.call_id}.json",
            canonical_hybrid_json_bytes(result),
        )
