from __future__ import annotations

import pytest

from azure_functions_agents.strict_json import DuplicateJsonKeyError, decode_json_object


def test_decode_json_object_rejects_duplicate_keys_without_changing_valid_values() -> None:
    assert decode_json_object(b'{"key":"value"}') == {"key": "value"}

    with pytest.raises(DuplicateJsonKeyError):
        decode_json_object(b'{"key":"first","key":"second"}')
