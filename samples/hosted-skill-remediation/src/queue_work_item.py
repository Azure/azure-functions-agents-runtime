"""Deterministic validation and arithmetic for queue work items."""

import json
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat


class WorkItem(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    operation: Literal["sum"]
    values: list[FiniteFloat] = Field(min_length=1)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON keys are not allowed")
        result[key] = value
    return result


def process_work_item_body(body: bytes) -> float:
    """Parse an unchanged UTF-8 JSON body and compute its finite sum."""
    payload = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
    item = WorkItem.model_validate(payload)
    try:
        result = math.fsum(item.values)
    except OverflowError:
        raise ValueError("Work item sum must be finite") from None
    if not math.isfinite(result):
        raise ValueError("Work item sum must be finite")
    return result