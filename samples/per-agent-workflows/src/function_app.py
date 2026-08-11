import os
from pathlib import Path

import azure_functions_agents
from azure_functions_agents import create_function_app

expected_root = os.environ.get("AZURE_FUNCTIONS_AGENTS_EXPECTED_ROOT")
if expected_root:
    runtime_file = Path(azure_functions_agents.__file__).resolve()
    if not runtime_file.is_relative_to(Path(expected_root).resolve()):
        raise RuntimeError(
            "azure_functions_agents was not imported from the verifier's current checkout"
        )

app = create_function_app()
