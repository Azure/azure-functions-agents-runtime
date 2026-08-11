from pathlib import Path

from azure_functions_agents._trigger_support import is_supported_trigger_type
from azure_functions_agents.config.loader import load_agent_specs

SAMPLE_SRC = (
    Path(__file__).resolve().parents[1] / "samples" / "outlook-reply-agent" / "src"
)


def test_outlook_reply_sample_uses_supported_connector_trigger() -> None:
    [agent] = load_agent_specs(SAMPLE_SRC, strict=True)

    assert agent.trigger is not None
    assert agent.trigger.type == "connector_trigger"
    assert is_supported_trigger_type(agent.trigger.type)
