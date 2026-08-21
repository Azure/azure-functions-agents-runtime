from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from azure_functions_agents.execution.foundry_application_content import (
    APPLICATION_CONTENT_MANIFEST_VERSION,
    MAX_APPLICATION_CONTENT_MANIFEST_BYTES,
    ApplicationContentManifest,
    ApplicationContentManifestEntry,
    ApplicationContentManifestError,
    ApplicationContentManifestValidationError,
    build_application_content_manifest,
    compute_application_content_digest,
    parse_application_content_manifest,
    serialize_application_content_manifest,
    validate_application_content_manifest,
)

_GOLDEN_DIGEST = "sha256:89acf0571874eccbdf2a4201b11b813243f61ce04404bfbf269f894e2850deba"


def _write(root: Path, relative_path: str, content: bytes) -> Path:
    path = root.joinpath(*relative_path.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _write_application_tree(root: Path) -> None:
    _write(root, "agents.config.yaml", b"version: 1\n")
    _write(root, "main.agent.md", b"---\nname: Main\n---\n")
    _write(root, "tools/echo.py", b"print('hello')\n")


def _write_semantic_application_tree(root: Path) -> None:
    _write(root, "agents.config.yaml", b"version: 1\n")
    _write(root, "main.agent.md", b"---\nname: Main\n---\n")
    _write(root, "mcp.json", b'{"servers":{}}\n')
    _write(root, "requirements.txt", b"example-package==1.0\n")
    _write(
        root,
        "tools/echo.py",
        (
            b"from pathlib import Path\n"
            b"from helper import reply\n"
            b"TEMPLATE = Path(__file__).parents[1] / 'assets' / 'reply.txt'\n"
        ),
    )
    _write(root, "helper.py", b"reply = 'hello'\n")
    _write(root, "assets/reply.txt", b"ready\n")
    _write(root, "skills/writer/SKILL.md", b"---\nname: writer\n---\n")


def _manifest_json(entries: list[dict[str, object]]) -> str:
    return json.dumps(
        {"version": APPLICATION_CONTENT_MANIFEST_VERSION, "entries": entries},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def test_application_content_golden_vector_is_platform_invariant(tmp_path: Path) -> None:
    _write_application_tree(tmp_path)
    (tmp_path / "empty-directory").mkdir()

    manifest = build_application_content_manifest(tmp_path)

    assert [entry.path for entry in manifest.entries] == [
        "agents.config.yaml",
        "main.agent.md",
        "tools/echo.py",
    ]
    assert "empty-directory" not in {entry.path for entry in manifest.entries}
    assert all("\\" not in entry.path for entry in manifest.entries)
    assert compute_application_content_digest(tmp_path, manifest) == _GOLDEN_DIGEST
    assert compute_application_content_digest(
        tmp_path, serialize_application_content_manifest(manifest)
    ) == (_GOLDEN_DIGEST)


def test_manifest_ignores_metadata_but_detects_content_mutations(tmp_path: Path) -> None:
    _write_application_tree(tmp_path)
    manifest = build_application_content_manifest(tmp_path)
    digest = compute_application_content_digest(tmp_path, manifest)
    source = tmp_path / "tools" / "echo.py"

    os.utime(source, None)
    assert compute_application_content_digest(tmp_path, manifest) == digest

    source.write_bytes(b"print('world')\n")
    assert compute_application_content_digest(tmp_path, manifest) != digest


def test_manifest_ignores_deployment_artifacts_and_selects_tool_dependencies(
    tmp_path: Path,
) -> None:
    local_root = tmp_path / "local"
    deployed_root = tmp_path / "deployed"
    local_root.mkdir()
    deployed_root.mkdir()
    _write_semantic_application_tree(local_root)
    _write_semantic_application_tree(deployed_root)
    _write(local_root, "wheels/runtime.whl", b"local-wheel")
    _write(local_root, ".funcignore", b"wheels\n")
    _write(local_root, "host.json", b'{"version":"2.0"}')
    _write(local_root, "local.settings.template.json", b'{"Values":{}}')
    _write(local_root, ".python_packages/lib/site.py", b"local-build")
    _write(local_root, "tools/host.json", b'{"version":"local-tool"}')
    _write(local_root, "skills/writer/.funcignore", b"local-tool-artifact\n")
    _write(deployed_root, ".python_packages/lib/site.py", b"deployed-build")
    _write(deployed_root, "host.json", b'{"version":"3.0"}')
    _write(deployed_root, "tools/host.json", b'{"version":"deployed-tool"}')
    _write(deployed_root, "skills/writer/.funcignore", b"deployed-tool-artifact\n")

    local_manifest = build_application_content_manifest(local_root)
    deployed_manifest = build_application_content_manifest(deployed_root)

    assert [entry.path for entry in local_manifest.entries] == [
        "agents.config.yaml",
        "assets/reply.txt",
        "helper.py",
        "main.agent.md",
        "mcp.json",
        "requirements.txt",
        "skills/writer/SKILL.md",
        "tools/echo.py",
    ]
    assert local_manifest == deployed_manifest
    local_digest = compute_application_content_digest(local_root, local_manifest)
    assert local_digest == compute_application_content_digest(deployed_root, deployed_manifest)

    for path, replacement in (
        ("main.agent.md", b"---\nname: Changed\n---\n"),
        ("tools/echo.py", b"def changed() -> None:\n    pass\n"),
        ("assets/reply.txt", b"changed\n"),
        ("agents.config.yaml", b"version: 2\n"),
    ):
        source = local_root.joinpath(*path.split("/"))
        original = source.read_bytes()
        source.write_bytes(replacement)
        assert compute_application_content_digest(local_root) != local_digest
        source.write_bytes(original)

    _write(local_root, "wheels/runtime.whl", b"changed-wheel")
    assert compute_application_content_digest(local_root, local_manifest) == local_digest


def test_manifest_includes_explicit_tool_asset_with_deployment_filename(tmp_path: Path) -> None:
    _write_application_tree(tmp_path)
    _write(tmp_path, "host.json", b'{"asset":"required-by-tool"}')
    _write(
        tmp_path,
        "tools/echo.py",
        (
            b"from pathlib import Path\n"
            b"TOOL_ASSET = Path(__file__).parents[1] / 'host.json'\n"
        ),
    )

    manifest = build_application_content_manifest(tmp_path)

    assert [entry.path for entry in manifest.entries] == [
        "agents.config.yaml",
        "host.json",
        "main.agent.md",
        "tools/echo.py",
    ]


def test_manifest_rejects_length_path_and_missing_file_mutations(tmp_path: Path) -> None:
    _write_application_tree(tmp_path)
    manifest = build_application_content_manifest(tmp_path)

    (tmp_path / "agents.config.yaml").write_bytes(b"version: 10\n")
    with pytest.raises(ApplicationContentManifestValidationError):
        compute_application_content_digest(tmp_path, manifest)

    _write_application_tree(tmp_path)
    (tmp_path / "main.agent.md").rename(tmp_path / "renamed.agent.md")
    with pytest.raises(ApplicationContentManifestValidationError):
        validate_application_content_manifest(tmp_path, manifest)

    _write_application_tree(tmp_path)
    (tmp_path / "tools" / "echo.py").unlink()
    with pytest.raises(ApplicationContentManifestValidationError):
        validate_application_content_manifest(tmp_path, manifest)


def test_manifest_rejects_new_selected_content_not_in_the_published_manifest(
    tmp_path: Path,
) -> None:
    _write_application_tree(tmp_path)
    manifest = build_application_content_manifest(tmp_path)
    _write(tmp_path, "added.agent.md", b"---\nname: Added\n---\n")

    with pytest.raises(ApplicationContentManifestValidationError):
        validate_application_content_manifest(tmp_path, manifest)


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "local.settings.json",
        "private.pem",
        "private.key",
        "private.pfx",
        "private.p12",
    ],
)
def test_manifest_rejects_secret_paths_outside_semantic_inputs(tmp_path: Path, path: str) -> None:
    _write(tmp_path, path, b"excluded")

    with pytest.raises(ApplicationContentManifestValidationError):
        build_application_content_manifest(tmp_path)


