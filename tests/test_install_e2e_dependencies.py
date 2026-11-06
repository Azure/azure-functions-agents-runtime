from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import Mock


def _load_module():
    script_path = Path(__file__).resolve().parents[1] / "eng" / "scripts" / "install_e2e_dependencies.py"
    spec = importlib.util.spec_from_file_location("install_e2e_dependencies", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_install_shared_dependencies_uses_base_package_by_default(monkeypatch, tmp_path: Path) -> None:
    module = _load_module()

    run = Mock()
    monkeypatch.setattr(module.subprocess, "run", run)

    repo_root = tmp_path / "repo"
    shared_packages = tmp_path / "site-packages"
    module._install_shared_dependencies(repo_root, shared_packages)

    run.assert_called_once_with(
        [
            module.sys.executable,
            "-m",
            "pip",
            "install",
            f"--target={shared_packages}",
            str(repo_root),
        ],
        check=True,
    )
    assert shared_packages.parent.is_dir()


def test_install_shared_dependencies_replaces_stale_shared_tree(monkeypatch, tmp_path: Path) -> None:
    module = _load_module()
    run = Mock()
    monkeypatch.setattr(module.subprocess, "run", run)

    repo_root = tmp_path / "repo"
    shared_packages = tmp_path / "site-packages"
    shared_packages.mkdir(parents=True)
    stale_dependency = shared_packages / "stale_package.whl"
    stale_dependency.write_text("stale")

    module._install_shared_dependencies(repo_root, shared_packages)

    assert not stale_dependency.exists()
    run.assert_called_once()
