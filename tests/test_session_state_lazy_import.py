"""Verifies the ``[aca_sandbox]`` extra (``azure-data-tables``) is never
imported as a side effect of importing ``azure_functions_agents`` or any
module on its normal startup path.

Mirrors the existing ``test_registration_import_defers_runner_loading``
pattern in ``tests/test_execution_in_lang_worker.py``: import something in a
fresh subprocess, then check ``sys.modules`` rather than trying to uninstall
a dependency mid-test-run.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _run_and_check_not_imported(import_statement: str) -> None:
    source_root = Path(__file__).resolve().parents[1] / "src"
    env = {**os.environ, "PYTHONPATH": str(source_root)}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"{import_statement}; import sys; "
            "print('azure.data.tables' in sys.modules)",
        ],
        capture_output=True,
        check=True,
        cwd=source_root.parent,
        env=env,
        text=True,
    )
    assert result.stdout.strip() == "False", result.stderr


def test_importing_the_package_never_imports_azure_data_tables() -> None:
    _run_and_check_not_imported("import azure_functions_agents")


def test_importing_session_state_never_imports_azure_data_tables() -> None:
    _run_and_check_not_imported("import azure_functions_agents.session_state")


def test_importing_the_store_and_connection_modules_never_imports_azure_data_tables() -> None:
    _run_and_check_not_imported(
        "import azure_functions_agents.session_state.store; "
        "import azure_functions_agents.session_state.connection"
    )


def test_importing_registration_auth_never_imports_azure_data_tables() -> None:
    # registration/_auth.py imports session_state.session_models directly
    # (P3a's dormant owner-context seam); confirm that path also stays lazy.
    _run_and_check_not_imported("import azure_functions_agents.registration._auth")


def test_registration_endpoints_import_never_imports_azure_data_tables() -> None:
    _run_and_check_not_imported("import azure_functions_agents.registration.endpoints")
