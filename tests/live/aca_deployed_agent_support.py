"""Public-HTTP helpers for the manually qualified deployed ACA agent test."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import NoReturn, Protocol
from urllib.parse import urlsplit, urlunsplit

from aiohttp import ClientResponse, ClientSession, ClientTimeout
from azure.identity.aio import DefaultAzureCredential
from tests.aca_smoke_diagnostics import AcaSmokeEnvironmentError

from azure_functions_agents.controller.http import management_urls
from azure_functions_agents.execution.backend import RunContext
from azure_functions_agents.session_state import EntraPrincipal, SessionStateContractError

_LIVE_GATE_ENV = "AZURE_FUNCTIONS_AGENTS_RUN_DEPLOYED_ACA_SMOKE"
_BASE_URL_ENV = "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_FUNCTION_BASE_URL"
_AGENT_SLUG_ENV = "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_AGENT_SLUG"
_TOKEN_SCOPE_ENV = "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_TOKEN_SCOPE"
_AUDIENCE_ENV = "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_AUDIENCE"
_TIMEOUT_ENV = "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TIMEOUT_SECONDS"
_BEARER_TOKEN_ENV = "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_BEARER_TOKEN"
_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_SETUP_RETRY_AFTER_SECONDS = 120.0
_CANCEL_RETRY_AFTER_SECONDS = 2.0
_TIMEOUT_RECOVERY_AGENT_SLUG = "deployed_setup_timeout"
_TIMEOUT_RECOVERY_MINIMUM_TIMEOUT_SECONDS = 120.0
_SSE_THROTTLE_MAX_ATTEMPTS = 4
_SSE_THROTTLE_STATUSES = frozenset({429, 503})
_SSE_THROTTLE_RETRY_AFTER_MAXIMUM_SECONDS = 10.0

_JSON_THROTTLE_MAX_ATTEMPTS = 4
_JSON_THROTTLE_STATUSES = frozenset({429, 503})
_FRONTEND_UNAVAILABLE_STATUSES = frozenset({502, 503})
_FRONTEND_UNAVAILABLE_RETRY_SECONDS = 2.0
# A 504 is an asserted setup outcome, not a retryable frontend response.
_JSON_THROTTLE_RETRY_AFTER_MAXIMUM_SECONDS = 10.0


class _TokenCredential(Protocol):
    async def get_token(self, *scopes: str) -> object: ...


def setup_retry_after_seconds(headers: Mapping[str, str]) -> float:
    """Read the bounded setup-lease retry delay without retaining response headers."""
    return _bounded_retry_after_seconds(
        headers,
        fallback_seconds=_SETUP_RETRY_AFTER_SECONDS,
        maximum_seconds=_SETUP_RETRY_AFTER_SECONDS,
    )


def cancel_retry_after_seconds(headers: Mapping[str, str]) -> float:
    """Read the public cancellation retry delay without waiting beyond one setup lease."""
    return _bounded_retry_after_seconds(
        headers,
        fallback_seconds=_CANCEL_RETRY_AFTER_SECONDS,
        maximum_seconds=_SETUP_RETRY_AFTER_SECONDS,
    )


def sse_throttle_retry_delay(
    status: int,
    headers: Mapping[str, str],
    *,
    is_final_attempt: bool,
) -> float | None:
    """Return the delay before retrying a throttled SSE response, or None to give up."""
    if is_final_attempt:
        return None
    if status in _SSE_THROTTLE_STATUSES:
        delay = optional_retry_after_seconds(
            headers, maximum_seconds=_SSE_THROTTLE_RETRY_AFTER_MAXIMUM_SECONDS
        )
        if delay is not None:
            return delay
    if _is_frontend_unavailable_response(status, headers):
        return _FRONTEND_UNAVAILABLE_RETRY_SECONDS
    return None


def optional_retry_after_seconds(
    headers: Mapping[str, str],
    *,
    maximum_seconds: float,
) -> float | None:
    """Return a bounded ``Retry-After``, or None when the server did not ask for one.

    Distinct from the fallback variant below: a caller deciding *whether* to
    retry must not invent a delay the server never sent, or it would retry
    responses that carry no backpressure signal at all.
    """
    value = response_header(headers, "retry-after")
    if value is None:
        return None
    try:
        seconds = int(value.strip())
    except ValueError:
        return None
    if not 1 <= seconds <= int(maximum_seconds):
        return None
    return float(seconds)


def response_header(headers: Mapping[str, str], name: str) -> str | None:
    """Return one case-insensitive response header value."""
    return next(
        (
            candidate
            for key, candidate in headers.items()
            if key.casefold() == name.casefold() and isinstance(candidate, str)
        ),
        None,
    )


def _is_frontend_unavailable_response(
    status: int,
    headers: Mapping[str, str],
) -> bool:
    if status not in _FRONTEND_UNAVAILABLE_STATUSES:
        return False
    content_type = response_header(headers, "content-type")
    if content_type is None:
        return False
    media_type = content_type.partition(";")[0].strip().casefold()
    return media_type in {"text/html", "text/plain"}


def _request_can_retry_frontend_unavailable(
    method: str,
    headers: Mapping[str, str] | None,
) -> bool:
    if method.upper() in {"GET", "HEAD"}:
        return True
    return headers is not None and response_header(headers, "idempotency-key") is not None


def timeout_recovery_submission_headers(
    authorization: str,
    idempotency_key: str,
) -> dict[str, str]:
    """Build the synchronous one-shot request that must surface a linked 504."""
    return {
        "Authorization": authorization,
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key,
    }


def _bounded_retry_after_seconds(
    headers: Mapping[str, str],
    *,
    fallback_seconds: float,
    maximum_seconds: float,
) -> float:
    value = response_header(headers, "retry-after")
    if value is None:
        return fallback_seconds
    try:
        retry_after = int(value.strip())
    except ValueError:
        return fallback_seconds
    if not 1 <= retry_after <= maximum_seconds:
        return fallback_seconds
    return float(retry_after)


@dataclass(frozen=True, slots=True)
class DeployedAcaSmokeConfig:
    """Operator-provided public endpoint and Entra token contract."""

    base_url: str
    agent_slug: str
    token_scope: str
    audience: str
    timeout_seconds: float

    @property
    def chat_url(self) -> str:
        return f"{self.base_url}/agents/{self.agent_slug}/chat"

    def management_urls(self, *, session_id: str, run_id: str) -> dict[str, str]:
        """Build controller-owned paths rather than duplicating their route contract."""

        paths = management_urls(
            agent_slug=self.agent_slug,
            context=RunContext(session_id=session_id, run_id=run_id),
        )
        return {name: f"{self.base_url}{path}" for name, path in paths.items()}


@dataclass(frozen=True, slots=True)
class SseEvent:
    """One replayable event frame emitted by the public management endpoint."""

    sequence: int
    payload: dict[str, object]
    event_name: str | None = None


@dataclass(frozen=True, slots=True)
class AcceptedRun:
    """Validated public LRO ticket returned by the controller submission route."""

    session_id: str
    run_id: str
    management_urls: Mapping[str, str]


class SseResponseStatusError(AcaSmokeEnvironmentError):
    """Redacted diagnostic for a public SSE response that cannot be parsed as events."""

    def __init__(self, url: str, status: int) -> None:
        self.status = status
        self.status_classification = _http_status_classification(status)
        super().__init__(
            "Function App SSE endpoint returned a non-success response "
            f"({self.status_classification}, HTTP {status}) at "
            f"{redact_deployed_aca_evidence(url)}."
        )


@dataclass(frozen=True, slots=True, repr=False)
class AuthorizationEvidence:
    """Validated app-only authorization material without a printable token representation."""

    authorization_header: str
    tenant_id: str
    object_id: str

    def __repr__(self) -> str:
        return "AuthorizationEvidence(<redacted>)"


def deployed_aca_smoke_enabled() -> bool:
    """Return whether an operator explicitly authorized the paid live qualification."""

    return os.environ.get(_LIVE_GATE_ENV) == "1"


def deployed_aca_smoke_config_from_environment() -> DeployedAcaSmokeConfig:
    """Load the explicit, secret-free deployed Function test contract."""

    if os.environ.get(_BEARER_TOKEN_ENV):
        raise AcaSmokeEnvironmentError(
            f"{_BEARER_TOKEN_ENV} is prohibited; acquire an app-only token with DefaultAzureCredential."
        )
    base_url = _required_function_base_url(_BASE_URL_ENV)
    agent_slug = _required_value(_AGENT_SLUG_ENV)
    if _SLUG_PATTERN.fullmatch(agent_slug) is None:
        raise AcaSmokeEnvironmentError(
            f"{_AGENT_SLUG_ENV} must be the lowercase built-in endpoint slug."
        )
    token_scope = _required_value(_TOKEN_SCOPE_ENV)
    if not token_scope.endswith("/.default"):
        raise AcaSmokeEnvironmentError(f"{_TOKEN_SCOPE_ENV} must end with '/.default'.")
    audience = _required_value(_AUDIENCE_ENV)
    scope_audience = token_scope.removesuffix("/.default").rstrip("/")
    accepted_audiences = {scope_audience}
    if scope_audience.startswith("api://"):
        accepted_audiences.add(scope_audience.removeprefix("api://"))
    if audience not in accepted_audiences:
        raise AcaSmokeEnvironmentError(
            f"{_AUDIENCE_ENV} must match the requested resource URI or its application client ID."
        )
    timeout_seconds = _timeout_from_environment()
    return DeployedAcaSmokeConfig(
        base_url=base_url,
        agent_slug=agent_slug,
        token_scope=token_scope,
        audience=audience,
        timeout_seconds=timeout_seconds,
    )


def deployed_aca_timeout_recovery_config_from_environment() -> DeployedAcaSmokeConfig:
    """Load the fixed fixture route and timeout needed by one-shot recovery coverage."""
    config = deployed_aca_smoke_config_from_environment()
    if config.agent_slug != _TIMEOUT_RECOVERY_AGENT_SLUG:
        raise AcaSmokeEnvironmentError(
            f"{_AGENT_SLUG_ENV} must be {_TIMEOUT_RECOVERY_AGENT_SLUG!r} for the controlled "
            "setup-timeout recovery fixture."
        )
    if config.timeout_seconds < _TIMEOUT_RECOVERY_MINIMUM_TIMEOUT_SECONDS:
        raise AcaSmokeEnvironmentError(
            f"{_TIMEOUT_ENV} must be at least "
            f"{_TIMEOUT_RECOVERY_MINIMUM_TIMEOUT_SECONDS:.0f} seconds for the controlled "
            "setup-timeout recovery fixture."
        )
    return config


def submission_payload(prompt: str) -> dict[str, object]:
    """Build the public chat submission body accepted by the registered endpoint."""

    return {"prompt": prompt}


def parse_accepted_run(payload: Mapping[str, object], config: DeployedAcaSmokeConfig) -> AcceptedRun:
    """Validate the controller's accepted-LRO payload and stable management route contract."""

    session_id = payload.get("session_id")
    run_id = payload.get("run_id")
    if not isinstance(session_id, str) or not session_id:
        raise AssertionError("Accepted run payload must contain a non-empty session_id.")
    if not isinstance(run_id, str) or not run_id:
        raise AssertionError("Accepted run payload must contain a non-empty run_id.")
    routes = config.management_urls(session_id=session_id, run_id=run_id)
    for name, url in routes.items():
        payload_name = name.removesuffix("_url")
        expected_path = url.removeprefix(config.base_url)
        if payload.get(f"{payload_name}_url") != expected_path:
            raise AssertionError(f"Accepted run payload has an invalid {payload_name}_url.")
    return AcceptedRun(session_id=session_id, run_id=run_id, management_urls=routes)


