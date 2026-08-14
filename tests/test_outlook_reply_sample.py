from pathlib import Path

from azure_functions_agents.config.loader import load_agent_specs
from azure_functions_agents.config.schema import TRIGGER_TYPES

SAMPLE_SRC = (
    Path(__file__).resolve().parents[1] / "samples" / "outlook-reply-agent" / "src"
)


def test_outlook_reply_sample_uses_supported_connector_trigger() -> None:
    [agent] = load_agent_specs(SAMPLE_SRC, strict=True)

    assert agent.trigger is not None
    assert agent.trigger.type == "connector_trigger"
    assert agent.trigger.type in TRIGGER_TYPES
    assert agent.trigger.args == {}
