"""Install E2E dependencies into each Function app's customer dependency path.

This script runs in Linux CI. It installs one shared package tree and symlinks it
into each app to avoid repeatedly installing the same dependencies.
"""

from __future__ import annotations

import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path


def _load_copilot_env_var() -> str:
    repo_root = Path(__file__).resolve().parents[2]
    env_module = runpy.run_path(repo_root / "src" / "azure_functions_agents" / "config" / "env.py")
    return str(env_module["EnvVar"].ENABLE_COPILOT)


COPILOT_ENV_VAR = _load_copilot_env_var()


def _link_app_dependencies(repo_root: Path, shared_packages: Path) -> None:
    for search_root in (repo_root / "tests" / "endtoend" / "apps", repo_root / "samples"):
        for host_json in search_root.rglob("host.json"):
            dependency_path = host_json.parent / ".python_packages" / "lib" / "site-packages"
            dependency_path.parent.mkdir(parents=True, exist_ok=True)
            if dependency_path.is_symlink():
                dependency_path.unlink()
            elif dependency_path.exists():
                raise FileExistsError(f"dependency path already exists: {dependency_path}")
            dependency_path.symlink_to(shared_packages, target_is_directory=True)


def _install_requirement(repo_root: Path) -> str:
    enabled = os.environ.get(COPILOT_ENV_VAR, "").strip().lower() in {"1", "true"}
    if enabled:
        return f"{repo_root}[copilot]"
    return str(repo_root)


def _reset_shared_dependencies(shared_packages: Path) -> None:
    if shared_packages.is_symlink():
        shared_packages.unlink()
    elif shared_packages.exists():
        shutil.rmtree(shared_packages)
    shared_packages.parent.mkdir(parents=True, exist_ok=True)


def _install_shared_dependencies(repo_root: Path, shared_packages: Path) -> None:
    _reset_shared_dependencies(shared_packages)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            f"--target={shared_packages}",
            _install_requirement(repo_root),
        ],
        check=True,
    )


def main() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    shared_packages = repo_root / ".e2e-python-packages" / "lib" / "site-packages"
    _install_shared_dependencies(repo_root, shared_packages)
    _link_app_dependencies(repo_root, shared_packages)


if __name__ == "__main__":
    main()
