"""Diagnostic classification shared by the opt-in ACA smoke coverage."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Literal

from azure.core.exceptions import ClientAuthenticationError

from azure_functions_agents.transport.transport_models import (
    AcaSandboxDependencyError,
    SandboxCapacityError,
    SandboxGroupBindingError,
)

type AcaSmokeDiagnosticBucket = Literal["environment", "unexpected"]

_ENVIRONMENT_STATUS_CODES = frozenset({401, 403, 404, 409, 429, 503})
_AUTHORIZATION_STATUS_CODES = frozenset({401, 403})


class AcaSmokeEnvironmentError(RuntimeError):
    """The enabled ACA smoke environment cannot exercise the runtime."""

    def __init__(self, message: str) -> None:
        super().__init__(f"ACA-SMOKE-ENV: {message}")


def classify_aca_smoke_exception(error: BaseException) -> AcaSmokeDiagnosticBucket:
    """Classify setup failures so live-test errors remain an operations signal."""

    for candidate in _exception_chain(error):
        if isinstance(
            candidate,
            (
                AcaSmokeEnvironmentError,
                AcaSandboxDependencyError,
                SandboxCapacityError,
                SandboxGroupBindingError,
                TimeoutError,
            ),
        ):
            return "environment"
        status_code = getattr(candidate, "status_code", None)
        if isinstance(status_code, int) and status_code in _ENVIRONMENT_STATUS_CODES:
            return "environment"
    return "unexpected"


def is_aca_authorization_failure(error: BaseException) -> bool:
    """Report a permanent 401/403 authorization failure that no retry can clear."""

    for candidate in _exception_chain(error):
        if isinstance(candidate, ClientAuthenticationError):
            return True
        status_code = getattr(candidate, "status_code", None)
        if isinstance(status_code, int) and status_code in _AUTHORIZATION_STATUS_CODES:
            return True
    return False


def _exception_chain(error: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    candidate: BaseException | None = error
    while candidate is not None and id(candidate) not in seen:
        seen.add(id(candidate))
        yield candidate
        candidate = candidate.__cause__ or candidate.__context__
