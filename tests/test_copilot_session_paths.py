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


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (r"Q:\session-state", "/session-state"),
        (r"q:\SESSION-STATE\temp", "/session-state/temp"),
        ("Q:/session-state/temp/Mixed.Txt", "/session-state/temp/Mixed.Txt"),
        (r"Q:\session-state\temp\.\file", "/session-state/temp/file"),
    ],
)
def test_windows_state_paths_use_only_the_current_workspace_drive(path, expected):
    assert paths.select_session_path_policy("windows").normalize(
        path, r"Q:\worker\workspace",
    ) == expected


@pytest.mark.parametrize(
    ("path", "workspace"),
    [
        (r"C:\session-state\temp", r"Q:\worker\workspace"),
        (r"Q:\session-statex\temp", r"Q:\worker\workspace"),
        (r"Q:\other\temp", r"Q:\worker\workspace"),
        (r"Q:session-state\temp", r"Q:\worker\workspace"),
        (r"Q:\session-state\..\workspace\file", r"Q:\worker\workspace"),
        (r"Q:\session-state\temp\NUL.txt", r"Q:\worker\workspace"),
        (r"Q:\session-state\temp\file:stream", r"Q:\worker\workspace"),
        (r"Q:\session-state\temp\trailing.", r"Q:\worker\workspace"),
        (r"\\server\share\session-state\temp", r"\\server\share\workspace"),
        (r"Q:\session-state\temp", ""),
        (r"Q:\session-state\temp", "/workspace"),
        (r"Q:\session-state\temp", r"Q:worker\workspace"),
        ("Q:\\session-state\\temp\\file\x00", r"Q:\worker\workspace"),
    ],
)
def test_windows_state_aliases_keep_existing_path_denials(path, workspace):
    with pytest.raises(OSError) as error:
        paths.select_session_path_policy("windows").normalize(path, workspace)
    assert error.value.errno == (errno.EINVAL if "\x00" in path else errno.EACCES)


def test_windows_state_alias_does_not_replace_the_exact_physical_workspace():
    assert paths.select_session_path_policy("windows").normalize(
        r"Q:\session-state\temp", r"Q:\session-state",
    ) == "/workspace/temp"


def test_posix_policy_does_not_accept_windows_state_alias():
    with pytest.raises(OSError) as error:
        paths.select_session_path_policy("posix").normalize(
            r"Q:\session-state\temp", r"Q:\worker\workspace",
        )
    assert error.value.errno == errno.EACCES
