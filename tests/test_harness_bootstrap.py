from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from azure_functions_agents.harness import bootstrap


def _cpython_abi() -> str:
    return f"{sys.version_info.major}{sys.version_info.minor}"


def _archive(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    return buffer.getvalue()


def _write_session(
    root: Path,
    archive: bytes,
    *,
    protocol_version: str = "1",
) -> tuple[Path, Path]:
    session = root / "session"
    content = session / "content"
    content.mkdir(parents=True)
    digest = f"sha256:{hashlib.sha256(archive).hexdigest()}"
    seed = {
        "manifest_version": 1,
        "protocol_version": protocol_version,
        "session_id": "session-1",
        "owner_hash_version": "o1",
        "owner_hash": "o1-" + ("a" * 52),
        "app_hash": "a1-" + ("b" * 52),
        "sandbox_group_resource_id": (
            "/subscriptions/subscription/resourceGroups/group/"
            "providers/Microsoft.App/sandboxGroups/sandbox-group"
        ),
        "sandbox_id": "sandbox-1",
        "generation": 1,
        "digest_kind": "funcs_zip",
        "digest": digest,
        "state_store_fingerprint": "s1-" + ("c" * 52),
    }
    (content / "app.zip").write_bytes(archive)
    (content / "app.sha256").write_text(f"{digest}\n", encoding="ascii")
    (content / "manifest.seed.json").write_text(
        json.dumps(seed, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    bootstrap_path = session / "bootstrap.py"
    source = Path(bootstrap.__file__ or "").read_bytes()
    bootstrap_path.write_bytes(source)
    (session / "bootstrap.sha256").write_text(
        f"sha256:{hashlib.sha256(source).hexdigest()}\n",
        encoding="ascii",
    )
    return session, bootstrap_path


@pytest.fixture
def _linux_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "_live_glibc_version", lambda: (2, 36))
    monkeypatch.setattr(bootstrap.sysconfig, "get_platform", lambda: "linux-x86_64")
    monkeypatch.setattr(bootstrap.sys, "path", list(bootstrap.sys.path))
    yield
    monkeypatch.delenv(bootstrap.SANDBOX_MARKER_ENV_VAR, raising=False)


def test_prepare_sandbox_verifies_content_and_publishes_manifest(
    tmp_path: Path,
    _linux_bootstrap: None,
) -> None:
    session, bootstrap_path = _write_session(
        tmp_path,
        _archive(
            {
                "function_app.py": b"print('agent')\n",
                ".python_packages/lib/site-packages/example/__init__.py": b"",
            }
        ),
    )
    application = tmp_path / "app"

    context = bootstrap.prepare_sandbox(
        session,
        application_directory=application,
        bootstrap_path=bootstrap_path,
    )

    assert context.application_directory == application
    assert (application / "function_app.py").read_bytes() == b"print('agent')\n"
    manifest = json.loads((session / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["digest"] == context.manifest["digest"]
    assert manifest["state_store_fingerprint"] == "s1-" + ("c" * 52)
    assert bootstrap.SANDBOX_MARKER_ENV_VAR in bootstrap.os.environ


def test_bootstrap_configures_delivered_import_paths_without_pythonpath(
    tmp_path: Path,
    _linux_bootstrap: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, bootstrap_path = _write_session(
        tmp_path,
        _archive(
            {
                "azure_functions_agents/__init__.py": b"",
                "azure_functions_agents/harness/__init__.py": b"MARKER = 'harness'\n",
                "azure_functions_agents/harness/__main__.py": b"from . import MARKER\n",
                (
                    ".python_packages/lib/site-packages/runtime_paths.pth"
                ): b"extra_modules\n",
                (
                    ".python_packages/lib/site-packages/extra_modules/delivered_dependency.py"
                ): b"MARKER = 'dependency'\n",
            }
        ),
    )
    application = tmp_path / "app"
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setattr(bootstrap.sys, "path", list(bootstrap.sys.path))
    bootstrap.prepare_sandbox(
        session,
        application_directory=application,
        bootstrap_path=bootstrap_path,
    )

    try:
        dependency = importlib.import_module("delivered_dependency")
        site_packages = application / ".python_packages" / "lib" / "site-packages"
        assert str(site_packages) in bootstrap.sys.path
        assert dependency.MARKER == "dependency"
    finally:
        sys.modules.pop("delivered_dependency", None)


def test_isolated_bootstrap_does_not_load_unverified_sitecustomize(tmp_path: Path) -> None:
    session, bootstrap_path = _write_session(tmp_path, _archive({"function_app.py": b"agent"}))
    application = tmp_path / "app"
    application.mkdir()
    marker = tmp_path / "sitecustomize-ran"
    (application / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    (session / "content" / "app.zip").write_bytes(b"tampered")
    environment = {**os.environ, "PYTHONPATH": str(application)}

    result = subprocess.run(
        [
            sys.executable,
            "-E",
            "-S",
            str(bootstrap_path),
            "--session-directory",
            str(session),
            "--journal-root",
            str(tmp_path / "journal"),
            "--application-directory",
            str(application),
        ],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=environment,
        text=True,
    )

    assert result.returncode == bootstrap.EX_CONFIG
    assert not marker.exists()
    assert not (session / "manifest.json").exists()


@pytest.mark.parametrize("runtime_minor", (13, 14))
def test_prepare_sandbox_accepts_compatible_abi3_archive_on_later_cpython(
    tmp_path: Path,
    _linux_bootstrap: None,
    monkeypatch: pytest.MonkeyPatch,
    runtime_minor: int,
) -> None:
    monkeypatch.setattr(
        bootstrap,
        "_runtime_cpython_version",
        lambda: (3, runtime_minor),
        raising=False,
    )
    session, bootstrap_path = _write_session(
        tmp_path,
        _archive(
            {
                "package/module.abi3.so": b"binary",
                "package-1.0.dist-info/WHEEL": (
                    b"Wheel-Version: 1.0\n"
                    b"Tag: cp39-abi3-manylinux_2_17_x86_64\n"
                ),
            }
        ),
    )

    bootstrap.prepare_sandbox(
        session,
        application_directory=tmp_path / "app",
        bootstrap_path=bootstrap_path,
    )


@pytest.mark.parametrize("runtime_minor", (13, 14))
def test_prepare_sandbox_rejects_newer_version_specific_abi(
    tmp_path: Path,
    _linux_bootstrap: None,
    monkeypatch: pytest.MonkeyPatch,
    runtime_minor: int,
) -> None:
    monkeypatch.setattr(
        bootstrap,
        "_runtime_cpython_version",
        lambda: (3, runtime_minor),
        raising=False,
    )
    incompatible = f"3{runtime_minor + 1}"
    session, bootstrap_path = _write_session(
        tmp_path,
        _archive(
            {
                "package-1.0.dist-info/WHEEL": (
                    f"Wheel-Version: 1.0\nTag: cp{incompatible}-cp{incompatible}-"
                    "manylinux_2_17_x86_64\n"
                ).encode()
            }
        ),
    )

    with pytest.raises(bootstrap.BootstrapFailure) as error:
        bootstrap.prepare_sandbox(
            session,
            application_directory=tmp_path / "app",
            bootstrap_path=bootstrap_path,
        )

    assert error.value.code == "python_abi_mismatch"


@pytest.mark.parametrize("runtime_minor", (13, 14))
def test_prepare_sandbox_rejects_newer_abi3_requirement(
    tmp_path: Path,
    _linux_bootstrap: None,
    monkeypatch: pytest.MonkeyPatch,
    runtime_minor: int,
) -> None:
    monkeypatch.setattr(bootstrap, "_runtime_cpython_version", lambda: (3, runtime_minor))
    incompatible = f"3{runtime_minor + 1}"
    session, bootstrap_path = _write_session(
        tmp_path,
        _archive(
            {
                "package-1.0.dist-info/WHEEL": (
                    f"Wheel-Version: 1.0\nTag: cp{incompatible}-abi3-"
                    "manylinux_2_17_x86_64\n"
                ).encode()
            }
        ),
    )

    with pytest.raises(bootstrap.BootstrapFailure) as error:
        bootstrap.prepare_sandbox(
            session,
            application_directory=tmp_path / "app",
            bootstrap_path=bootstrap_path,
        )

    assert error.value.code == "python_abi_mismatch"


def test_prepare_sandbox_rejects_digest_mismatch_without_publishing(
    tmp_path: Path,
    _linux_bootstrap: None,
) -> None:
    session, bootstrap_path = _write_session(tmp_path, _archive({"function_app.py": b"agent"}))
    (session / "content" / "app.zip").write_bytes(b"tampered")

    with pytest.raises(bootstrap.BootstrapFailure, match="verification failed"):
        bootstrap.prepare_sandbox(
            session,
            application_directory=tmp_path / "app",
            bootstrap_path=bootstrap_path,
        )

    assert not (session / "manifest.json").exists()


def test_prepare_sandbox_rejects_zip_slip_members(
    tmp_path: Path,
    _linux_bootstrap: None,
) -> None:
    session, bootstrap_path = _write_session(
        tmp_path,
        _archive({"../escape.py": b"agent"}),
    )

    with pytest.raises(bootstrap.BootstrapFailure, match="archive is invalid"):
        bootstrap.prepare_sandbox(
            session,
            application_directory=tmp_path / "app",
            bootstrap_path=bootstrap_path,
        )

    assert not (session / "manifest.json").exists()


def test_prepare_sandbox_rejects_duplicate_archive_members(
    tmp_path: Path,
    _linux_bootstrap: None,
) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("function_app.py", b"first")
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("function_app.py", b"second")
    session, bootstrap_path = _write_session(tmp_path, buffer.getvalue())

    with pytest.raises(bootstrap.BootstrapFailure) as error:
        bootstrap.prepare_sandbox(
            session,
            application_directory=tmp_path / "app",
            bootstrap_path=bootstrap_path,
        )

    assert error.value.code == "archive_path_invalid"
    assert not (session / "manifest.json").exists()


def test_prepare_sandbox_rejects_a_standard_library_shadow(
    tmp_path: Path,
    _linux_bootstrap: None,
) -> None:
    session, bootstrap_path = _write_session(
        tmp_path,
        _archive({"json.py": b"not the standard library"}),
    )

    with pytest.raises(bootstrap.BootstrapFailure) as error:
        bootstrap.prepare_sandbox(
            session,
            application_directory=tmp_path / "app",
            bootstrap_path=bootstrap_path,
        )

    assert error.value.code == "stdlib_shadowing"
    assert not (session / "manifest.json").exists()


def test_prepare_sandbox_rejects_incompatible_compiled_python(
    tmp_path: Path,
    _linux_bootstrap: None,
) -> None:
    session, bootstrap_path = _write_session(
        tmp_path,
        _archive(
            {
                (
                    "package/module.cpython-"
                    f"{sys.version_info.major}{sys.version_info.minor + 1}-x86_64-linux-gnu.so"
                ): b"binary"
            }
        ),
    )

    with pytest.raises(bootstrap.BootstrapFailure, match="Python ABI"):
        bootstrap.prepare_sandbox(
            session,
            application_directory=tmp_path / "app",
            bootstrap_path=bootstrap_path,
        )

    assert not (session / "manifest.json").exists()


def test_prepare_sandbox_rejects_musl_compiled_extension(
    tmp_path: Path,
    _linux_bootstrap: None,
) -> None:
    session, bootstrap_path = _write_session(
        tmp_path,
        _archive({f"package/module.cpython-{_cpython_abi()}-x86_64-linux-musl.so": b"binary"}),
    )

    with pytest.raises(bootstrap.BootstrapFailure, match="platform"):
        bootstrap.prepare_sandbox(
            session,
            application_directory=tmp_path / "app",
            bootstrap_path=bootstrap_path,
        )

    assert not (session / "manifest.json").exists()


@pytest.mark.parametrize("runtime_minor", (13, 14))
def test_prepare_sandbox_rejects_musllinux_wheel_tag(
    tmp_path: Path,
    _linux_bootstrap: None,
    monkeypatch: pytest.MonkeyPatch,
    runtime_minor: int,
) -> None:
    monkeypatch.setattr(bootstrap, "_runtime_cpython_version", lambda: (3, runtime_minor))
    session, bootstrap_path = _write_session(
        tmp_path,
        _archive(
            {
                "package-1.0.dist-info/WHEEL": (
                    (
                        "Wheel-Version: 1.0\n"
                        f"Tag: cp3{runtime_minor}-abi3-musllinux_1_2_x86_64\n"
                    ).encode()
                )
            }
        ),
    )

    with pytest.raises(bootstrap.BootstrapFailure, match="platform"):
        bootstrap.prepare_sandbox(
            session,
            application_directory=tmp_path / "app",
            bootstrap_path=bootstrap_path,
        )

    assert not (session / "manifest.json").exists()


@pytest.mark.parametrize("runtime_minor", (13, 14))
def test_prepare_sandbox_rejects_wheel_requiring_newer_glibc(
    tmp_path: Path,
    _linux_bootstrap: None,
    monkeypatch: pytest.MonkeyPatch,
    runtime_minor: int,
) -> None:
    monkeypatch.setattr(bootstrap, "_live_glibc_version", lambda: (2, 36))
    monkeypatch.setattr(bootstrap, "_runtime_cpython_version", lambda: (3, runtime_minor))
    session, bootstrap_path = _write_session(
        tmp_path,
        _archive(
            {
                "package-1.0.dist-info/WHEEL": (
                    (
                        "Wheel-Version: 1.0\n"
                        f"Tag: cp3{runtime_minor}-abi3-manylinux_2_39_x86_64\n"
                    ).encode()
                )
            }
        ),
    )

    with pytest.raises(bootstrap.BootstrapFailure) as error:
        bootstrap.prepare_sandbox(
            session,
            application_directory=tmp_path / "app",
            bootstrap_path=bootstrap_path,
        )

    assert error.value.code == "glibc_abi_mismatch"
    assert not (session / "manifest.json").exists()


def test_prepare_sandbox_rejects_unsupported_protocol(
    tmp_path: Path,
    _linux_bootstrap: None,
) -> None:
    session, bootstrap_path = _write_session(
        tmp_path,
        _archive({"function_app.py": b"agent"}),
        protocol_version="unknown",
    )

    with pytest.raises(bootstrap.BootstrapFailure, match="protocol"):
        bootstrap.prepare_sandbox(
            session,
            application_directory=tmp_path / "app",
            bootstrap_path=bootstrap_path,
        )

    assert not (session / "manifest.json").exists()


def test_matching_content_digest_skips_reextract_on_a_later_boot(
    tmp_path: Path,
    _linux_bootstrap: None,
) -> None:
    session, bootstrap_path = _write_session(tmp_path, _archive({"function_app.py": b"agent"}))
    application = tmp_path / "app"
    bootstrap.prepare_sandbox(
        session,
        application_directory=application,
        bootstrap_path=bootstrap_path,
    )
    (application / "retained.txt").write_text("retained", encoding="utf-8")

    bootstrap.prepare_sandbox(
        session,
        application_directory=application,
        bootstrap_path=bootstrap_path,
    )

    assert (application / "retained.txt").read_text(encoding="utf-8") == "retained"


def test_main_writes_a_typed_error_report_on_permanent_failure(tmp_path: Path) -> None:
    session, _ = _write_session(tmp_path, _archive({"function_app.py": b"agent"}))
    (session / "content" / "app.sha256").write_text("sha256:" + ("0" * 64) + "\n", encoding="ascii")

    result = bootstrap.main(
        [
            "--session-directory",
            str(session),
            "--journal-root",
            str(tmp_path),
            "--application-directory",
            str(tmp_path / "app"),
        ]
    )

    assert result == bootstrap.EX_CONFIG
    report = json.loads((session / "bootstrap.error.json").read_text(encoding="utf-8"))
    assert report["code"] == "content_digest_mismatch"
    assert report["permanent"] is True


def test_bootstrap_publishes_protocol_before_manifest(
    tmp_path: Path,
    _linux_bootstrap: None,
) -> None:
    session, bootstrap_path = _write_session(
        tmp_path,
        _archive({"function_app.py": b"agent"}),
    )
    journal = tmp_path / "journal"

    context = bootstrap.prepare_sandbox(
        session,
        journal_directory=journal,
        application_directory=tmp_path / "app",
        bootstrap_path=bootstrap_path,
    )

    protocol = json.loads((journal / "protocol.json").read_text(encoding="utf-8"))
    assert protocol["capabilities"] == dict(context.capabilities)
    assert (session / "manifest.json").exists()