async def acquire_authorization_header(credential: _TokenCredential, scope: str) -> str:
    """Acquire one app-only token and format the only authorization header this test accepts.

    Unit tests may supply a narrow credential stub to verify this wiring. Such a stub
    does not prove Entra authentication or Azure authorization.
    """

    return f"Bearer {await _acquire_token_value(credential, scope)}"


async def acquire_default_authorization_header(scope: str) -> str:
    """Acquire an app-only token through the deployed test's real Azure credential chain."""

    credential = DefaultAzureCredential()
    try:
        return await acquire_authorization_header(credential, scope)
    finally:
        await credential.close()


async def acquire_default_authorization_evidence(scope: str) -> AuthorizationEvidence:
    """Acquire the request token and validate its owner claims only for local correlation."""
    credential = DefaultAzureCredential()
    try:
        return await acquire_authorization_evidence(credential, scope)
    finally:
        await credential.close()


async def acquire_authorization_evidence(
    credential: _TokenCredential,
    scope: str,
) -> AuthorizationEvidence:
    """Acquire and locally validate the same token that is sent to Easy Auth."""
    token = await _acquire_token_value(credential, scope)
    tenant_id, object_id = _validated_token_owner(token)
    return AuthorizationEvidence(
        authorization_header=f"Bearer {token}",
        tenant_id=tenant_id,
        object_id=object_id,
    )