def test_manifest_rejects_symlinks_without_following_them(tmp_path: Path) -> None:
    target = _write(tmp_path, "target.py", b"target")
    link = tmp_path / "linked.py"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("This platform does not permit test symlink creation.")

    with pytest.raises(ApplicationContentManifestValidationError):
        build_application_content_manifest(tmp_path)


def test_manifest_rejects_nonregular_files(tmp_path: Path) -> None:
    if os.name == "nt" or not hasattr(os, "mkfifo"):
        pytest.skip("Named pipes are unavailable on this platform.")
    pipe = tmp_path / "application.pipe"
    try:
        os.mkfifo(pipe)
    except OSError:
        pytest.skip("This platform does not permit named pipe creation.")
    try:
        with pytest.raises(ApplicationContentManifestValidationError):
            build_application_content_manifest(tmp_path)
    finally:
        pipe.unlink(missing_ok=True)


def test_manifest_parser_requires_canonical_paths_and_serialization() -> None:
    unnormalized_path = _manifest_json([{"path": "tools/../echo.py", "kind": "file", "length": 1}])
    noncanonical_order = json.dumps(
        {
            "version": APPLICATION_CONTENT_MANIFEST_VERSION,
            "entries": [{"path": "echo.py", "kind": "file", "length": 1}],
        }
    )
    duplicate_keys = '{"entries":[],"entries":[],"version":"fha_application_content_v2"}'

    for payload in (unnormalized_path, noncanonical_order, duplicate_keys):
        with pytest.raises(ApplicationContentManifestError):
            parse_application_content_manifest(payload)


