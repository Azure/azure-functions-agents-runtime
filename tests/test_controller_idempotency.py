from __future__ import annotations

from datetime import UTC, datetime, timedelta

from azure_functions_agents.controller.idempotency import build_idempotency_attempt
from azure_functions_agents.session_state import (
    AppIdentity,
    DurableOwnerIdempotencyRecord,
    FunctionAppOwnerContext,
    OwnerIdempotencyRowKey,
    owner_partition,
    parse_row_key,
)


def test_logical_attempt_hash_uses_only_agent_prompt_and_timeout() -> None:
    first = build_idempotency_attempt(
        agent_slug="main",
        prompt="hello",
        timeout=30.0,
        idempotency_key="caller-key",
    )
    second = build_idempotency_attempt(
        agent_slug="main",
        prompt="hello",
        timeout=30.0,
        idempotency_key="caller-key",
    )
    changed = build_idempotency_attempt(
        agent_slug="main",
        prompt="different",
        timeout=30.0,
        idempotency_key="caller-key",
    )

    assert first is not None
    assert second == first
    assert changed is not None
    assert changed.key_hash == first.key_hash
    assert changed.request_hash != first.request_hash


def test_raw_idempotency_key_is_not_stored_on_attempt() -> None:
    attempt = build_idempotency_attempt(
        agent_slug="main",
        prompt="hello",
        timeout=None,
        idempotency_key="private-caller-key",
    )

    assert attempt is not None
    assert "private-caller-key" not in repr(attempt)
    assert "private-caller-key" not in attempt.key_hash


def test_owner_idempotency_record_uses_a_distinct_durable_row_key() -> None:
    now = datetime.now(UTC)
    owner = FunctionAppOwnerContext.create(
        AppIdentity.create(
            subscription_id="11111111-2222-3333-4444-555555555555",
            site_name="agent-app",
        ),
        "main",
    )
    record = DurableOwnerIdempotencyRecord.create(
        owner_partition=owner_partition(owner),
        idempotency_hash="a" * 64,
        request_hash="b" * 64,
        session_id="session-1",
        run_id="run-1",
        expires_at=now + timedelta(hours=1),
        created_at=now,
    )

    assert isinstance(parse_row_key(str(record.row_key)), OwnerIdempotencyRowKey)
    assert DurableOwnerIdempotencyRecord.from_table_entity(record.to_table_entity()) == record
