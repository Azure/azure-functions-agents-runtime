"""Azure Durable Entity binding for session authority."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, cast

import azure.durable_functions as df
import azure.functions as func
from pydantic import BaseModel, ConfigDict, ValidationError

from ._session_authority import (
    MAX_ENTITY_STATE_BYTES,
    AdmissionCommand,
    CompleteTurnCommand,
    DeleteSessionCommand,
    SessionAuthorityContractError,
    SessionAuthorityState,
    SessionCreation,
    StartTurnCommand,
    acknowledge_turn_start,
    admit_turn,
    complete_turn,
    create_session_authority,
    decode_session_authority_state,
    derive_session_entity_key,
    encode_session_authority_state,
    mark_session_deleted,
)

SESSION_AUTHORITY_ENTITY_NAME = "agents_durable_state_v1"
MAX_ENTITY_OPERATION_BYTES = 64 * 1024


class SessionAuthorityEntityError(ValueError):
    """A session authority Entity operation cannot be applied safely."""


class SessionAuthorityOperation(StrEnum):
    """Names the supported private Durable Entity operations."""

    CREATE = "create"
    GET = "get"
    ADMIT = "admit"
    ACKNOWLEDGE_START = "acknowledge_start"
    COMPLETE = "complete"
    DELETE = "delete"


class _EntityResult(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class SessionCreated(_EntityResult):
    """Returns the stable incarnation selected for a new or reconciled session."""

    entity_key: str
    session_incarnation_id: str
    next_turn_generation: int
    created: bool


def register_session_authority_entity(app: func.FunctionApp) -> None:
    """Register the app-wide session authority Entity exactly once."""
    blueprint = df.Blueprint()

    def agents_durable_state_v1(context: df.DurableEntityContext) -> None:
        _dispatch_session_authority_operation(context)

    entity_trigger = blueprint.entity_trigger(
        context_name="context",
        entity_name=SESSION_AUTHORITY_ENTITY_NAME,
    )
    # The SDK supports this v1 context shape but its decorator annotation names only v2 callables.
    entity_trigger(cast(Any, agents_durable_state_v1))

    app.register_blueprint(blueprint)


def _dispatch_session_authority_operation(
    context: df.DurableEntityContext,
) -> None:
    try:
        operation = SessionAuthorityOperation(context.operation_name)
    except (TypeError, ValueError):
        raise SessionAuthorityEntityError(
            "Unsupported Durable session authority operation."
        ) from None

    if operation is SessionAuthorityOperation.CREATE:
        _create_or_reconcile_session(context)
        return

    state = _load_required_state(context)
    _validate_entity_key(context.entity_key, state)

    if operation is SessionAuthorityOperation.GET:
        _require_empty_input(context.get_input())
        _set_result(context, state, maximum=MAX_ENTITY_STATE_BYTES)
        return
    if operation is SessionAuthorityOperation.ADMIT:
        admission_command = _parse_operation_input(
            AdmissionCommand,
            context.get_input(),
        )
        admitted_state, admission_outcome = admit_turn(state, admission_command)
        _commit_transition(context, state, admitted_state, admission_outcome)
        return
    if operation is SessionAuthorityOperation.ACKNOWLEDGE_START:
        start_command = _parse_operation_input(
            StartTurnCommand,
            context.get_input(),
        )
        started_state, start_outcome = acknowledge_turn_start(state, start_command)
        _commit_transition(context, state, started_state, start_outcome)
        return
    if operation is SessionAuthorityOperation.COMPLETE:
        completion_command = _parse_operation_input(
            CompleteTurnCommand,
            context.get_input(),
        )
        completed_state, completion_outcome = complete_turn(state, completion_command)
        _commit_transition(context, state, completed_state, completion_outcome)
        return
    if operation is SessionAuthorityOperation.DELETE:
        deletion_command = _parse_operation_input(
            DeleteSessionCommand,
            context.get_input(),
        )
        deleted_state, deletion_outcome = mark_session_deleted(state, deletion_command)
        _commit_transition(context, state, deleted_state, deletion_outcome)
        return
    raise SessionAuthorityEntityError(
        "Unsupported Durable session authority operation."
    )


def _create_or_reconcile_session(context: df.DurableEntityContext) -> None:
    creation = _parse_operation_input(SessionCreation, context.get_input())
    expected_key = derive_session_entity_key(
        backend_name=creation.backend_name,
        task_hub_name=creation.task_hub_name,
        agent_slug=creation.agent_slug,
        owner_scope_digest=creation.owner_scope_digest,
        entry_id=creation.entry_id,
        session_id=creation.session_id,
    )
    if context.entity_key != expected_key:
        raise SessionAuthorityEntityError(
            "Durable session authority Entity key does not match the session identity."
        )

    state = _load_optional_state(context)
    if state is None:
        state = create_session_authority(creation)
        _set_state(context, state)
        created = True
    else:
        _validate_creation_identity(state, creation)
        created = False

    _set_result(
        context,
        SessionCreated(
            entity_key=expected_key,
            session_incarnation_id=state.session_incarnation_id,
            next_turn_generation=state.next_turn_generation,
            created=created,
        ),
        maximum=MAX_ENTITY_OPERATION_BYTES,
    )


def _load_required_state(
    context: df.DurableEntityContext,
) -> SessionAuthorityState:
    state = _load_optional_state(context)
    if state is None:
        raise SessionAuthorityEntityError(
            "Durable session authority Entity has not been created."
        )
    return state


def _load_optional_state(
    context: df.DurableEntityContext,
) -> SessionAuthorityState | None:
    raw_state = context.get_state()
    if raw_state is None:
        return None
    try:
        payload = _json_payload(
            raw_state,
            maximum=MAX_ENTITY_STATE_BYTES,
            subject="Durable session authority state",
        )
        return decode_session_authority_state(payload)
    except SessionAuthorityContractError:
        raise SessionAuthorityEntityError(
            "Persisted Durable session authority state is invalid."
        ) from None


def _set_state(
    context: df.DurableEntityContext,
    state: SessionAuthorityState,
) -> None:
    payload = encode_session_authority_state(state)
    context.set_state(json.loads(payload))


def _commit_transition(
    context: df.DurableEntityContext,
    previous: SessionAuthorityState,
    updated: SessionAuthorityState,
    outcome: BaseModel,
) -> None:
    if updated is not previous:
        _set_state(context, updated)
    _set_result(context, outcome, maximum=MAX_ENTITY_OPERATION_BYTES)


def _set_result(
    context: df.DurableEntityContext,
    result: BaseModel,
    *,
    maximum: int,
) -> None:
    payload = _json_payload(
        result.model_dump(mode="json"),
        maximum=maximum,
        subject="Durable session authority result",
    )
    context.set_result(json.loads(payload))


def _parse_operation_input[T: BaseModel](
    model_type: type[T],
    raw_input: Any,
) -> T:
    try:
        payload = _json_payload(
            raw_input,
            maximum=MAX_ENTITY_OPERATION_BYTES,
            subject="Durable session authority operation input",
        )
        return model_type.model_validate_json(payload, strict=True)
    except ValidationError:
        raise SessionAuthorityEntityError(
            "Durable session authority operation input is invalid."
        ) from None


def _json_payload(value: Any, *, maximum: int, subject: str) -> bytes:
    try:
        payload = json.dumps(
            value,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise SessionAuthorityEntityError(f"{subject} is not valid JSON.") from None
    if len(payload) > maximum:
        raise SessionAuthorityEntityError(
            f"{subject} exceeds its serialized size limit."
        )
    return payload


def _validate_entity_key(
    entity_key: str,
    state: SessionAuthorityState,
) -> None:
    expected_key = derive_session_entity_key(
        backend_name=state.backend_name,
        task_hub_name=state.task_hub_name,
        agent_slug=state.agent_slug,
        owner_scope_digest=state.owner_scope_digest,
        entry_id=state.entry_id,
        session_id=state.session_id,
    )
    if entity_key != expected_key:
        raise SessionAuthorityEntityError(
            "Durable session authority Entity key does not match persisted state."
        )


def _validate_creation_identity(
    state: SessionAuthorityState,
    creation: SessionCreation,
) -> None:
    if (
        state.backend_name != creation.backend_name
        or state.task_hub_name != creation.task_hub_name
        or state.agent_slug != creation.agent_slug
        or state.owner_scope_digest != creation.owner_scope_digest
        or state.entry_id != creation.entry_id
        or state.session_id != creation.session_id
    ):
        raise SessionAuthorityEntityError(
            "Durable session authority creation identity conflicts with persisted state."
        )


def _require_empty_input(raw_input: Any) -> None:
    if raw_input is not None:
        raise SessionAuthorityEntityError(
            "Durable session authority get operation does not accept input."
        )


__all__ = [
    "SESSION_AUTHORITY_ENTITY_NAME",
    "SessionAuthorityEntityError",
    "SessionAuthorityOperation",
    "register_session_authority_entity",
]
