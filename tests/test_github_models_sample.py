import json
from pathlib import Path

from azure_functions_agents import create_function_app

SAMPLE_ROOT = Path(__file__).resolve().parents[1] / "samples" / "github-models-chat"
SAMPLE_SRC = SAMPLE_ROOT / "src"


def test_github_models_chat_sample_composes_builtin_endpoints() -> None:
    app = create_function_app(app_root=SAMPLE_SRC)

    names = {function.get_function_name() for function in app.get_functions()}

    assert {
        "agent_main_builtin_chat",
        "agent_main_builtin_chat_page",
        "agent_main_builtin_chatstream",
    } <= names


def test_github_models_chat_sample_uses_dedicated_provider_settings() -> None:
    settings = json.loads(
        (SAMPLE_SRC / "local.settings.template.json").read_text(encoding="utf-8")
    )["Values"]

    assert settings["AZURE_FUNCTIONS_AGENTS_PROVIDER"] == "github"
    assert settings["GITHUB_MODELS_TOKEN"] == "<github-token>"
    assert settings["GITHUB_MODELS_MODEL"] == "openai/gpt-4.1-mini"