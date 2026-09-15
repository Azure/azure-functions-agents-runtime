from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest

from azure_functions_agents.experimental.durable_chat_execution_observer import (
    DurableChatExecutionObserver,
)
from azure_functions_agents.experimental.durable_chat_journal import (
    DurableChatObserver,
)
from azure_functions_agents.experimental.durable_chat_protocol import (
    DurableChatEnqueueResultV1,
    DurableChatModelProducerV1,
    DurableChatObservationBatchV1,
    DurableChatObservationHealthV1,
    DurableChatObservationV1,
    DurableChatPublicationDisposition,
    DurableChatPublicationResultV1,
)


def _producer() -> DurableChatModelProducerV1:
    return DurableChatModelProducerV1(step_index=0, observation_epoch=1)


class _FailingSink:
    def try_enqueue(
        self,
        *,
        observation: DurableChatObservationV1,
    ) -> DurableChatEnqueueResultV1:
        del observation
        raise OSError("storage failure with private assistant content")


class _MalformedSink:
    def try_enqueue(self, **_kwargs: object) -> object:
        return None


def test_observation_capture_failure_is_sanitized_and_never_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    observer = DurableChatExecutionObserver(
        sink=_FailingSink(),
        run_id="run-1",
        session_id="session-1",
    )

    with caplog.at_level(logging.WARNING):
        observer.model_attempt_started(_producer())
        observer.assistant_text(_producer(), "private assistant content")
        observer.model_attempt_completed(_producer())

    assert "private assistant content" not in caplog.text
    assert "error_type=OSError" in caplog.text


def test_malformed_observer_boundary_never_reaches_execution() -> None:
    observer = DurableChatExecutionObserver(
        sink=_MalformedSink(),  # type: ignore[arg-type]
        run_id="run-1",
        session_id="session-1",
    )

    observer.model_attempt_started(_producer())
    observer.assistant_text(_producer(), "private assistant content")
    observer.model_attempt_completed(_producer())


class _SlowJournal:
    def __init__(self) -> None:
        self.publish_started = asyncio.Event()
        self.release_publish = asyncio.Event()
        self.published_revision = 0

    async def publish(
        self,
        *,
        run_id: str,
        expected_published_revision: int,
        batch: DurableChatObservationBatchV1,
        deadline: datetime,
    ) -> DurableChatPublicationResultV1:
        del deadline
        assert expected_published_revision == self.published_revision
        self.publish_started.set()
        await self.release_publish.wait()
        self.published_revision += 1
        return DurableChatPublicationResultV1(
            run_id=run_id,
            disposition=DurableChatPublicationDisposition.PUBLISHED,
            published_revision=self.published_revision,
            through_sequence=len(batch.observations),
            health=DurableChatObservationHealthV1(),
        )


@pytest.mark.asyncio
async def test_slow_observer_publication_never_blocks_foreground_execution() -> None:
    journal = _SlowJournal()
    sink = DurableChatObserver(journal=journal, run_id="run-1")
    sink.start()
    observer = DurableChatExecutionObserver(
        sink=sink,
        drainer=sink,
        run_id="run-1",
        session_id="session-1",
    )

    observer.model_attempt_started(_producer())
    observer.assistant_text(_producer(), "first text")
    await asyncio.wait_for(journal.publish_started.wait(), timeout=1)

    async def complete_model_attempt() -> str:
        observer.assistant_text(_producer(), "second text")
        observer.model_attempt_completed(_producer())
        return "model completed"

    assert await asyncio.wait_for(complete_model_attempt(), timeout=0.05) == (
        "model completed"
    )

    journal.release_publish.set()
    await observer.drain(deadline=datetime.now(UTC) + timedelta(seconds=1))
