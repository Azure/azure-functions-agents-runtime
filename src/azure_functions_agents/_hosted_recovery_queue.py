"""Bounded textual Storage Queue input replacement without message settlement."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import NoReturn

from azure.functions import QueueMessage

from ._hosted_recovery_input import HostedSkillRecoveryError, QueueRetryArguments

MAX_QUEUE_RECOVERY_BYTES = 32 * 1024


def _body_text(body: bytes) -> str:
    if len(body) > MAX_QUEUE_RECOVERY_BYTES:
        raise ValueError("Queue recovery supports bodies up to 32 KiB")
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("Queue recovery requires a UTF-8 body") from None


class _CorrectedQueueMessage(QueueMessage):
    def __init__(self, original: QueueMessage, body: bytes) -> None:
        super().__init__(id=original.id, body=body, pop_receipt=original.pop_receipt)
        self._original = original

    @property
    def dequeue_count(self) -> int | None:
        return self._original.dequeue_count

    @property
    def expiration_time(self) -> datetime | None:
        return self._original.expiration_time

    @property
    def insertion_time(self) -> datetime | None:
        return self._original.insertion_time

    @property
    def time_next_visible(self) -> datetime | None:
        return self._original.time_next_visible


@dataclass(frozen=True)
class RecoveryQueueMessage:
    """Expose reviewed synthetic queue text, never delivery credentials, to recovery."""

    request: QueueMessage = field(repr=False)
    _text: str = field(repr=False)

    @classmethod
    def create(cls, message: QueueMessage) -> RecoveryQueueMessage:
        return cls(message, _body_text(message.get_body()))

    @property
    def retry_arguments(self) -> type[QueueRetryArguments]:
        return QueueRetryArguments

    @property
    def retry_description(self) -> str:
        return (
            "Replace the original queue message body with the supplied UTF-8 body string "
            "(at most 32 KiB). Preserve intended data and unfamiliar properties; do not invent "
            "values or drop fields to force success. Delivery metadata cannot be changed."
        )

    def diagnostic(self) -> dict[str, object]:
        return {"body": self._text}

    def corrected_arguments(self, arguments: QueueRetryArguments) -> QueueMessage:
        body = arguments.body.encode("utf-8")
        _body_text(body)
        return _CorrectedQueueMessage(self.request, body)

    def failure(self, remediation: str) -> NoReturn:
        raise HostedSkillRecoveryError(remediation) from None