def test_manifest_rejects_absolute_traversal_duplicate_and_case_colliding_paths() -> None:
    for path in ("/absolute.py", "../parent.py", r"C:\absolute.py"):
        with pytest.raises(ApplicationContentManifestValidationError):
            ApplicationContentManifestEntry.create(path=path, length=1)

    first = ApplicationContentManifestEntry.create(path="tools/echo.py", length=1)
    duplicate = ApplicationContentManifestEntry.create(path="tools/echo.py", length=1)
    upper = ApplicationContentManifestEntry.create(path="Tools/echo.py", length=1)
    with pytest.raises(ApplicationContentManifestValidationError):
        ApplicationContentManifest.create(entries=(first, duplicate))
    with pytest.raises(ApplicationContentManifestValidationError):
        ApplicationContentManifest.create(entries=(first, upper))


def test_manifest_serialization_is_bounded_and_round_trips() -> None:
    manifest = ApplicationContentManifest.create(
        entries=(
            ApplicationContentManifestEntry.create(path="main.agent.md", length=7),
            ApplicationContentManifestEntry.create(path="tools/echo.py", length=12),
        ),
        runtime_projection='{"version":"fha_runtime_projection_v0"}',
    )

    serialized = serialize_application_content_manifest(manifest)

    assert len(serialized.encode("utf-8")) <= MAX_APPLICATION_CONTENT_MANIFEST_BYTES
    assert parse_application_content_manifest(serialized) == manifest


def test_runtime_projection_is_carried_and_bound_into_content_digest(tmp_path: Path) -> None:
    _write_application_tree(tmp_path)
    source_manifest = build_application_content_manifest(tmp_path)
    first = ApplicationContentManifest.create(
        entries=source_manifest.entries,
        runtime_projection='{"default_model":"model-a"}',
    )
    second = ApplicationContentManifest.create(
        entries=source_manifest.entries,
        runtime_projection='{"default_model":"model-b"}',
    )

    assert validate_application_content_manifest(tmp_path, first) == first
    assert parse_application_content_manifest(
        serialize_application_content_manifest(first)
    ) == first
    assert compute_application_content_digest(
        tmp_path,
        first,
    ) != compute_application_content_digest(tmp_path, second)
