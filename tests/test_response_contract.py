from __future__ import annotations

import jsonschema
import pytest

from azure_functions_agents.response_contract import (
    ResponseSchemaValidationError,
    validate_response_contract,
)


def test_invalid_response_schema_uses_shared_validation_error() -> None:
    with pytest.raises(ResponseSchemaValidationError) as raised:
        validate_response_contract('{"message":"ok"}', {"type": 123})

    assert raised.value.details
    assert isinstance(raised.value.__cause__, jsonschema.SchemaError)