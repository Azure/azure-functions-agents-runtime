from __future__ import annotations

import errno

import pytest

from azure_functions_agents.harness.copilot_sdk import _copilot_session_paths as paths


@pytest.mark.parametrize(
    ("conventions", "path", "workspace", "expected"),
    [
        ("windows", r"C:\WORKER\Workspace\Mixed.Txt", r"c:\worker\workspace", "/workspace/Mixed.Txt"),
        ("windows", r"\\server\share\workspace\file", r"\\server\share\workspace", "/workspace/file"),
        ("windows", r"\workspace\file", "", "/workspace/file"),
        ("windows", "folder/File", "", "/workspace/folder/File"),
        ("posix", "/host/Workspace/Mixed.Txt", "/host/Workspace", "/workspace/Mixed.Txt"),
        ("posix", "/workspace/file", r"C:\host\workspace", "/workspace/file"),
        ("posix", "./folder/./file", "", "/workspace/folder/file"),
        ("posix", "/", "", "/"),
        ("windows", "/session-state/arbitrary.sdk", "", "/session-state/arbitrary.sdk"),
    ],
)
def test_policies_preserve_exact_workspace_and_emitted_spelling(
    conventions, path, workspace, expected,
):
    policy = paths.select_session_path_policy(conventions)
    assert policy.normalize(path, workspace) == expected


@pytest.mark.parametrize(
    ("conventions", "path", "workspace"),
    [
        ("windows", r"C:\worker\workspace-lookalike\file", r"C:\worker\workspace"),
        ("windows", r"C:\other\workspace\file", r"C:\worker\workspace"),
        ("windows", r"\\other\share\workspace\file", r"\\server\share\workspace"),
        ("windows", "/workspace/CON", ""),
        ("windows", "/workspace/file:stream", ""),
        ("windows", "/workspace/NUL.txt", ""),
        ("windows", "/workspace/trailing.", ""),
        ("windows", "/workspace/a/../b", ""),
        ("posix", "/host/workspace/file", "/host/Workspace"),
        ("posix", r"folder\file", ""),
        ("posix", "/session-state/../workspace/file", ""),
        ("posix", "/workspace/a\x1fb", ""),
        ("posix", "//outside/worker", ""),
        ("posix", "/Workspace/file", ""),
    ],
)
def test_policy_does_not_resolve_traversal_aliases_or_reserved_names(conventions, path, workspace):
    with pytest.raises(OSError):
        paths.select_session_path_policy(conventions).normalize(path, workspace)


def test_callback_and_physical_host_policy_are_independent():
    assert paths.select_session_path_policy("posix").normalize("file:stream") == "/workspace/file:stream"
    host = paths.select_session_path_policy("windows")
    with pytest.raises(OSError) as error:
        host.validate_local_parts(("C:\\", "workspace", "file:stream"))
    assert error.value.errno == errno.EINVAL


def test_registry_contains_only_the_two_sdk_conventions():
    assert set(paths._PATH_POLICIES) == {"windows", "posix"}
    assert paths.select_session_path_policy("windows") is paths.select_session_path_policy("windows")
    assert paths.select_session_path_policy("posix") is paths.select_session_path_policy("posix")
