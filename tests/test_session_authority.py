from datetime import UTC, datetime, timedelta

import pytest

from azure_functions_agents.durable._session_authority import (
    MAX_ENTITY_STATE_BYTES,
    ActiveTurn,
    AdmissionAccepted,
    AdmissionBusy,
    AdmissionCommand,
    AdmissionConflict,
    AdmissionDeleted,
    AdmissionGenerationMismatch,
    AdmissionReceipt,
    CompleteTurnCommand,
    DeleteSessionCommand,
    GenerationFenceStatus,
    SessionAuthorityContractError,
    SessionAuthorityState,
    SessionCreation,
    StartAcknowledged,
    StartRejected,
    StartTurnCommand,
    TerminalCommitAccepted,
    TerminalCommitRejected,
    TerminalTurn,
    TurnTerminalStatus,
    acknowledge_turn_start,
    admit_turn,
    complete_turn,
    create_session_authority,
    decode_session_authority_state,
    derive_request_fingerprint,
    derive_request_id,
    derive_run_id,
    encode_session_authority_state,
    generation_fence_status,
    mark_session_deleted,
)
from azure_functions_agents.harness._checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    ContentRef,
    IntegrityMetadata,
    StorageKind,
)

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
OWNER_SCOPE_DIGEST = "a" * 64


def _content_ref(seed: str = "a") -> ContentRef:
    digest = seed * 64
    return ContentRef(
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        storage_kind=StorageKind.BLOB,
        opaque_content_key=f"content/{seed}",
        content_digest=digest,
        content_length=7,
        content_type="application/json",
        created_at=NOW,
        expires_at=NOW + timedelta(days=1),
        integrity_metadata=IntegrityMetadata(
            digest=digest,
            content_length=7,
        ),
    )


def _state() -> SessionAuthorityState:
    return create_session_authority(
        SessionCreation(
            backend_name="storage",
            task_hub_name="agents",
            agent_slug="weather",
            owner_scope_digest=OWNER_SCOPE_DIGEST,
            entry_id="builtin-chat",
            session_id="session-1",
            creation_nonce="nonce-1",
        )
    )


def _admission(
    *,
    key: str = "request-key",
    body: bytes = b'{"prompt":"weather"}',
    generation: int = 1,
) -> AdmissionCommand:
    return AdmissionCommand(
        idempotency_key=key,
        request_fingerprint=derive_request_fingerprint("builtin-chat", body),
        request_ref=_content_ref(),
        turn_generation=generation,
        admitted_at=NOW,
    )


def _accepted_turn() -> tuple[SessionAuthorityState, AdmissionAccepted]:
    state, outcome = admit_turn(_state(), _admission())
    assert isinstance(outcome, AdmissionAccepted)
    return state, outcome


def test_session_authority_round_trips_strict_bounded_state() -> None:
    state = _state()

    payload = encode_session_authority_state(state)

    assert decode_session_authority_state(payload) == state
    assert len(payload) < MAX_ENTITY_STATE_BYTES


def test_session_authority_decode_rejects_unknown_and_duplicate_fields() -> None:
    payload = encode_session_authority_state(_state())
    unknown = payload[:-1] + b',"unexpected":true}'
    duplicate = payload[:-1] + b',"session_id":"other"}'

    with pytest.raises(SessionAuthorityContractError):
        decode_session_authority_state(unknown)
    with pytest.raises(SessionAuthorityContractError):
        decode_session_authority_state(duplicate)


def test_session_authority_decode_rejects_oversized_state_before_parsing() -> None:
    with pytest.raises(SessionAuthorityContractError, match="size limit"):
        decode_session_authority_state(b" " * (MAX_ENTITY_STATE_BYTES + 1))


