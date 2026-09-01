"""Tests for the E2E dependency setup script."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_script() -> ModuleType:
    script = Path(__file__).resolve().parents[1] / "eng" / "scripts" / "install_e2e_dependencies.py"
    spec = importlib.util.spec_from_file_location("install_e2e_dependencies", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_link_app_dependencies_links_each_function_app(
    tmp_path: Path,
    monkeypatch,
) -> None:
    setup = _load_script()
    app_roots = (
        tmp_path / "tests" / "endtoend" / "apps" / "test-app",
        tmp_path / "samples" / "sample-app" / "src",
    )
    for app_root in app_roots:
        app_root.mkdir(parents=True)
        (app_root / "host.json").touch()

    links: list[tuple[Path, Path, bool]] = []

    def record_link(path: Path, target: Path, *, target_is_directory: bool) -> None:
        links.append((path, target, target_is_directory))

    monkeypatch.setattr(Path, "symlink_to", record_link)
    shared_packages = tmp_path / ".e2e-python-packages" / "lib" / "site-packages"

    setup._link_app_dependencies(tmp_path, shared_packages)

    assert links == [
        (
            app_root / ".python_packages" / "lib" / "site-packages",
            shared_packages,
            True,
        )
        for app_root in app_roots
    ]
