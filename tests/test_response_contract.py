from __future__ import annotations

import jsonschema
import pytest

from azure_functions_agents.config.schema import ResolvedAgent
from azure_functions_agents.response_contract import (
    ResponseSchemaValidationError,
    response_format_instructions,
    validate_response_contract,
)


def test_empty_response_schema_still_adds_json_instructions() -> None:
    resolved = ResolvedAgent.model_construct(response_example=None, response_schema={})

    instructions = response_format_instructions(resolved)

    assert len(instructions) == 1
    assert "conform to this JSON Schema" in instructions[0]


def test_invalid_response_schema_uses_shared_validation_error() -> None:
    with pytest.raises(ResponseSchemaValidationError) as raised:
        validate_response_contract('{"message":"ok"}', {"type": 123})

    assert raised.value.details
    assert isinstance(raised.value.__cause__, jsonschema.SchemaError)