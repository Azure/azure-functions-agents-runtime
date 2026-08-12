"""Conformance tests for the exact direct-file and separate process ports."""

from __future__ import annotations

import pytest

from azure_functions_agents.transport.aca_sdk import AcaSandboxAdapter, AcaSandboxHandle
from azure_functions_agents.transport.ports import (
    SandboxFileTransport,
    SandboxProcessTransport,
    SandboxSessionHandle,
    SandboxSessionProvider,
)
from azure_functions_agents.transport.transport_models import (
    ProvisionedSandboxIdentity,
    SandboxExecResult,
    SandboxGroupIdentity,
)
from tests.doubles.fake_aca_sdk import FakeCredential, FakeSdkEnvironment
from tests.doubles.fake_sandbox_transport import FakeSandboxTransport


def _protocol_methods(protocol: type[object]) -> set[str]:
    return {
        name
        for name, member in protocol.__dict__.items()
        if callable(member) and not name.startswith("_")
    }


def test_file_transport_protocol_has_exactly_six_direct_file_verbs() -> None:
    assert _protocol_methods(SandboxFileTransport) == {
        "list_files",
        "stat_file",
        "read_file",
        "write_file",
        "delete_file",
        "mkdir",
    }
    assert "exec" not in _protocol_methods(SandboxFileTransport)


def test_process_transport_protocol_has_only_process_exec() -> None:
    assert _protocol_methods(SandboxProcessTransport) == {"exec"}
    assert not _protocol_methods(SandboxProcessTransport) & _protocol_methods(SandboxFileTransport)


def test_fake_structurally_satisfies_both_runtime_owned_protocols() -> None:
    transport = FakeSandboxTransport()

    assert isinstance(transport, SandboxFileTransport)
    assert isinstance(transport, SandboxProcessTransport)


def test_aca_adapter_and_handle_satisfy_session_protocols() -> None:
    environment = FakeSdkEnvironment()
    credential = FakeCredential()
    group = SandboxGroupIdentity(
        resource_id=(
            "/subscriptions/sub/resourceGroups/rg/providers/"
            "Microsoft.App/sandboxGroups/group"
        ),
        subscription_id="sub",
        resource_group="rg",
        group_name="group",
        region="westus2",
    )
    adapter = AcaSandboxAdapter(
        group=group,
        credential=credential,
        group_client=environment.make_group_client(
            "endpoint",
            credential,
            subscription_id="sub",
            resource_group="rg",
            sandbox_group="group",
        ),
        factories=environment.factories(),
    )
    handle = AcaSandboxHandle(
        sdk_client=environment.add_sandbox("sandbox-1"),
        identity=ProvisionedSandboxIdentity.create(
            sandbox_id="sandbox-1",
            group_resource_id=group.resource_id,
            region=group.region,
        ),
    )

    assert isinstance(adapter, SandboxSessionProvider)
    assert isinstance(handle, SandboxSessionHandle)


@pytest.mark.asyncio
async def test_fake_direct_file_operations_round_trip_binary_without_exec() -> None:
    transport = FakeSandboxTransport()
    content = b"\x00binary\xff"

    await transport.mkdir("/session")
    await transport.write_file("/session/input.bin", content)

    listed = await transport.list_files("/session")
    stat = await transport.stat_file("/session/input.bin")
    read = await transport.read_file("/session/input.bin")
    await transport.delete_file("/session/input.bin")

    assert listed[0].name == "input.bin"
    assert stat.size == len(content)
    assert read == content
    assert [call.operation for call in transport.calls] == [
        "mkdir",
        "write_file",
        "list_files",
        "stat_file",
        "read_file",
        "delete_file",
    ]


@pytest.mark.asyncio
async def test_fake_exec_is_separate_from_file_calls() -> None:
    transport = FakeSandboxTransport()
    transport.next_exec_result = SandboxExecResult(exit_code=7, stdout="out", stderr="err")

    result = await transport.exec("harness --status")

    assert result == SandboxExecResult(exit_code=7, stdout="out", stderr="err")
    assert [call.operation for call in transport.calls] == ["exec"]


@pytest.mark.asyncio
async def test_fake_handles_are_independent_for_concurrent_sessions() -> None:
    first = FakeSandboxTransport()
    second = FakeSandboxTransport()

    await first.write_file("/journal/value", b"first", create_dirs=True)
    await second.write_file("/journal/value", b"second", create_dirs=True)

    assert await first.read_file("/journal/value") == b"first"
    assert await second.read_file("/journal/value") == b"second"