async def _acquire_token_value(credential: _TokenCredential, scope: str) -> str:
    try:
        token = await credential.get_token(scope)
    except Exception as exc:
        raise AcaSmokeEnvironmentError(
            f"DefaultAzureCredential could not acquire the Easy Auth app-only token: "
            f"{redact_deployed_aca_evidence(str(exc))}"
        ) from exc
    value = getattr(token, "token", None)
    if not isinstance(value, str) or not value.strip():
        raise AcaSmokeEnvironmentError("DefaultAzureCredential returned an empty Easy Auth token.")
    return value


def _validated_token_owner(token: str) -> tuple[str, str]:
    """Extract only validated tenant/object IDs from a JWT payload without emitting it."""
    try:
        _, payload_segment, _ = token.split(".")
        payload_segment += "=" * (-len(payload_segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_segment))
        if not isinstance(payload, dict):
            _invalid_token_owner()
        tenant_id = payload.get("tid")
        object_id = payload.get("oid")
        if not isinstance(tenant_id, str) or not isinstance(object_id, str):
            _invalid_token_owner()
        principal = EntraPrincipal.create(
            tenant_id=tenant_id,
            object_id=object_id,
        )
    except (ValueError, TypeError, json.JSONDecodeError, SessionStateContractError) as exc:
        raise AcaSmokeEnvironmentError(
            "DefaultAzureCredential returned a token without valid tid and oid ownership claims."
        ) from exc
    return principal.tenant_id, principal.object_id