def test_state_capacity_uses_durable_wire_json_size() -> None:
    state = _state()
    receipts = []
    for generation in range(1, 652):
        idempotency_key = f"request-{generation}"
        request_id = derive_request_id(
            state.session_incarnation_id,
            idempotency_key,
        )
        receipts.append(
            AdmissionReceipt(
                idempotency_key=idempotency_key,
                request_fingerprint=derive_request_fingerprint(
                    "builtin-chat",
                    f'{{"generation":{generation}}}'.encode(),
                ),
                request_id=request_id,
                run_id=derive_run_id(request_id),
                turn_generation=generation,
                request_ref=_content_ref(),
                admitted_at=NOW,
            )
        )
    last_receipt = receipts[-1]
    oversized_on_wire = SessionAuthorityState.model_validate(
        state.model_dump(mode="python")
        | {
            "next_turn_generation": 652,
            "admission_receipts": tuple(receipts),
            "last_terminal_turn": TerminalTurn(
                request_id=last_receipt.request_id,
                run_id=last_receipt.run_id,
                turn_generation=last_receipt.turn_generation,
                status=TurnTerminalStatus.COMPLETED,
                terminal_at=NOW + timedelta(minutes=5),
                result_ref=_content_ref("b"),
            ),
        },
        strict=True,
    )

    assert len(oversized_on_wire.model_dump_json().encode("utf-8")) < (
        MAX_ENTITY_STATE_BYTES
    )
    with pytest.raises(SessionAuthorityContractError, match="size limit"):
        encode_session_authority_state(oversized_on_wire)


def test_session_incarnation_changes_when_any_identity_component_changes() -> None:
    original = _state()
    changed = create_session_authority(
        SessionCreation(
            backend_name="storage",
            task_hub_name="agents",
            agent_slug="weather",
            owner_scope_digest=OWNER_SCOPE_DIGEST,
            entry_id="authored-http",
            session_id="session-1",
            creation_nonce="nonce-1",
        )
    )

    assert original.session_incarnation_id != changed.session_incarnation_id


def test_admission_accepts_once_and_replays_the_original_receipt() -> None:
    state = _state()

    accepted_state, first = admit_turn(state, _admission())
    replayed_state, replay = admit_turn(accepted_state, _admission())

    assert isinstance(first, AdmissionAccepted)
    assert first.replayed is False
    assert isinstance(replay, AdmissionAccepted)
    assert replay.replayed is True
    assert replay.receipt == first.receipt
    assert replayed_state is accepted_state
    assert accepted_state.next_turn_generation == 2
    assert accepted_state.active_turn == ActiveTurn(
        request_id=first.receipt.request_id,
        run_id=first.receipt.run_id,
        turn_generation=1,
        admitted_at=NOW,
    )


def test_admission_rejects_same_key_with_changed_fingerprint() -> None:
    state, accepted = _accepted_turn()

    unchanged, outcome = admit_turn(
        state,
        _admission(body=b'{"prompt":"different"}'),
    )

    assert isinstance(outcome, AdmissionConflict)
    assert outcome.request_id == accepted.receipt.request_id
    assert unchanged is state


def test_admission_rejects_different_request_while_turn_is_active() -> None:
    state, accepted = _accepted_turn()

    unchanged, outcome = admit_turn(
        state,
        _admission(key="second-key", generation=2),
    )

    assert isinstance(outcome, AdmissionBusy)
    assert outcome.active_run_id == accepted.receipt.run_id
    assert unchanged is state


def test_admission_rejects_content_prepared_for_stale_generation() -> None:
    state = _state()

    unchanged, outcome = admit_turn(state, _admission(generation=2))

    assert isinstance(outcome, AdmissionGenerationMismatch)
    assert outcome.expected_turn_generation == 1
    assert unchanged is state


def test_start_acknowledgement_binds_deadline_once() -> None:
    state, accepted = _accepted_turn()
    command = StartTurnCommand(
        request_id=accepted.receipt.request_id,
        run_id=accepted.receipt.run_id,
        turn_generation=accepted.receipt.turn_generation,
        started_at=NOW + timedelta(seconds=1),
        deadline=NOW + timedelta(minutes=5),
    )

    started_state, first = acknowledge_turn_start(state, command)
    replayed_state, replay = acknowledge_turn_start(started_state, command)
    unchanged, conflict = acknowledge_turn_start(
        started_state,
        command.model_copy(update={"deadline": NOW + timedelta(minutes=6)}),
    )

    assert isinstance(first, StartAcknowledged)
    assert first.replayed is False
    assert isinstance(replay, StartAcknowledged)
    assert replay.replayed is True
    assert replayed_state is started_state
    assert isinstance(conflict, StartRejected)
    assert conflict.reason == "deadline_conflict"
    assert unchanged is started_state


