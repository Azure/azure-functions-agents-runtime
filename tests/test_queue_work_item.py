from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

SAMPLE_SRC = Path(__file__).resolve().parents[1] / "samples/hosted-skill-remediation/src"


@pytest.fixture
def process_body(monkeypatch):
    name = "_test_queue_work_item"
    spec = importlib.util.spec_from_file_location(name, SAMPLE_SRC / "queue_work_item.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module.process_work_item_body


@pytest.mark.parametrize(("values", "total"), [
    ([1, 2, 3], 6.0), ([1.5, -2], -0.5), ([0], 0.0),
    ([1e16, 1, -1e16], 1.0), ([1e-300, 1e-300], 2e-300),
])
def test_valid_work_item_computes_finite_sum(process_body, values, total):
    body = json.dumps({"operation": "sum", "values": values}).encode("utf-8")
    assert process_body(body) == total


@pytest.mark.parametrize("body", [
    b'{"operation":"sum","values":[1,2,3]}}',
    b'{"operation":"sum","values":[1,2,3],}',
    b'{"operation":"sum","values":[1,2,3,]}',
    b'{operation:"sum","values":[1]}',
    b"", b"not JSON",
])
def test_json_syntax_is_not_repaired(process_body, body):
    with pytest.raises(json.JSONDecodeError):
        process_body(body)


@pytest.mark.parametrize("payload", [
    {"operation": "sum", "values": [1], "unfamiliar": "preserve me"},
    {"operation": "SUM", "values": [1]},
    {"operation": "average", "values": [1]},
    {"operation": "sum", "values": []},
    {"operation": "sum", "values": [True]},
    {"operation": "sum", "values": [False]},
    {"operation": "sum", "values": ["1"]},
    {"operation": "sum", "values": [None]},
    {"operation": "sum", "values": [[1]]},
    {"operation": "sum", "values": {"0": 1}},
    {"operation": "sum", "values": None},
    {"operation": "sum"},
    {"values": [1]},
    [], None, "sum", 1, True,
])
def test_business_contract_is_strict(process_body, payload):
    with pytest.raises(ValidationError):
        process_body(json.dumps(payload).encode("utf-8"))


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
def test_nonfinite_numbers_are_rejected(process_body, number):
    with pytest.raises(ValidationError):
        process_body(f'{{"operation":"sum","values":[{number}]}}'.encode())


@pytest.mark.parametrize("body", [
    b'{"operation":"sum","operation":"sum","values":[1]}',
    b'{"operation":"average","operation":"sum","values":[1]}',
    b'{"operation":"sum","values":[100],"values":[1]}',
    b'{"operation":"sum","values":[1],"val\\u0075es":[2]}',
    b'{"operation":"sum","values":[1],"unfamiliar":{"x":1,"x":2}}',
])
def test_duplicate_json_keys_are_rejected(process_body, body):
    with pytest.raises(ValueError, match="Duplicate JSON keys"):
        process_body(body)


@pytest.mark.parametrize("body", [
    b'{"operation":"sum","values":[1],"unfamiliar":"\xff"}',
    '{"operation":"sum","values":[1]}'.encode("utf-16"),
])
def test_body_must_be_utf8(process_body, body):
    with pytest.raises(UnicodeDecodeError):
        process_body(body)


def test_unknown_unicode_property_is_not_dropped(process_body):
    body = json.dumps(
        {"operation": "sum", "values": [1], "\u00e9tiquette": "synthetic"}, ensure_ascii=False,
    ).encode("utf-8")
    with pytest.raises(ValidationError) as failure:
        process_body(body)
    assert failure.value.errors()[0]["type"] == "extra_forbidden"
    assert failure.value.errors()[0]["loc"] == ("\u00e9tiquette",)


def test_finite_inputs_with_overflowing_sum_are_rejected(process_body):
    with pytest.raises(ValueError, match="sum must be finite"):
        process_body(b'{"operation":"sum","values":[1e308,1e308]}')