def _invalid_token_owner() -> NoReturn:
    raise ValueError("token ownership claims are invalid")


async def json_request(
    session: ClientSession,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    payload: dict[str, object] | None = None,
    retry_throttled: bool = True,
) -> tuple[int, dict[str, object], Mapping[str, str]]:
    """Make one public JSON request without logging prompt, result, or credentials."""

    for attempt in range(1, _JSON_THROTTLE_MAX_ATTEMPTS + 1):
        try:
            async with session.request(method, url, headers=headers, json=payload) as response:
                status = response.status
                resp_headers: Mapping[str, str] = dict(response.headers)
                frontend_unavailable = _is_frontend_unavailable_response(
                    status,
                    resp_headers,
                )
                if frontend_unavailable:
                    await response.read()
                    body: dict[str, object] = {}
                else:
                    body = await _json_body(response)
        except (TimeoutError, OSError) as exc:
            raise AcaSmokeEnvironmentError(
                f"Function App was unavailable at {redact_deployed_aca_evidence(url)}: "
                f"{type(exc).__name__}"
            ) from exc

        if not retry_throttled:
            if frontend_unavailable:
                raise AcaSmokeEnvironmentError(f"Function App returned HTTP {status}.")
            return status, body, resp_headers
        is_final = attempt == _JSON_THROTTLE_MAX_ATTEMPTS
        if frontend_unavailable:
            if (
                is_final
                or not _request_can_retry_frontend_unavailable(method, headers)
            ):
                raise AcaSmokeEnvironmentError(f"Function App returned HTTP {status}.")
            delay = optional_retry_after_seconds(
                resp_headers,
                maximum_seconds=_JSON_THROTTLE_RETRY_AFTER_MAXIMUM_SECONDS,
            )
            await asyncio.sleep(
                delay
                if delay is not None
                else _FRONTEND_UNAVAILABLE_RETRY_SECONDS
            )
            continue
        if status not in _JSON_THROTTLE_STATUSES:
            return status, body, resp_headers
        if is_final:
            return status, body, resp_headers
        delay = optional_retry_after_seconds(
            resp_headers, maximum_seconds=_JSON_THROTTLE_RETRY_AFTER_MAXIMUM_SECONDS
        )
        if delay is None:
            return status, body, resp_headers
        await asyncio.sleep(delay)

    raise AssertionError("unreachable")