def test_generation_fence_rejects_stale_and_mismatched_work() -> None:
    state, accepted = _accepted_turn()
    receipt = accepted.receipt

    assert (
        generation_fence_status(
            state,
            request_id=receipt.request_id,
            run_id=receipt.run_id,
            turn_generation=receipt.turn_generation,
        )
        is GenerationFenceStatus.ACTIVE
    )
    assert (
        generation_fence_status(
            state,
            request_id=receipt.request_id,
            run_id=receipt.run_id,
            turn_generation=receipt.turn_generation + 1,
        )
        is GenerationFenceStatus.STALE_GENERATION
    )
    assert (
        generation_fence_status(
            state,
            request_id="b" * 64,
            run_id=receipt.run_id,
            turn_generation=receipt.turn_generation,
        )
        is GenerationFenceStatus.IDENTITY_MISMATCH
    )


@pytest.mark.parametrize(
    ("status", "result_ref", "error_code"),
    [
        (TurnTerminalStatus.COMPLETED, _content_ref("b"), None),
        (TurnTerminalStatus.FAILED, None, "provider_failure"),
        (TurnTerminalStatus.CANCELLED, None, "cancelled"),
        (TurnTerminalStatus.TIMED_OUT, None, "deadline_exceeded"),
    ],
)
def test_terminal_commit_releases_slot_for_next_admission(
    status: TurnTerminalStatus,
    result_ref: ContentRef | None,
    error_code: str | None,
) -> None:
    state, accepted = _accepted_turn()
    command = CompleteTurnCommand(
        request_id=accepted.receipt.request_id,
        run_id=accepted.receipt.run_id,
        turn_generation=accepted.receipt.turn_generation,
        status=status,
        terminal_at=NOW + timedelta(minutes=5),
        result_ref=result_ref,
        error_code=error_code,
    )

    terminal_state, first = complete_turn(state, command)
    replayed_state, replay = complete_turn(terminal_state, command)
    next_state, next_admission = admit_turn(
        terminal_state,
        _admission(key="second-key", generation=2),
    )

    assert isinstance(first, TerminalCommitAccepted)
    assert first.replayed is False
    assert isinstance(replay, TerminalCommitAccepted)
    assert replay.replayed is True
    assert replayed_state is terminal_state
    assert terminal_state.active_turn is None
    assert terminal_state.last_terminal_turn == first.terminal_turn
    assert isinstance(next_admission, AdmissionAccepted)
    assert next_admission.receipt.turn_generation == 2
    assert next_state.active_turn is not None


def test_terminal_commit_rejects_changed_replay() -> None:
    state, accepted = _accepted_turn()
    command = CompleteTurnCommand(
        request_id=accepted.receipt.request_id,
        run_id=accepted.receipt.run_id,
        turn_generation=accepted.receipt.turn_generation,
        status=TurnTerminalStatus.FAILED,
        terminal_at=NOW + timedelta(minutes=5),
        error_code="provider_failure",
    )
    terminal_state, _ = complete_turn(state, command)

    unchanged, outcome = complete_turn(
        terminal_state,
        command.model_copy(update={"error_code": "different_failure"}),
    )

    assert isinstance(outcome, TerminalCommitRejected)
    assert outcome.reason == "terminal_conflict"
    assert unchanged is terminal_state


def test_state_rejects_obsolete_active_generation() -> None:
    first_state, first = _accepted_turn()
    terminal_state, _ = complete_turn(
        first_state,
        CompleteTurnCommand(
            request_id=first.receipt.request_id,
            run_id=first.receipt.run_id,
            turn_generation=1,
            status=TurnTerminalStatus.COMPLETED,
            terminal_at=NOW + timedelta(minutes=5),
            result_ref=_content_ref("b"),
        ),
    )
    second_state, second = admit_turn(
        terminal_state,
        _admission(key="second-key", generation=2),
    )
    assert isinstance(second, AdmissionAccepted)

    corrupted = second_state.model_dump(mode="python")
    corrupted["active_turn"] = first_state.active_turn

    with pytest.raises(ValueError, match="latest accepted generation"):
        SessionAuthorityState.model_validate(corrupted, strict=True)


