from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import azure.durable_functions as df
import pytest

from azure_functions_agents.durable._session_authority import (
    AdmissionCommand,
    CompleteTurnCommand,
    DeleteSessionCommand,
    SessionCreation,
    StartTurnCommand,
    TurnTerminalStatus,
    derive_request_fingerprint,
    derive_session_entity_key,
)
from azure_functions_agents.durable._session_entity import (
    SESSION_AUTHORITY_ENTITY_NAME,
    SessionAuthorityEntityError,
    SessionAuthorityOperation,
    register_session_authority_entity,
)
from azure_functions_agents.harness._checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    ContentRef,
    IntegrityMetadata,
    StorageKind,
)

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
OWNER_SCOPE_DIGEST = "a" * 64


class _FakeEntityContext:
    def __init__(
        self,
        *,
        entity_key: str,
        operation_name: str,
        operation_input: Any = None,
        state: Any = None,
    ) -> None:
        self.entity_key = entity_key
        self.operation_name = operation_name
        self._input = operation_input
        self._state = state
        self.result: Any = None

    def get_input(self) -> Any:
        return self._input

    def get_state(self) -> Any:
        return self._state

    def set_state(self, state: Any) -> None:
        self._state = state

    def set_result(self, result: Any) -> None:
        self.result = result


def _creation(*, nonce: str = "nonce-1") -> SessionCreation:
    return SessionCreation(
        backend_name="storage",
        task_hub_name="agents",
        agent_slug="weather",
        owner_scope_digest=OWNER_SCOPE_DIGEST,
        entry_id="builtin-chat",
        session_id="session-1",
        creation_nonce=nonce,
    )


def _entity_key() -> str:
    creation = _creation()
    return derive_session_entity_key(
        backend_name=creation.backend_name,
        task_hub_name=creation.task_hub_name,
        agent_slug=creation.agent_slug,
        owner_scope_digest=creation.owner_scope_digest,
        entry_id=creation.entry_id,
        session_id=creation.session_id,
    )


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


def _registered_entity() -> Any:
    app = df.DFApp()
    register_session_authority_entity(app)
    for builder in app._function_builders:
        function = builder._function
        if function._name == SESSION_AUTHORITY_ENTITY_NAME:
            return function._func.entity_function
    raise AssertionError("session authority Entity was not registered")


def _invoke(
    entity: Any,
    *,
    operation: SessionAuthorityOperation | str,
    operation_input: Any = None,
    state: Any = None,
    entity_key: str | None = None,
) -> _FakeEntityContext:
    context = _FakeEntityContext(
        entity_key=entity_key or _entity_key(),
        operation_name=str(operation),
        operation_input=operation_input,
        state=state,
    )
    entity(context)
    return context


def test_registers_named_session_authority_entity_binding() -> None:
    app = df.DFApp()

    register_session_authority_entity(app)

    matching = [
        builder._function
        for builder in app._function_builders
        if builder._function._name == SESSION_AUTHORITY_ENTITY_NAME
    ]
    assert len(matching) == 1
    bindings = [
        binding.get_dict_repr()
        for binding in matching[0]._bindings
        if binding.get_dict_repr()["type"] == "entityTrigger"
    ]
    assert bindings == [
        {
            "direction": bindings[0]["direction"],
            "type": "entityTrigger",
            "name": "context",
            "entityName": SESSION_AUTHORITY_ENTITY_NAME,
            "durableRequiresGrpc": True,
        }
    ]


def test_create_persists_state_and_reconciles_to_original_incarnation() -> None:
    entity = _registered_entity()
    created = _invoke(
        entity,
        operation=SessionAuthorityOperation.CREATE,
        operation_input=_creation().model_dump(mode="json"),
    )

    reconciled = _invoke(
        entity,
        operation=SessionAuthorityOperation.CREATE,
        operation_input=_creation(nonce="retry-nonce").model_dump(mode="json"),
        state=created._state,
    )

    assert created.result["created"] is True
    assert reconciled.result == created.result | {"created": False}
    assert reconciled._state == created._state


