"""Private, run-idempotent history helpers for hosted Responses handlers."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .._session_id import SESSION_ID_PATTERN
from ..strict_json import DuplicateJsonKeyError, decode_json_object

if TYPE_CHECKING:
    from .fha_run_idempotent_history import FhaRunIdempotentHistoryProvider

_FHA_HISTORY_DIRECTORY = ".azure-functions-agents-runtime"
_FHA_HISTORY_ROOT = "history"
_FHA_RUN_MARKERS_DIRECTORY = "runs"
_FHA_STAGE_RECORD_VERSION: Literal["fha_model_stage_v1"] = "fha_model_stage_v1"
_MAX_STAGE_RECORD_BYTES = 1_048_576
_AGENT_SLUG_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
_HISTORY_SCOPE_PATTERN = re.compile(r"^o[0-9]+-[a-z2-7]{52}$")


class FhaPrivateHistoryError(ValueError):
    """Hosted-agent history input or durable state is invalid."""


class FhaResponsesRequestEnvelope(BaseModel):
    """Strict hosted-handler request envelope with no caller identity field."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    agent_slug: str
    history_scope: str
    runtime_session_id: str
    runtime_run_id: str
    prompt: str | None = None
    input: str | None = None

    @field_validator("agent_slug")
    @classmethod
    def _validate_agent_slug(cls, value: str) -> str:
        if not _AGENT_SLUG_PATTERN.fullmatch(value):
            raise ValueError("agent_slug must be a safe non-empty agent slug")
        return value

    @field_validator("history_scope")
    @classmethod
    def _validate_history_scope(cls, value: str) -> str:
        if _HISTORY_SCOPE_PATTERN.fullmatch(value) is None:
            raise ValueError("history_scope must be a versioned opaque owner hash")
        return value

    @field_validator("runtime_session_id")
    @classmethod
    def _validate_runtime_session_id(cls, value: str) -> str:
        return _validate_runtime_identifier(value, "runtime_session_id")

    @field_validator("runtime_run_id")
    @classmethod
    def _validate_runtime_run_id(cls, value: str) -> str:
        return _validate_runtime_identifier(value, "runtime_run_id")

    @field_validator("prompt", "input")
    @classmethod
    def _validate_text(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("prompt/input must not be empty")
        return value

    @model_validator(mode="after")
    def _require_one_input_shape(self) -> Self:
        if (self.prompt is None) == (self.input is None):
            raise ValueError("exactly one of prompt or input is required")
        return self

    @classmethod
    def parse_json_input(cls, value: str) -> Self:
        """Parse untrusted Responses input without accepting duplicate keys."""
        try:
            document = decode_json_object(value.encode("utf-8"))
            return cls.model_validate(document)
        except (
            DuplicateJsonKeyError,
            UnicodeEncodeError,
            ValueError,
            ValidationError,
        ):
            raise FhaPrivateHistoryError("Hosted Responses request envelope is invalid.") from None

    @property
    def effective_prompt(self) -> str:
        """Return the one validated prompt representation."""
        value = self.prompt if self.prompt is not None else self.input
        assert value is not None
        return value


class _FhaCommittedStageDocument(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    version: Literal["fha_model_stage_v1"]
    runtime_run_id: str
    prompt_digest: str
    messages: list[dict[str, object]] = Field(default_factory=list)
    output: str | None = None


@dataclass(frozen=True, slots=True)
class FhaCommittedModelStage:
    """One model output durably committed for a runtime run."""

    output: str


@dataclass(frozen=True, slots=True)
class FhaHistoryPaths:
    """Private persistent paths for one runtime session and run."""

    session_directory: Path
    run_marker_path: Path


@dataclass(frozen=True, slots=True)
class FhaHistoryFactory:
    """Force private file history below the hosted agent's persistent home."""

    home_directory: Path | None = None

    def paths_for(self, envelope: FhaResponsesRequestEnvelope) -> FhaHistoryPaths:
        """Return only path-safe private paths derived from opaque runtime IDs."""
        home = self.home_directory if self.home_directory is not None else Path.home()
        session_directory = (
            home
            / _FHA_HISTORY_DIRECTORY
            / _FHA_HISTORY_ROOT
            / envelope.history_scope
            / envelope.runtime_session_id
        )
        return FhaHistoryPaths(
            session_directory=session_directory,
            run_marker_path=session_directory
            / _FHA_RUN_MARKERS_DIRECTORY
            / f"{envelope.runtime_run_id}.json",
        )

    def create_maf_history_provider(
        self, envelope: FhaResponsesRequestEnvelope
    ) -> FhaRunIdempotentHistoryProvider:
        """Create a MAF file-history provider rooted at the forced private session directory."""
        paths = self.paths_for(envelope)
        _ensure_private_directory(paths.session_directory)
        from .fha_run_idempotent_history import FhaRunIdempotentHistoryProvider

        return FhaRunIdempotentHistoryProvider(self, envelope)

    def read_committed_stage(
        self, envelope: FhaResponsesRequestEnvelope
    ) -> FhaCommittedModelStage | None:
        """Return a validated run marker or ``None`` before a history commit."""
        document = self._read_stage_document(envelope)
        if document is None:
            return None
        if document.output is None:
            recovered_output = _assistant_output_from_messages(document.messages)
            if recovered_output is None:
                return None
            document = document.model_copy(update={"output": recovered_output})
            self._replace_stage_document(envelope, document)
            return FhaCommittedModelStage(output=recovered_output)
        return FhaCommittedModelStage(output=document.output)

    def commit_model_stage(
        self,
        envelope: FhaResponsesRequestEnvelope,
        output: str,
    ) -> FhaCommittedModelStage:
        """Persist one model stage once, keyed by the opaque runtime run ID."""
        if not isinstance(output, str):
            raise FhaPrivateHistoryError("Hosted Responses model output is invalid.")
        document = self._read_stage_document(envelope)
        if document is None:
            document = _FhaCommittedStageDocument(
                version=_FHA_STAGE_RECORD_VERSION,
                runtime_run_id=envelope.runtime_run_id,
                prompt_digest=_prompt_digest(envelope),
                output=output,
            )
            if self._create_stage_document(envelope, document):
                return FhaCommittedModelStage(output=output)
            document = self._read_stage_document(envelope)
            if document is None:
                raise FhaPrivateHistoryError("Hosted Responses history marker is incomplete.")
        if document.output is not None:
            return FhaCommittedModelStage(output=document.output)
        recovered_output = _assistant_output_from_messages(document.messages)
        committed_output = output if recovered_output is None else recovered_output
        self._replace_stage_document(
            envelope,
            document.model_copy(update={"output": committed_output}),
        )
        return FhaCommittedModelStage(output=committed_output)

    def commit_history_messages(
        self,
        envelope: FhaResponsesRequestEnvelope,
        messages: list[dict[str, object]],
    ) -> None:
        """Commit one MAF message delta once under the runtime run marker."""
        if self._read_stage_document(envelope) is not None:
            return
        document = _FhaCommittedStageDocument(
            version=_FHA_STAGE_RECORD_VERSION,
            runtime_run_id=envelope.runtime_run_id,
            prompt_digest=_prompt_digest(envelope),
            messages=messages,
        )
        self._create_stage_document(envelope, document)

    def read_history_messages(self, envelope: FhaResponsesRequestEnvelope) -> list[dict[str, object]]:
        """Return all message deltas for the private runtime session in commit order."""
        run_directory = self.paths_for(envelope).run_marker_path.parent
        if not run_directory.exists():
            return []
        if run_directory.is_symlink() or not run_directory.is_dir():
            raise FhaPrivateHistoryError("Hosted Responses history path is invalid.")

        documents: list[tuple[int, str, _FhaCommittedStageDocument]] = []
        for marker_path in run_directory.glob("*.json"):
            if marker_path.is_symlink():
                raise FhaPrivateHistoryError("Hosted Responses history marker must not be a link.")
            document = self._read_document_path(marker_path)
            if marker_path.stem != document.runtime_run_id:
                raise FhaPrivateHistoryError("Hosted Responses history marker run does not match.")
            try:
                modified_at = marker_path.stat().st_mtime_ns
            except OSError as exc:
                raise FhaPrivateHistoryError("Hosted Responses history marker cannot be read.") from exc
            documents.append((modified_at, marker_path.name, document))

        messages: list[dict[str, object]] = []
        for _modified_at, _name, document in sorted(documents):
            messages.extend(document.messages)
        return messages

    def _read_stage_document(
        self, envelope: FhaResponsesRequestEnvelope
    ) -> _FhaCommittedStageDocument | None:
        marker_path = self.paths_for(envelope).run_marker_path
        if not marker_path.exists():
            return None
        document = self._read_document_path(marker_path)
        if document.runtime_run_id != envelope.runtime_run_id:
            raise FhaPrivateHistoryError("Hosted Responses history marker run does not match.")
        if document.prompt_digest != _prompt_digest(envelope):
            raise FhaPrivateHistoryError("Hosted Responses history marker input does not match.")
        return document

    def _read_document_path(self, marker_path: Path) -> _FhaCommittedStageDocument:
        if marker_path.is_symlink():
            raise FhaPrivateHistoryError("Hosted Responses history marker must not be a link.")
        try:
            payload = marker_path.read_bytes()
        except OSError as exc:
            raise FhaPrivateHistoryError("Hosted Responses history marker cannot be read.") from exc
        if len(payload) > _MAX_STAGE_RECORD_BYTES:
            raise FhaPrivateHistoryError("Hosted Responses history marker exceeds its size limit.")
        try:
            return _FhaCommittedStageDocument.model_validate(decode_json_object(payload))
        except (
            DuplicateJsonKeyError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValidationError,
            ValueError,
        ):
            raise FhaPrivateHistoryError("Hosted Responses history marker is invalid.") from None

    def _create_stage_document(
        self,
        envelope: FhaResponsesRequestEnvelope,
        document: _FhaCommittedStageDocument,
    ) -> bool:
        paths = self.paths_for(envelope)
        _ensure_private_directory(paths.run_marker_path.parent)
        encoded = _encode_stage_document(document)
        try:
            with paths.run_marker_path.open("xb") as marker:
                marker.write(encoded)
                marker.flush()
                os.fsync(marker.fileno())
        except FileExistsError:
            return False
        except OSError as exc:
            raise FhaPrivateHistoryError("Hosted Responses history marker cannot be committed.") from exc
        _sync_directory(paths.run_marker_path.parent)
        return True

    def _replace_stage_document(
        self,
        envelope: FhaResponsesRequestEnvelope,
        document: _FhaCommittedStageDocument,
    ) -> None:
        paths = self.paths_for(envelope)
        _ensure_private_directory(paths.run_marker_path.parent)
        pending_path = paths.run_marker_path.with_name(
            f".{paths.run_marker_path.name}.{uuid4().hex}.pending"
        )
        encoded = _encode_stage_document(document)
        try:
            with pending_path.open("xb") as pending:
                pending.write(encoded)
                pending.flush()
                os.fsync(pending.fileno())
            os.replace(pending_path, paths.run_marker_path)
        except OSError as exc:
            pending_path.unlink(missing_ok=True)
            raise FhaPrivateHistoryError("Hosted Responses history marker cannot be committed.") from exc
        _sync_directory(paths.run_marker_path.parent)


def _encode_stage_document(document: _FhaCommittedStageDocument) -> bytes:
    encoded = (
        json.dumps(
            document.model_dump(mode="json"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    if len(encoded) > _MAX_STAGE_RECORD_BYTES:
        raise FhaPrivateHistoryError("Hosted Responses history marker exceeds its size limit.")
    return encoded


def _validate_runtime_identifier(value: str, field_name: str) -> str:
    if SESSION_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must match {SESSION_ID_PATTERN.pattern}")
    return value


def _prompt_digest(envelope: FhaResponsesRequestEnvelope) -> str:
    return hashlib.sha256(envelope.effective_prompt.encode("utf-8")).hexdigest()


def _assistant_output_from_messages(messages: list[dict[str, object]]) -> str | None:
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        contents = message.get("contents")
        if not isinstance(contents, list):
            raise FhaPrivateHistoryError("Hosted Responses history messages are invalid.")
        text_parts: list[str] = []
        for content in contents:
            if not isinstance(content, dict) or content.get("type") != "text":
                continue
            text = content.get("text")
            if not isinstance(text, str):
                raise FhaPrivateHistoryError("Hosted Responses history messages are invalid.")
            text_parts.append(text)
        if text_parts:
            return "".join(text_parts)
    return None


def _ensure_private_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise FhaPrivateHistoryError("Hosted Responses history path is invalid.")


def _sync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