async def read_sse_events(
    session: ClientSession,
    url: str,
    *,
    headers: Mapping[str, str],
) -> tuple[int, list[SseEvent], Mapping[str, str]]:
    """Read a bounded public SSE response using the controller's emitted frame shape."""
    status, events, response_headers, _ = await read_sse_events_with_first_event_time(
        session,
        url,
        headers=headers,
    )
    return status, events, response_headers


async def read_sse_until_matching_event(
    session: ClientSession,
    url: str,
    *,
    headers: Mapping[str, str],
    matches: Callable[[SseEvent], bool],
    overall_timeout_seconds: float = 120.0,
) -> tuple[int, SseEvent | None, Mapping[str, str]]:
    """Read one public SSE stream until one strictly ordered matching event arrives."""
    try:
        async with asyncio.timeout(overall_timeout_seconds):
            for attempt in range(1, _SSE_THROTTLE_MAX_ATTEMPTS + 1):
                is_final = attempt == _SSE_THROTTLE_MAX_ATTEMPTS
                async with session.get(url, headers=headers) as response:
                    status = response.status
                    response_headers = dict(response.headers)
                    if status != 200:
                        delay = sse_throttle_retry_delay(
                            status, response_headers, is_final_attempt=is_final
                        )
                        if delay is None:
                            raise SseResponseStatusError(url, status)
                        await asyncio.sleep(delay)
                        continue
                    events: list[SseEvent] = []
                    pending = ""
                    async for chunk in response.content:
                        pending += chunk.decode("utf-8").replace("\r\n", "\n")
                        frames = pending.split("\n\n")
                        pending = frames.pop()
                        for event in parse_sse_frames(frames):
                            events = append_contiguous_sse_events(events, [event])
                            if matches(event):
                                return status, event, response_headers
            raise SseResponseStatusError(url, status)
    except TimeoutError as exc:
        raise AcaSmokeEnvironmentError(
            "Function App SSE stream did not emit the required event before the overall deadline."
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise AcaSmokeEnvironmentError(
            f"Function App SSE endpoint was unavailable at {redact_deployed_aca_evidence(url)}: "
            f"{type(exc).__name__}"
        ) from exc
    raise AssertionError("Public SSE stream ended before the required event.")


async def read_sse_events_with_first_event_time(
    session: ClientSession,
    url: str,
    *,
    headers: Mapping[str, str],
    overall_timeout_seconds: float = 240.0,
) -> tuple[int, list[SseEvent], Mapping[str, str], float | None]:
    """Reconnect public SSE with ``Last-Event-ID`` until the terminal ``done`` event."""
    status, events, response_headers, first_event_at, _ = (
        await read_sse_events_with_observation_times(
            session,
            url,
            headers=headers,
            overall_timeout_seconds=overall_timeout_seconds,
        )
    )
    return status, events, response_headers, first_event_at


async def read_sse_events_with_observation_times(
    session: ClientSession,
    url: str,
    *,
    headers: Mapping[str, str],
    overall_timeout_seconds: float = 240.0,
) -> tuple[int, list[SseEvent], Mapping[str, str], float | None, tuple[float, ...]]:
    """Reconnect public SSE and record client-side observation times for each event."""
    deadline = time.perf_counter() + overall_timeout_seconds
    events: list[SseEvent] = []
    observed_at: list[float] = []
    first_event_at: float | None = None
    response_headers: Mapping[str, str] = {}
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise AcaSmokeEnvironmentError(
                "Function App SSE stream did not reach done before the overall deadline."
            )
        request_headers = dict(headers)
        if events:
            request_headers["Last-Event-ID"] = str(events[-1].sequence)
        try:
            async with asyncio.timeout(remaining):
                (
                    status,
                    segment,
                    response_headers,
                    segment_first_event_at,
                    segment_observed_at,
                ) = await _read_sse_response(
                    session,
                    url,
                    headers=request_headers,
                )
        except TimeoutError as exc:
            raise AcaSmokeEnvironmentError(
                "Function App SSE stream did not reach done before the overall deadline."
            ) from exc
        events = append_contiguous_sse_events(events, segment)
        observed_at.extend(segment_observed_at)
        if first_event_at is None and segment_first_event_at is not None:
            first_event_at = segment_first_event_at
        if events and events[-1].payload.get("type") == "done":
            return status, events, response_headers, first_event_at, tuple(observed_at)
        if time.perf_counter() >= deadline:
            raise AcaSmokeEnvironmentError(
                "Function App SSE stream did not reach done before the overall deadline."
            )
        await asyncio.sleep(0.1)


async def _read_sse_response(
    session: ClientSession,
    url: str,
    *,
    headers: Mapping[str, str],
) -> tuple[int, list[SseEvent], Mapping[str, str], float | None, tuple[float, ...]]:
    """Read one public SSE response, which may end at the server lease boundary."""
    for attempt in range(1, _SSE_THROTTLE_MAX_ATTEMPTS + 1):
        is_final = attempt == _SSE_THROTTLE_MAX_ATTEMPTS
        try:
            async with session.get(url, headers=headers) as response:
                status = response.status
                response_headers = dict(response.headers)
                if status != 200:
                    delay = sse_throttle_retry_delay(
                        status, response_headers, is_final_attempt=is_final
                    )
                    if delay is None:
                        raise SseResponseStatusError(url, status)
                    await asyncio.sleep(delay)
                    continue
                events: list[SseEvent] = []
                observed_at: list[float] = []
                first_event_at: float | None = None
                pending = ""
                async for chunk in response.content:
                    decoded = chunk.decode("utf-8")
                    pending += decoded.replace("\r\n", "\n")
                    frames = pending.split("\n\n")
                    pending = frames.pop()
                    parsed = parse_sse_frames(frames)
                    if parsed:
                        timestamp = time.perf_counter()
                        if first_event_at is None:
                            first_event_at = timestamp
                        events.extend(parsed)
                        observed_at.extend([timestamp] * len(parsed))
                if pending.strip():
                    parsed = parse_sse_frames([pending])
                    if parsed:
                        timestamp = time.perf_counter()
                        if first_event_at is None:
                            first_event_at = timestamp
                        events.extend(parsed)
                        observed_at.extend([timestamp] * len(parsed))
                return status, events, response_headers, first_event_at, tuple(observed_at)
        except AcaSmokeEnvironmentError:
            raise
        except (TimeoutError, OSError, UnicodeDecodeError) as exc:
            raise AcaSmokeEnvironmentError(
                f"Function App SSE endpoint was unavailable at "
                f"{redact_deployed_aca_evidence(url)}: {type(exc).__name__}"
            ) from exc
    raise SseResponseStatusError(url, status)


def append_contiguous_sse_events(
    prior: list[SseEvent],
    segment: list[SseEvent],
) -> list[SseEvent]:
    """Append a replay segment while rejecting duplicate, skipped, or reordered event IDs."""
    if not segment:
        return prior
    expected_sequence = prior[-1].sequence + 1 if prior else 1
    if [event.sequence for event in segment] != list(
        range(expected_sequence, expected_sequence + len(segment))
    ):
        raise AssertionError("Public SSE replay did not preserve strictly contiguous event IDs.")
    return [*prior, *segment]


def parse_sse_frames(frames: list[str]) -> list[SseEvent]:
    """Parse the minimal SSE fields emitted by ``controller.streaming.render_event``."""

    events: list[SseEvent] = []
    for frame in frames:
        stripped = frame.strip()
        if not stripped or stripped.startswith(":"):
            continue
        fields: dict[str, str] = {}
        for line in stripped.splitlines():
            name, separator, value = line.partition(":")
            if separator:
                fields[name] = value.lstrip()
        if "id" not in fields or "data" not in fields:
            raise ValueError("SSE event frame must include id and data fields.")
        try:
            sequence = int(fields["id"])
            decoded = json.loads(fields["data"])
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError("SSE event frame contains an invalid id or JSON payload.") from exc
        if not isinstance(decoded, dict):
            raise ValueError("SSE event payload must be a JSON object.")
        events.append(SseEvent(sequence=sequence, payload=decoded, event_name=fields.get("event")))
    return events

def redact_deployed_aca_evidence(value: str) -> str:
    """Remove URL queries, credentials, and bearer values from operator diagnostics."""

    parsed = urlsplit(value)
    if parsed.scheme and parsed.netloc:
        host = parsed.hostname or ""
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        value = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    value = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", value)
    value = re.sub(r"(?i)\b(token|secret|api[_-]?key)\s*[:=]\s*\S+", r"\1=[redacted]", value)
    return value


def client_timeout(config: DeployedAcaSmokeConfig) -> ClientTimeout:
    """Use the operator-selected limit for each public endpoint interaction."""

    return ClientTimeout(total=config.timeout_seconds)


def _required_function_base_url(name: str) -> str:
    value = _required_value(name)
    parsed = urlsplit(value)
    normalized_path = parsed.path.rstrip("/")
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or normalized_path not in ("", "/api")
    ):
        raise AcaSmokeEnvironmentError(
            f"{name} must be an HTTPS Function base URL without a path, credentials, "
            "query, or fragment, except for the /api route root."
        )
    return urlunsplit(("https", parsed.netloc, normalized_path, "", ""))


