from __future__ import annotations

import errno
import os
from unittest.mock import Mock

import pytest
from copilot.session_fs_provider import SessionFsFileInfo, SessionFsProvider

from azure_functions_agents.harness.copilot_sdk import _copilot_session_fs as fs
from azure_functions_agents.harness.copilot_sdk._copilot_session_local import (
    LocalSessionFileBackend,
    open_local_backend,
)
from tests.test_copilot_session_fs import local_route as local_route


@pytest.mark.asyncio
async def test_local_factory_is_structural_and_provider_is_the_actual_sdk_contract(local_route):
    backend = await open_local_backend(local_route, "owned-prefix")
    assert type(backend) is LocalSessionFileBackend
    assert fs.SessionFileBackend not in LocalSessionFileBackend.__mro__
    await backend.initialize()
    await backend.write_file("opaque", b"\x00\xff\r\n", None)
    assert await backend.read_file("opaque") == b"\x00\xff\r\n"
    assert isinstance(await backend.stat("opaque"), SessionFsFileInfo)
    provider = fs.CopilotSessionFs(backend)
    assert isinstance(provider, SessionFsProvider)
    await provider.close()


@pytest.mark.asyncio
async def test_callback_policy_and_backend_factory_are_selected_once(local_route, monkeypatch):
    from azure_functions_agents.harness.copilot_sdk._copilot_session_identity import StorageMode

    factory = Mock(wraps=fs._BACKEND_FACTORIES[StorageMode.LOCAL])
    monkeypatch.setitem(fs._BACKEND_FACTORIES, StorageMode.LOCAL, factory)
    provider = await fs.open_session_fs(local_route, "agent", "session", conventions="posix")
    monkeypatch.setitem(
        fs._BACKEND_FACTORIES, StorageMode.LOCAL, Mock(side_effect=AssertionError("Backend reselection"))
    )
    monkeypatch.setattr(
        fs, "select_session_path_policy", Mock(side_effect=AssertionError("Callback policy reselection"))
    )
    try:
        await provider.write_file("/workspace/file", "opaque")
        assert await provider.read_file("/workspace/file") == "opaque"
        factory.assert_called_once()
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Physical Windows filenames")
async def test_posix_callbacks_still_obey_the_windows_host(local_route):
    provider = await fs.open_session_fs(local_route, "agent", "session", conventions="posix")
    try:
        with pytest.raises(OSError) as error:
            await provider.write_file("/workspace/CON", "not a device")
        assert error.value.errno == errno.EINVAL
    finally:
        await provider.close()