def test_state_rejects_noncontiguous_generation_and_next_generation_drift() -> None:
    state, accepted = _accepted_turn()
    corrupted_receipt = accepted.receipt.model_copy(update={"turn_generation": 2})

    with pytest.raises(ValueError, match="contiguous"):
        SessionAuthorityState.model_validate(
            state.model_dump(mode="python")
            | {
                "admission_receipts": (corrupted_receipt,),
                "active_turn": state.active_turn.model_copy(
                    update={"turn_generation": 2}
                )
                if state.active_turn is not None
                else None,
                "next_turn_generation": 3,
            },
            strict=True,
        )

    with pytest.raises(ValueError, match="next turn generation"):
        SessionAuthorityState.model_validate(
            state.model_dump(mode="python") | {"next_turn_generation": 3},
            strict=True,
        )


def test_state_rejects_tombstone_with_unrelated_identity() -> None:
    deleted_state, _ = mark_session_deleted(
        _state(),
        DeleteSessionCommand(
            idempotency_key="delete-key",
            requested_at=NOW,
        ),
    )
    assert deleted_state.tombstone is not None
    corrupted = deleted_state.model_dump(mode="python")
    corrupted["tombstone"] = deleted_state.tombstone.model_copy(
        update={"deletion_id": "f" * 64}
    )

    with pytest.raises(ValueError, match="tombstone identity"):
        SessionAuthorityState.model_validate(corrupted, strict=True)


def test_deletion_is_stable_and_immediately_revokes_admission() -> None:
    state, accepted = _accepted_turn()
    command = DeleteSessionCommand(
        idempotency_key="delete-key",
        requested_at=NOW + timedelta(seconds=2),
    )

    deleted_state, first = mark_session_deleted(state, command)
    replayed_state, replay = mark_session_deleted(
        deleted_state,
        command.model_copy(update={"idempotency_key": "another-key"}),
    )
    unchanged, admission = admit_turn(
        deleted_state,
        _admission(key="second-key", generation=2),
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.tombstone == first.tombstone
    assert replayed_state is deleted_state
    assert deleted_state.active_turn is None
    assert isinstance(admission, AdmissionDeleted)
    assert admission.deletion_id == first.tombstone.deletion_id
    assert unchanged is deleted_state
    assert (
        generation_fence_status(
            deleted_state,
            request_id=accepted.receipt.request_id,
            run_id=accepted.receipt.run_id,
            turn_generation=accepted.receipt.turn_generation,
        )
        is GenerationFenceStatus.SESSION_DELETED
    )


def test_deletion_fences_a_later_active_generation() -> None:
    first_state, first = _accepted_turn()
    terminal_state, _ = complete_turn(
        first_state,
        CompleteTurnCommand(
            request_id=first.receipt.request_id,
            run_id=first.receipt.run_id,
            turn_generation=1,
            status=TurnTerminalStatus.COMPLETED,
            terminal_at=NOW + timedelta(minutes=5),
            result_ref=_content_ref("b"),
        ),
    )
    second_state, second = admit_turn(
        terminal_state,
        _admission(key="second-key", generation=2),
    )
    assert isinstance(second, AdmissionAccepted)

    deleted_state, outcome = mark_session_deleted(
        second_state,
        DeleteSessionCommand(
            idempotency_key="delete-key",
            requested_at=NOW + timedelta(minutes=6),
        ),
    )

    assert outcome.tombstone.fenced_turn_generation == 2
    assert deleted_state.last_terminal_turn is not None
    assert deleted_state.last_terminal_turn.turn_generation == 1
    assert decode_session_authority_state(
        encode_session_authority_state(deleted_state)
    ) == deleted_state
