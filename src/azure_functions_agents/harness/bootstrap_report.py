"""Typed, redacted bootstrap-failure reports read by the controller."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from ..strict_json import DuplicateJsonKeyError, decode_json_object

type _ErrorCode = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
type _ErrorMessage = Annotated[str, StringConstraints(min_length=1, max_length=240)]


class BootstrapReportError(Exception):
    """A sandbox bootstrap report was malformed or unsafe to consume."""


class _BootstrapErrorPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    code: _ErrorCode
    message: _ErrorMessage
    permanent: bool


@dataclass(frozen=True, slots=True)
class BootstrapErrorReport:
    """A bounded, non-secret error published when sandbox bootstrap cannot start."""

    code: str
    message: str
    permanent: bool

    @classmethod
    def create(cls, *, code: str, message: str, permanent: bool) -> BootstrapErrorReport:
        try:
            payload = _BootstrapErrorPayload.model_validate(
                {"code": code, "message": message, "permanent": permanent}
            )
        except ValidationError:
            raise BootstrapReportError("Sandbox bootstrap error report is invalid.") from None
        return cls(code=payload.code, message=payload.message, permanent=payload.permanent)

    def to_bytes(self) -> bytes:
        """Render the stable report payload written by the bootstrap process."""

        payload = {
            "code": self.code,
            "message": self.message,
            "permanent": self.permanent,
        }
        return f"{json.dumps(payload, sort_keys=True, separators=(',', ':'))}\n".encode()


def parse_bootstrap_error_report(payload: bytes | str) -> BootstrapErrorReport:
    """Strictly parse an untrusted bootstrap report without exposing its contents."""

    try:
        decoded = decode_json_object(payload)
        model = _BootstrapErrorPayload.model_validate(decoded)
    except (
        DuplicateJsonKeyError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValidationError,
    ):
        raise BootstrapReportError("Sandbox bootstrap error report is invalid.") from None
    return BootstrapErrorReport.create(
        code=model.code,
        message=model.message,
        permanent=model.permanent,
    )
