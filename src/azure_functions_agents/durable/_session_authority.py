"""Private Durable session authority and admission state."""

from __future__ import annotations

import hashlib
import hmac
import json
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from azure_functions_agents.harness._checkpoint import (
    MAX_AGENT_SLUG_LENGTH,
    MAX_IDENTITY_VALUE_LENGTH,
    OWNER_SCOPE_DIGEST_PATTERN,
    ContentRef,
)

ENTITY_STATE_SCHEMA_VERSION: Literal[1] = 1
MAX_ENTITY_STATE_BYTES = 512 * 1024
MAX_IDEMPOTENCY_KEY_LENGTH = 512
MAX_CREATION_NONCE_LENGTH = 256

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_IdentityValue = Annotated[
    str,
    Field(min_length=1, max_length=MAX_IDENTITY_VALUE_LENGTH),
]
_EntitySchemaVersion = Literal[1]


class SessionAuthorityContractError(ValueError):
    """Persisted session authority state violates the active contract."""


class GenerationFenceStatus(StrEnum):
    """Explains whether work may mutate the authoritative session."""

    ACTIVE = "active"
    SESSION_DELETED = "session_deleted"
    NO_ACTIVE_TURN = "no_active_turn"
    STALE_GENERATION = "stale_generation"
    IDENTITY_MISMATCH = "identity_mismatch"