def test_entity_dispatches_admission_and_terminal_transitions() -> None:
    entity = _registered_entity()
    created = _invoke(
        entity,
        operation=SessionAuthorityOperation.CREATE,
        operation_input=_creation().model_dump(mode="json"),
    )
    admission = AdmissionCommand(
        idempotency_key="request-key",
        request_fingerprint=derive_request_fingerprint(
            "builtin-chat",
            b'{"prompt":"weather"}',
        ),
        request_ref=_content_ref(),
        turn_generation=1,
        admitted_at=NOW,
    )

    admitted = _invoke(
        entity,
        operation=SessionAuthorityOperation.ADMIT,
        operation_input=admission.model_dump(mode="json"),
        state=created._state,
    )
    receipt = admitted.result["receipt"]
    started = _invoke(
        entity,
        operation=SessionAuthorityOperation.ACKNOWLEDGE_START,
        operation_input=StartTurnCommand(
            request_id=receipt["request_id"],
            run_id=receipt["run_id"],
            turn_generation=receipt["turn_generation"],
            started_at=NOW + timedelta(seconds=1),
            deadline=NOW + timedelta(minutes=5),
        ).model_dump(mode="json"),
        state=admitted._state,
    )
    completed = _invoke(
        entity,
        operation=SessionAuthorityOperation.COMPLETE,
        operation_input=CompleteTurnCommand(
            request_id=receipt["request_id"],
            run_id=receipt["run_id"],
            turn_generation=receipt["turn_generation"],
            status=TurnTerminalStatus.COMPLETED,
            terminal_at=NOW + timedelta(minutes=5),
            result_ref=_content_ref("b"),
        ).model_dump(mode="json"),
        state=started._state,
    )

    assert admitted.result["kind"] == "accepted"
    assert started.result["kind"] == "acknowledged"
    assert completed.result["kind"] == "committed"
    assert completed._state["active_turn"] is None
    assert completed._state["last_terminal_turn"]["turn_generation"] == 1


def test_entity_rejects_wrong_key_unknown_operation_and_missing_state() -> None:
    entity = _registered_entity()

    with pytest.raises(SessionAuthorityEntityError, match="key"):
        _invoke(
            entity,
            operation=SessionAuthorityOperation.CREATE,
            operation_input=_creation().model_dump(mode="json"),
            entity_key="wrong",
        )
    with pytest.raises(SessionAuthorityEntityError, match="Unsupported"):
        _invoke(entity, operation="unknown")
    with pytest.raises(SessionAuthorityEntityError, match="not been created"):
        _invoke(entity, operation=SessionAuthorityOperation.ADMIT)


def test_entity_dispatches_stable_deletion() -> None:
    entity = _registered_entity()
    created = _invoke(
        entity,
        operation=SessionAuthorityOperation.CREATE,
        operation_input=_creation().model_dump(mode="json"),
    )
    command = DeleteSessionCommand(
        idempotency_key="delete-key",
        requested_at=NOW,
    )

    deleted = _invoke(
        entity,
        operation=SessionAuthorityOperation.DELETE,
        operation_input=command.model_dump(mode="json"),
        state=created._state,
    )
    replayed = _invoke(
        entity,
        operation=SessionAuthorityOperation.DELETE,
        operation_input=command.model_dump(mode="json"),
        state=deleted._state,
    )

    assert deleted.result["replayed"] is False
    assert replayed.result["replayed"] is True
    assert replayed.result["tombstone"] == deleted.result["tombstone"]
    assert replayed._state == deleted._state


def test_entity_rejects_unknown_operation_input_fields() -> None:
    entity = _registered_entity()

    with pytest.raises(SessionAuthorityEntityError, match="input is invalid"):
        _invoke(
            entity,
            operation=SessionAuthorityOperation.CREATE,
            operation_input=_creation().model_dump(mode="json")
            | {"unexpected": True},
        )


def test_entity_rejects_corrupted_persisted_state() -> None:
    entity = _registered_entity()

    with pytest.raises(SessionAuthorityEntityError, match="Persisted"):
        _invoke(
            entity,
            operation=SessionAuthorityOperation.ADMIT,
            state={"schema_version": 1},
        )