def _required_value(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise AcaSmokeEnvironmentError(f"{name} must be set to a non-blank value.")
    return value.strip()


def _timeout_from_environment() -> float:
    raw = _required_value(_TIMEOUT_ENV)
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise AcaSmokeEnvironmentError(
            f"{_TIMEOUT_ENV} must be a positive number of seconds."
        ) from exc
    if not 1 <= timeout <= 230:
        raise AcaSmokeEnvironmentError(f"{_TIMEOUT_ENV} must be between 1 and 230 seconds.")
    return timeout


def _http_status_classification(status: int) -> str:
    if 400 <= status <= 499:
        return "client_error"
    if 500 <= status <= 599:
        return "server_error"
    return "unexpected_status"


async def _json_body(response: ClientResponse) -> dict[str, object]:
    if response.status in {401, 403}:
        try:
            payload = await response.json(content_type=None)
        except (json.JSONDecodeError, UnicodeDecodeError):
            await response.read()
            return {}
        return payload if isinstance(payload, dict) else {}
    try:
        payload = await response.json(content_type=None)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        if response.status >= 502:
            raise AcaSmokeEnvironmentError(
                f"Function App returned HTTP {response.status}."
            ) from exc
        raise AssertionError(f"Expected JSON response, received HTTP {response.status}.") from exc
    if not isinstance(payload, dict):
        raise AssertionError("Expected a JSON object response.")
    return payload