class TurnTerminalStatus(StrEnum):
    """Records why an active turn released the session slot."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class _AuthorityModel(BaseModel):
    """Base for strict persisted Entity contracts."""

    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class SessionCreation(_AuthorityModel):
    """Names one owner-scoped session and its runtime-minted incarnation nonce."""

    backend_name: _IdentityValue
    task_hub_name: _IdentityValue
    agent_slug: str = Field(min_length=1, max_length=MAX_AGENT_SLUG_LENGTH)
    owner_scope_digest: str = Field(pattern=OWNER_SCOPE_DIGEST_PATTERN)
    entry_id: _IdentityValue
    session_id: _IdentityValue
    creation_nonce: str = Field(min_length=1, max_length=MAX_CREATION_NONCE_LENGTH)


class AdmissionCommand(_AuthorityModel):
    """Requests admission after content has been persisted by reference."""

    idempotency_key: str = Field(min_length=1, max_length=MAX_IDEMPOTENCY_KEY_LENGTH)
    request_fingerprint: str = Field(pattern=_DIGEST_PATTERN)
    request_ref: ContentRef
    turn_generation: int = Field(ge=1)
    admitted_at: AwareDatetime


class AdmissionReceipt(_AuthorityModel):
    """Records the stable identity returned for one accepted request."""

    idempotency_key: str = Field(min_length=1, max_length=MAX_IDEMPOTENCY_KEY_LENGTH)
    request_fingerprint: str = Field(pattern=_DIGEST_PATTERN)
    request_id: str = Field(pattern=_DIGEST_PATTERN)
    run_id: str = Field(pattern=_DIGEST_PATTERN)
    turn_generation: int = Field(ge=1)
    request_ref: ContentRef
    admitted_at: AwareDatetime


class ActiveTurn(_AuthorityModel):
    """Identifies the only turn currently allowed to commit session state."""

    request_id: str = Field(pattern=_DIGEST_PATTERN)
    run_id: str = Field(pattern=_DIGEST_PATTERN)
    turn_generation: int = Field(ge=1)
    admitted_at: AwareDatetime
    started_at: AwareDatetime | None = None
    deadline: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_start(self) -> ActiveTurn:
        """Bind start acknowledgement and deadline as one immutable transition."""
        if (self.started_at is None) != (self.deadline is None):
            raise ValueError("turn start time and deadline must be recorded together")
        if (
            self.started_at is not None
            and self.deadline is not None
            and (
                self.started_at < self.admitted_at
                or self.deadline <= self.started_at
            )
        ):
            raise ValueError("turn start and deadline must follow admission")
        return self


class SessionTombstone(_AuthorityModel):
    """Prevents a deleted session incarnation from being admitted again."""

    deletion_id: str = Field(pattern=_DIGEST_PATTERN)
    idempotency_key: str = Field(min_length=1, max_length=MAX_IDEMPOTENCY_KEY_LENGTH)
    requested_at: AwareDatetime
    fenced_turn_generation: int | None = Field(default=None, ge=1)


class TerminalTurn(_AuthorityModel):
    """Records the latest generation-fenced terminal outcome by reference."""

    request_id: str = Field(pattern=_DIGEST_PATTERN)
    run_id: str = Field(pattern=_DIGEST_PATTERN)
    turn_generation: int = Field(ge=1)
    status: TurnTerminalStatus
    terminal_at: AwareDatetime
    result_ref: ContentRef | None = None
    error_code: _IdentityValue | None = None

    @model_validator(mode="after")
    def validate_terminal_metadata(self) -> TerminalTurn:
        """Keep successful result and terminal failure metadata unambiguous."""
        if self.status is TurnTerminalStatus.COMPLETED:
            if self.result_ref is None or self.error_code is not None:
                raise ValueError("completed turns require only a result reference")
        elif self.result_ref is not None:
            raise ValueError("non-completed turns cannot contain a result reference")
        return self


class SessionAuthorityState(_AuthorityModel):
    """Stores bounded owner/session authority without raw user or harness data."""

    schema_version: _EntitySchemaVersion = ENTITY_STATE_SCHEMA_VERSION
    backend_name: _IdentityValue
    task_hub_name: _IdentityValue
    agent_slug: str = Field(min_length=1, max_length=MAX_AGENT_SLUG_LENGTH)
    owner_scope_digest: str = Field(pattern=OWNER_SCOPE_DIGEST_PATTERN)
    entry_id: _IdentityValue
    session_id: _IdentityValue
    creation_nonce: str = Field(min_length=1, max_length=MAX_CREATION_NONCE_LENGTH)
    session_incarnation_id: str = Field(pattern=_DIGEST_PATTERN)
    next_turn_generation: int = Field(ge=1)
    admission_receipts: tuple[AdmissionReceipt, ...] = ()
    active_turn: ActiveTurn | None = None
    last_terminal_turn: TerminalTurn | None = None
    tombstone: SessionTombstone | None = None

    @model_validator(mode="after")
    def validate_authority(self) -> SessionAuthorityState:
        """Keep persisted identities, receipts and the active fence consistent."""
        expected_incarnation = derive_session_incarnation_id(
            backend_name=self.backend_name,
            task_hub_name=self.task_hub_name,
            agent_slug=self.agent_slug,
            owner_scope_digest=self.owner_scope_digest,
            entry_id=self.entry_id,
            session_id=self.session_id,
            creation_nonce=self.creation_nonce,
        )
        if not hmac.compare_digest(
            self.session_incarnation_id,
            expected_incarnation,
        ):
            raise ValueError("session incarnation identity is inconsistent")

        seen_keys: set[str] = set()
        seen_requests: set[str] = set()
        seen_runs: set[str] = set()
        seen_generations: set[int] = set()
        expected_generation = 1
        receipts_by_request: dict[str, AdmissionReceipt] = {}
        for receipt in self.admission_receipts:
            expected_request_id = derive_request_id(
                self.session_incarnation_id,
                receipt.idempotency_key,
            )
            if not hmac.compare_digest(receipt.request_id, expected_request_id):
                raise ValueError("admission request identity is inconsistent")
            expected_run_id = derive_run_id(receipt.request_id)
            if not hmac.compare_digest(receipt.run_id, expected_run_id):
                raise ValueError("admission run identity is inconsistent")
            if receipt.turn_generation != expected_generation:
                raise ValueError("admission generations must be contiguous")
            if (
                receipt.idempotency_key in seen_keys
                or receipt.request_id in seen_requests
                or receipt.run_id in seen_runs
                or receipt.turn_generation in seen_generations
            ):
                raise ValueError("admission receipt identities must be unique")
            expected_generation += 1
            seen_keys.add(receipt.idempotency_key)
            seen_requests.add(receipt.request_id)
            seen_runs.add(receipt.run_id)
            seen_generations.add(receipt.turn_generation)
            receipts_by_request[receipt.request_id] = receipt

        if self.next_turn_generation != expected_generation:
            raise ValueError("next turn generation must follow accepted generations")
        if self.active_turn is not None:
            active_receipt = receipts_by_request.get(self.active_turn.request_id)
            if active_receipt is None or (
                active_receipt.run_id != self.active_turn.run_id
                or active_receipt.turn_generation != self.active_turn.turn_generation
                or active_receipt.admitted_at != self.active_turn.admitted_at
            ):
                raise ValueError("active turn must match an admission receipt")
            if active_receipt is not self.admission_receipts[-1]:
                raise ValueError("active turn must be the latest accepted generation")
        if self.last_terminal_turn is not None:
            terminal_receipt = receipts_by_request.get(
                self.last_terminal_turn.request_id
            )
            if terminal_receipt is None or (
                terminal_receipt.run_id != self.last_terminal_turn.run_id
                or terminal_receipt.turn_generation
                != self.last_terminal_turn.turn_generation
            ):
                raise ValueError("terminal turn must match an admission receipt")
            if self.last_terminal_turn.terminal_at < terminal_receipt.admitted_at:
                raise ValueError("terminal turn cannot precede admission")
            expected_terminal_generation = self.next_turn_generation - 1
            if self.active_turn is not None:
                expected_terminal_generation = self.active_turn.turn_generation - 1
            elif (
                self.tombstone is not None
                and self.tombstone.fenced_turn_generation is not None
            ):
                expected_terminal_generation = (
                    self.tombstone.fenced_turn_generation - 1
                )
            if self.last_terminal_turn.turn_generation != expected_terminal_generation:
                raise ValueError("terminal turn must be the latest completed generation")
        elif (
            self.admission_receipts
            and self.active_turn is None
            and self.tombstone is None
        ):
            raise ValueError("an idle admitted session requires a terminal turn")
        elif self.active_turn is not None and self.active_turn.turn_generation > 1:
            raise ValueError("later active generations require the prior terminal turn")
        if self.tombstone is not None:
            expected_deletion_id = derive_deletion_id(
                self.session_incarnation_id,
                self.tombstone.idempotency_key,
            )
            if not hmac.compare_digest(
                self.tombstone.deletion_id,
                expected_deletion_id,
            ):
                raise ValueError("session tombstone identity is inconsistent")
            if self.tombstone.fenced_turn_generation is not None and (
                self.tombstone.fenced_turn_generation
                != self.next_turn_generation - 1
            ):
                raise ValueError("session tombstone fence is inconsistent")
        if self.tombstone is not None and self.active_turn is not None:
            raise ValueError("deleted sessions cannot retain an active turn")
        return self


class AdmissionAccepted(_AuthorityModel):
    """Returns the stable receipt for a new or replayed admission."""

    kind: Literal["accepted"] = "accepted"
    receipt: AdmissionReceipt
    replayed: bool


class AdmissionBusy(_AuthorityModel):
    """Rejects a different request while one turn remains active."""

    kind: Literal["session_busy"] = "session_busy"
    active_request_id: str = Field(pattern=_DIGEST_PATTERN)
    active_run_id: str = Field(pattern=_DIGEST_PATTERN)
    active_turn_generation: int = Field(ge=1)


class AdmissionConflict(_AuthorityModel):
    """Rejects reuse of an idempotency key for different request content."""

    kind: Literal["idempotency_conflict"] = "idempotency_conflict"
    request_id: str = Field(pattern=_DIGEST_PATTERN)


class AdmissionGenerationMismatch(_AuthorityModel):
    """Rejects content prepared for a generation that is no longer current."""

    kind: Literal["generation_mismatch"] = "generation_mismatch"
    expected_turn_generation: int = Field(ge=1)


class AdmissionDeleted(_AuthorityModel):
    """Rejects admission after the session incarnation was tombstoned."""

    kind: Literal["session_deleted"] = "session_deleted"
    deletion_id: str = Field(pattern=_DIGEST_PATTERN)


type AdmissionOutcome = Annotated[
    AdmissionAccepted
    | AdmissionBusy
    | AdmissionConflict
    | AdmissionGenerationMismatch
    | AdmissionDeleted,
    Field(discriminator="kind"),
]


class StartTurnCommand(_AuthorityModel):
    """Acknowledges stable run start and binds its absolute deadline once."""

    request_id: str = Field(pattern=_DIGEST_PATTERN)
    run_id: str = Field(pattern=_DIGEST_PATTERN)
    turn_generation: int = Field(ge=1)
    started_at: AwareDatetime
    deadline: AwareDatetime

    @model_validator(mode="after")
    def validate_deadline(self) -> StartTurnCommand:
        """Require one positive absolute execution window."""
        if self.deadline <= self.started_at:
            raise ValueError("turn deadline must follow start acknowledgement")
        return self


class StartAcknowledged(_AuthorityModel):
    """Confirms that the unchanged absolute deadline is authoritative."""

    kind: Literal["acknowledged"] = "acknowledged"
    active_turn: ActiveTurn
    replayed: bool


class StartRejected(_AuthorityModel):
    """Rejects a stale start or an attempt to alter the bound deadline."""

    kind: Literal["rejected"] = "rejected"
    reason: Literal[
        "session_deleted",
        "no_active_turn",
        "stale_generation",
        "identity_mismatch",
        "deadline_conflict",
    ]


type StartTurnOutcome = Annotated[
    StartAcknowledged | StartRejected,
    Field(discriminator="kind"),
]


class CompleteTurnCommand(_AuthorityModel):
    """Commits a terminal outcome and releases the active session slot."""

    request_id: str = Field(pattern=_DIGEST_PATTERN)
    run_id: str = Field(pattern=_DIGEST_PATTERN)
    turn_generation: int = Field(ge=1)
    status: TurnTerminalStatus
    terminal_at: AwareDatetime
    result_ref: ContentRef | None = None
    error_code: _IdentityValue | None = None

    @model_validator(mode="after")
    def validate_terminal_metadata(self) -> CompleteTurnCommand:
        """Apply the same terminal metadata contract as persisted state."""
        TerminalTurn(
            request_id=self.request_id,
            run_id=self.run_id,
            turn_generation=self.turn_generation,
            status=self.status,
            terminal_at=self.terminal_at,
            result_ref=self.result_ref,
            error_code=self.error_code,
        )
        return self


class TerminalCommitAccepted(_AuthorityModel):
    """Returns the stable terminal record for a new or replayed commit."""

    kind: Literal["committed"] = "committed"
    terminal_turn: TerminalTurn
    replayed: bool


class TerminalCommitRejected(_AuthorityModel):
    """Rejects terminal work that does not own the active generation."""

    kind: Literal["rejected"] = "rejected"
    reason: Literal[
        "session_deleted",
        "no_active_turn",
        "stale_generation",
        "identity_mismatch",
        "terminal_conflict",
    ]


type TerminalCommitOutcome = Annotated[
    TerminalCommitAccepted | TerminalCommitRejected,
    Field(discriminator="kind"),
]


class DeleteSessionCommand(_AuthorityModel):
    """Requests a stable tombstone for an owner-authorized session deletion."""

    idempotency_key: str = Field(min_length=1, max_length=MAX_IDEMPOTENCY_KEY_LENGTH)
    requested_at: AwareDatetime


class DeletionAccepted(_AuthorityModel):
    """Returns the original tombstone for new and repeated deletion requests."""

    tombstone: SessionTombstone
    replayed: bool


_STATE_ADAPTER = TypeAdapter(SessionAuthorityState)


def _derive_digest(domain: str, *values: str | bytes) -> str:
    digest = hashlib.sha256()
    for value in (domain, *values):
        encoded = value if isinstance(value, bytes) else value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, byteorder="big"))
        digest.update(encoded)
    return digest.hexdigest()


def derive_session_incarnation_id(
    *,
    backend_name: str,
    task_hub_name: str,
    agent_slug: str,
    owner_scope_digest: str,
    entry_id: str,
    session_id: str,
    creation_nonce: str,
) -> str:
    """Derive the immutable identity for one owner-scoped session incarnation."""
    return _derive_digest(
        "session-v1",
        backend_name,
        task_hub_name,
        agent_slug,
        owner_scope_digest,
        entry_id,
        session_id,
        creation_nonce,
    )


def derive_request_id(session_incarnation_id: str, idempotency_key: str) -> str:
    """Derive the stable request identity used to reconcile retries."""
    return _derive_digest("request-v1", session_incarnation_id, idempotency_key)


def derive_run_id(request_id: str) -> str:
    """Derive the stable Durable run identity for one accepted request."""
    return _derive_digest("run-v1", request_id)


def derive_deletion_id(session_incarnation_id: str, idempotency_key: str) -> str:
    """Derive a stable internal deletion receipt identity."""
    return _derive_digest("delete-v1", session_incarnation_id, idempotency_key)


def derive_request_fingerprint(entry_id: str, normalized_request_body: bytes) -> str:
    """Hash the complete normalized request body with its registered entry identity."""
    return _derive_digest("fingerprint-v1", entry_id, normalized_request_body)


def create_session_authority(creation: SessionCreation) -> SessionAuthorityState:
    """Create the initial authority state for a runtime-minted incarnation."""
    state = SessionAuthorityState(
        backend_name=creation.backend_name,
        task_hub_name=creation.task_hub_name,
        agent_slug=creation.agent_slug,
        owner_scope_digest=creation.owner_scope_digest,
        entry_id=creation.entry_id,
        session_id=creation.session_id,
        creation_nonce=creation.creation_nonce,
        session_incarnation_id=derive_session_incarnation_id(
            backend_name=creation.backend_name,
            task_hub_name=creation.task_hub_name,
            agent_slug=creation.agent_slug,
            owner_scope_digest=creation.owner_scope_digest,
            entry_id=creation.entry_id,
            session_id=creation.session_id,
            creation_nonce=creation.creation_nonce,
        ),
        next_turn_generation=1,
    )
    _ensure_state_capacity(state)
    return state


def admit_turn(
    state: SessionAuthorityState,
    command: AdmissionCommand,
) -> tuple[SessionAuthorityState, AdmissionOutcome]:
    """Apply one idempotent admission transition without storing request bytes."""
    if state.tombstone is not None:
        return state, AdmissionDeleted(deletion_id=state.tombstone.deletion_id)

    for receipt in state.admission_receipts:
        if receipt.idempotency_key != command.idempotency_key:
            continue
        if hmac.compare_digest(
            receipt.request_fingerprint,
            command.request_fingerprint,
        ):
            return state, AdmissionAccepted(receipt=receipt, replayed=True)
        return state, AdmissionConflict(request_id=receipt.request_id)

    if state.active_turn is not None:
        return state, AdmissionBusy(
            active_request_id=state.active_turn.request_id,
            active_run_id=state.active_turn.run_id,
            active_turn_generation=state.active_turn.turn_generation,
        )

    if command.turn_generation != state.next_turn_generation:
        return state, AdmissionGenerationMismatch(
            expected_turn_generation=state.next_turn_generation
        )

    request_id = derive_request_id(
        state.session_incarnation_id,
        command.idempotency_key,
    )
    receipt = AdmissionReceipt(
        idempotency_key=command.idempotency_key,
        request_fingerprint=command.request_fingerprint,
        request_id=request_id,
        run_id=derive_run_id(request_id),
        turn_generation=command.turn_generation,
        request_ref=command.request_ref,
        admitted_at=command.admitted_at,
    )
    active_turn = ActiveTurn(
        request_id=receipt.request_id,
        run_id=receipt.run_id,
        turn_generation=receipt.turn_generation,
        admitted_at=receipt.admitted_at,
    )
    updated = state.model_copy(
        update={
            "next_turn_generation": state.next_turn_generation + 1,
            "admission_receipts": (*state.admission_receipts, receipt),
            "active_turn": active_turn,
        }
    )
    updated = SessionAuthorityState.model_validate(
        updated.model_dump(mode="python"),
        strict=True,
    )
    _ensure_state_capacity(updated)
    return updated, AdmissionAccepted(receipt=receipt, replayed=False)


def acknowledge_turn_start(
    state: SessionAuthorityState,
    command: StartTurnCommand,
) -> tuple[SessionAuthorityState, StartTurnOutcome]:
    """Bind the first successful start acknowledgement and absolute deadline."""
    fence = generation_fence_status(
        state,
        request_id=command.request_id,
        run_id=command.run_id,
        turn_generation=command.turn_generation,
    )
    if fence is not GenerationFenceStatus.ACTIVE:
        return state, StartRejected(reason=_start_rejection_reason(fence))

    active = state.active_turn
    assert active is not None
    if active.deadline is not None:
        if active.deadline == command.deadline and active.started_at == command.started_at:
            return state, StartAcknowledged(active_turn=active, replayed=True)
        return state, StartRejected(reason="deadline_conflict")

    started = active.model_copy(
        update={
            "started_at": command.started_at,
            "deadline": command.deadline,
        }
    )
    updated = state.model_copy(update={"active_turn": started})
    updated = SessionAuthorityState.model_validate(
        updated.model_dump(mode="python"),
        strict=True,
    )
    _ensure_state_capacity(updated)
    return updated, StartAcknowledged(active_turn=started, replayed=False)


def generation_fence_status(
    state: SessionAuthorityState,
    *,
    request_id: str,
    run_id: str,
    turn_generation: int,
) -> GenerationFenceStatus:
    """Classify whether a later commit belongs to the authoritative active turn."""
    if state.tombstone is not None:
        return GenerationFenceStatus.SESSION_DELETED
    active = state.active_turn
    if active is None:
        return GenerationFenceStatus.NO_ACTIVE_TURN
    if turn_generation != active.turn_generation:
        return GenerationFenceStatus.STALE_GENERATION
    if active.request_id != request_id or active.run_id != run_id:
        return GenerationFenceStatus.IDENTITY_MISMATCH
    return GenerationFenceStatus.ACTIVE


def complete_turn(
    state: SessionAuthorityState,
    command: CompleteTurnCommand,
) -> tuple[SessionAuthorityState, TerminalCommitOutcome]:
    """Commit one terminal outcome idempotently and release the active slot."""
    prior = state.last_terminal_turn
    if prior is not None and (
        prior.request_id == command.request_id
        and prior.run_id == command.run_id
        and prior.turn_generation == command.turn_generation
    ):
        if _terminal_matches_command(prior, command):
            return state, TerminalCommitAccepted(
                terminal_turn=prior,
                replayed=True,
            )
        return state, TerminalCommitRejected(reason="terminal_conflict")

    fence = generation_fence_status(
        state,
        request_id=command.request_id,
        run_id=command.run_id,
        turn_generation=command.turn_generation,
    )
    if fence is not GenerationFenceStatus.ACTIVE:
        return state, TerminalCommitRejected(reason=_start_rejection_reason(fence))

    terminal = TerminalTurn(
        request_id=command.request_id,
        run_id=command.run_id,
        turn_generation=command.turn_generation,
        status=command.status,
        terminal_at=command.terminal_at,
        result_ref=command.result_ref,
        error_code=command.error_code,
    )
    updated = state.model_copy(
        update={
            "active_turn": None,
            "last_terminal_turn": terminal,
        }
    )
    updated = SessionAuthorityState.model_validate(
        updated.model_dump(mode="python"),
        strict=True,
    )
    _ensure_state_capacity(updated)
    return updated, TerminalCommitAccepted(
        terminal_turn=terminal,
        replayed=False,
    )


def mark_session_deleted(
    state: SessionAuthorityState,
    command: DeleteSessionCommand,
) -> tuple[SessionAuthorityState, DeletionAccepted]:
    """Tombstone admission immediately while preserving a stable deletion receipt."""
    if state.tombstone is not None:
        return state, DeletionAccepted(tombstone=state.tombstone, replayed=True)

    tombstone = SessionTombstone(
        deletion_id=derive_deletion_id(
            state.session_incarnation_id,
            command.idempotency_key,
        ),
        idempotency_key=command.idempotency_key,
        requested_at=command.requested_at,
        fenced_turn_generation=(
            state.active_turn.turn_generation
            if state.active_turn is not None
            else None
        ),
    )
    updated = state.model_copy(
        update={
            "active_turn": None,
            "tombstone": tombstone,
        }
    )
    updated = SessionAuthorityState.model_validate(
        updated.model_dump(mode="python"),
        strict=True,
    )
    _ensure_state_capacity(updated)
    return updated, DeletionAccepted(tombstone=tombstone, replayed=False)


def encode_session_authority_state(state: SessionAuthorityState) -> bytes:
    """Serialize validated Entity control state within the hydrated-state limit."""
    try:
        validated = SessionAuthorityState.model_validate(
            state.model_dump(mode="python"),
            strict=True,
        )
        payload = validated.model_dump_json().encode("utf-8")
    except (ValidationError, ValueError, TypeError):
        raise SessionAuthorityContractError(
            "Durable session authority state is invalid."
        ) from None
    if len(payload) > MAX_ENTITY_STATE_BYTES:
        raise SessionAuthorityContractError(
            "Durable session authority state exceeds its serialized size limit."
        )
    return payload


def decode_session_authority_state(payload: bytes) -> SessionAuthorityState:
    """Deserialize strict Entity control state after rejecting ambiguous JSON."""
    if len(payload) > MAX_ENTITY_STATE_BYTES:
        raise SessionAuthorityContractError(
            "Durable session authority state exceeds its serialized size limit."
        )
    try:
        json.loads(payload, object_pairs_hook=_reject_duplicate_json_keys)
        return _STATE_ADAPTER.validate_json(payload, strict=True)
    except (ValidationError, ValueError, UnicodeDecodeError):
        raise SessionAuthorityContractError(
            "Durable session authority state is invalid."
        ) from None


def _ensure_state_capacity(state: SessionAuthorityState) -> None:
    encode_session_authority_state(state)


def _terminal_matches_command(
    terminal: TerminalTurn,
    command: CompleteTurnCommand,
) -> bool:
    return (
        terminal.status is command.status
        and terminal.terminal_at == command.terminal_at
        and terminal.result_ref == command.result_ref
        and terminal.error_code == command.error_code
    )


def _start_rejection_reason(
    status: GenerationFenceStatus,
) -> Literal[
    "session_deleted",
    "no_active_turn",
    "stale_generation",
    "identity_mismatch",
]:
    if status is GenerationFenceStatus.SESSION_DELETED:
        return "session_deleted"
    if status is GenerationFenceStatus.NO_ACTIVE_TURN:
        return "no_active_turn"
    if status is GenerationFenceStatus.STALE_GENERATION:
        return "stale_generation"
    if status is GenerationFenceStatus.IDENTITY_MISMATCH:
        return "identity_mismatch"
    raise ValueError("active generation is not a rejection")


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result
