"""Install E2E dependencies into each Function app's customer dependency path."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


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


def main() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    shared_packages = repo_root / ".e2e-python-packages" / "lib" / "site-packages"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            f"--target={shared_packages}",
            "-e",
            str(repo_root),
        ],
        check=True,
    )
    _link_app_dependencies(repo_root, shared_packages)


if __name__ == "__main__":
    main()
