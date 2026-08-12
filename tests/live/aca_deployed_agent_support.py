"""Public-HTTP helpers for the manually qualified deployed ACA agent test."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from aiohttp import ClientResponse, ClientSession, ClientTimeout
from azure.identity.aio import DefaultAzureCredential
from tests.aca_smoke_diagnostics import AcaSmokeEnvironmentError

from azure_functions_agents.controller.http import management_urls
from azure_functions_agents.execution.backend import RunContext

_LIVE_GATE_ENV = "AZURE_FUNCTIONS_AGENTS_RUN_DEPLOYED_ACA_SMOKE"
_BASE_URL_ENV = "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_FUNCTION_BASE_URL"
_AGENT_SLUG_ENV = "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_AGENT_SLUG"
_TOKEN_SCOPE_ENV = "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_TOKEN_SCOPE"
_AUDIENCE_ENV = "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_AUDIENCE"
_TIMEOUT_ENV = "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TIMEOUT_SECONDS"
_BEARER_TOKEN_ENV = "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_BEARER_TOKEN"
_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class _TokenCredential(Protocol):
    async def get_token(self, *scopes: str) -> object: ...


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
    if token_scope != f"{audience.rstrip('/')}/.default":
        raise AcaSmokeEnvironmentError(
            f"{_TOKEN_SCOPE_ENV} must be the .default scope for {_AUDIENCE_ENV}."
        )
    timeout_seconds = _timeout_from_environment()
    return DeployedAcaSmokeConfig(
        base_url=base_url,
        agent_slug=agent_slug,
        token_scope=token_scope,
        audience=audience,
        timeout_seconds=timeout_seconds,
    )


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
    return f"Bearer {value}"


async def acquire_default_authorization_header(scope: str) -> str:
    """Acquire an app-only token through the deployed test's real Azure credential chain."""

    credential = DefaultAzureCredential()
    try:
        return await acquire_authorization_header(credential, scope)
    finally:
        await credential.close()


async def json_request(
    session: ClientSession,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    payload: dict[str, object] | None = None,
) -> tuple[int, dict[str, object], Mapping[str, str]]:
    """Make one public JSON request without logging prompt, result, or credentials."""

    try:
        async with session.request(method, url, headers=headers, json=payload) as response:
            if response.status >= 502:
                raise AcaSmokeEnvironmentError(
                    f"Function App was unavailable at {redact_deployed_aca_evidence(url)} "
                    f"(HTTP {response.status})."
                )
            return response.status, await _json_body(response), dict(response.headers)
    except (TimeoutError, OSError) as exc:
        raise AcaSmokeEnvironmentError(
            f"Function App was unavailable at {redact_deployed_aca_evidence(url)}: "
            f"{type(exc).__name__}"
        ) from exc


async def read_sse_events(
    session: ClientSession,
    url: str,
    *,
    headers: Mapping[str, str],
) -> tuple[int, list[SseEvent], Mapping[str, str]]:
    """Read a bounded public SSE response using the controller's emitted frame shape."""

    try:
        async with session.get(url, headers=headers) as response:
            status = response.status
            response_headers = dict(response.headers)
            if status >= 502:
                _raise_unavailable_response(url, status)
            chunks: list[str] = []
            async for chunk in response.content:
                chunks.append(chunk.decode("utf-8"))
            body = "".join(chunks).replace("\r\n", "\n")
            return status, parse_sse_frames(body.split("\n\n")), response_headers
    except AcaSmokeEnvironmentError:
        raise
    except (TimeoutError, OSError, UnicodeDecodeError) as exc:
        raise AcaSmokeEnvironmentError(
            f"Function App SSE endpoint was unavailable at {redact_deployed_aca_evidence(url)}: "
            f"{type(exc).__name__}"
        ) from exc


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


def _raise_unavailable_response(url: str, status: int) -> None:
    raise AcaSmokeEnvironmentError(
        f"Function App was unavailable at {redact_deployed_aca_evidence(url)} (HTTP {status})."
    )


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
