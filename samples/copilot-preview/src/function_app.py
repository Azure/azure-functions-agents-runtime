from pathlib import Path

from azure_functions_agents import create_function_app

app = create_function_app(Path(__file__).resolve().parent)
