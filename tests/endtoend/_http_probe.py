"""HTTP invocation + assertion helpers for the end-to-end tests.

These build on :func:`tests.endtoend._func_host.running_host` to talk to a live
``func start`` host: discover which HTTP endpoints it exposes (via the Functions
admin API), invoke them with any HTTP method, and assert on the responses.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import requests

# Default per-request timeout. Agent invocations can be slow, so keep this
# generous; callers can override per call.
DEFAULT_TIMEOUT = 60.0


@dataclass(frozen=True)
class HttpEndpoint:
    """One HTTP-triggered route exposed by a running host."""

    function_name: str
    route: str
    methods: tuple[str, ...]
    auth_level: str

    def url(self, base_url: str) -> str:
        """Absolute URL for this route against ``base_url``."""
        return f"{base_url.rstrip('/')}/{self.route.lstrip('/')}"

    def allows(self, method: str) -> bool:
        """Whether this endpoint is registered for ``method`` (case-insensitive)."""
        return method.upper() in self.methods


@dataclass(frozen=True)
class AdminFunction:
    """A function indexed by the host, as reported by ``/admin/functions``.

    ``trigger_type`` is the raw binding type (e.g. ``httpTrigger``,
    ``timerTrigger``, ``blobTrigger``). ``route``/``methods`` are only populated
    for HTTP triggers.
    """

    name: str
    trigger_type: str
    route: str | None = None
    methods: tuple[str, ...] = ()
    auth_level: str | None = None


class HttpClient:
    """Thin wrapper over :class:`requests.Session` bound to a base URL."""

    def __init__(self, base_url: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """Send ``method`` to ``path`` (relative to the base URL or absolute)."""
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        kwargs.setdefault("timeout", self.timeout)
        return self._session.request(method.upper(), url, **kwargs)

    def get(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("DELETE", path, **kwargs)

    def head(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("HEAD", path, **kwargs)

    def options(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("OPTIONS", path, **kwargs)

    def wait_until_responsive(self, *, timeout: float = 30.0, poll: float = 0.5) -> None:
        """Block until the host answers the admin API (or raise on timeout)."""
        admin = f"{self.base_url}/admin/functions"
        deadline = time.monotonic() + timeout
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                resp = self._session.get(admin, timeout=5.0)
                if resp.status_code < 500:
                    return
            except requests.RequestException as exc:  # pragma: no cover - timing
                last_err = exc
            time.sleep(poll)
        raise TimeoutError(f"host at {self.base_url} did not become responsive: {last_err}")


def discover_http_endpoints(client: HttpClient, *, timeout: float = 30.0) -> list[HttpEndpoint]:
    """Return every ``httpTrigger`` route the host exposes, via ``/admin/functions``.

    This is the authoritative "what endpoints are exposed" mechanism: it reads
    each indexed function's bindings and extracts the route, methods, and auth
    level for HTTP triggers.
    """
    resp = client.get("/admin/functions", timeout=timeout)
    resp.raise_for_status()

    endpoints: list[HttpEndpoint] = []
    for fn in resp.json():
        config = fn.get("config") or {}
        function_name = fn.get("name") or config.get("name") or ""
        for binding in config.get("bindings") or []:
            if str(binding.get("type", "")).lower() != "httptrigger":
                continue
            route = binding.get("route")
            if route is None:
                continue
            methods = tuple(str(m).upper() for m in (binding.get("methods") or []))
            endpoints.append(
                HttpEndpoint(
                    function_name=str(function_name),
                    route=str(route),
                    methods=methods,
                    auth_level=str(binding.get("authLevel", "")).lower(),
                )
            )
    return endpoints


def discover_functions(client: HttpClient, *, timeout: float = 30.0) -> list[AdminFunction]:
    """Return every function the host indexed, keyed by its primary trigger.

    Reads ``/admin/functions`` and records the first trigger binding for each
    function (HTTP, timer, blob, queue, etc.), populating route/methods/auth for
    HTTP triggers. This is the discovery mechanism used to locate non-HTTP
    triggers (e.g. timers) that are invoked via the admin API.
    """
    resp = client.get("/admin/functions", timeout=timeout)
    resp.raise_for_status()

    functions: list[AdminFunction] = []
    for fn in resp.json():
        config = fn.get("config") or {}
        name = str(fn.get("name") or config.get("name") or "")
        for binding in config.get("bindings") or []:
            btype = str(binding.get("type", ""))
            if binding.get("direction", "IN").upper() == "OUT" or btype.lower() == "http":
                # Skip output bindings and the plain http return binding.
                continue
            route = binding.get("route")
            methods = tuple(str(m).upper() for m in (binding.get("methods") or []))
            functions.append(
                AdminFunction(
                    name=name,
                    trigger_type=btype,
                    route=str(route) if route is not None else None,
                    methods=methods,
                    auth_level=(
                        str(binding.get("authLevel")).lower()
                        if binding.get("authLevel") is not None
                        else None
                    ),
                )
            )
            break  # first (trigger) binding only
    return functions


def find_functions(
    functions: Iterable[AdminFunction], *, trigger_type: str
) -> list[AdminFunction]:
    """Return functions whose trigger binding type matches ``trigger_type``."""
    wanted = trigger_type.lower()
    return [fn for fn in functions if fn.trigger_type.lower() == wanted]


def invoke_admin_function(
    client: HttpClient,
    function_name: str,
    *,
    data: str = "",
    timeout: float = 60.0,
) -> requests.Response:
    """Invoke a non-HTTP function via ``POST /admin/functions/{name}``.

    This is how timer (and other non-HTTP) triggers are fired on demand locally.
    The admin endpoint accepts ``{"input": <data>}`` and returns ``202 Accepted``;
    the function then runs in the background.
    """
    return client.post(
        f"/admin/functions/{function_name}",
        json={"input": data},
        timeout=timeout,
    )


def find_endpoint(
    endpoints: Iterable[HttpEndpoint],
    *,
    route_suffix: str | None = None,
    route_exact: str | None = None,
    method: str | None = None,
) -> HttpEndpoint:
    """Return the single endpoint matching the given filters, or raise.

    Filters are ANDed together. ``route_suffix`` matches routes ending with the
    given string; ``route_exact`` matches the full route; ``method`` requires the
    endpoint to allow that HTTP method.
    """
    matches = [
        ep
        for ep in endpoints
        if (route_exact is None or ep.route == route_exact)
        and (route_suffix is None or ep.route.endswith(route_suffix))
        and (method is None or ep.allows(method))
    ]
    if not matches:
        available = ", ".join(f"{ep.route} {ep.methods}" for ep in endpoints) or "<none>"
        raise AssertionError(
            "no endpoint matched "
            f"(route_exact={route_exact!r}, route_suffix={route_suffix!r}, method={method!r}); "
            f"available: {available}"
        )
    if len(matches) > 1:
        found = ", ".join(f"{ep.route} {ep.methods}" for ep in matches)
        raise AssertionError(f"expected exactly one endpoint, found {len(matches)}: {found}")
    return matches[0]


def _summarize(resp: requests.Response) -> str:
    body = resp.text
    if len(body) > 500:
        body = body[:500] + "...(truncated)"
    return f"{resp.request.method} {resp.url} -> {resp.status_code}\nbody: {body!r}"


def expect_status(resp: requests.Response, *expected: int) -> requests.Response:
    """Assert the response has one of ``expected`` status codes."""
    assert resp.status_code in expected, (
        f"expected status in {expected}, got {resp.status_code}\n{_summarize(resp)}"
    )
    return resp


def expect_json(resp: requests.Response) -> Any:
    """Assert the response body is JSON and return the parsed value."""
    try:
        return resp.json()
    except ValueError as exc:  # pragma: no cover - diagnostic path
        raise AssertionError(f"response was not valid JSON: {exc}\n{_summarize(resp)}") from exc


def expect_header(resp: requests.Response, name: str) -> str:
    """Assert a header is present and return its value."""
    value = resp.headers.get(name)
    assert value is not None, f"expected header {name!r} to be present\n{_summarize(resp)}"
    return value


def expect_body_contains(resp: requests.Response, needle: str) -> requests.Response:
    """Assert the response body contains ``needle`` (case-insensitive)."""
    assert needle.lower() in resp.text.lower(), (
        f"expected body to contain {needle!r}\n{_summarize(resp)}"
    )
    return resp


def expect_json_keys(resp: requests.Response, keys: Mapping[str, object] | Iterable[str]) -> Any:
    """Assert the JSON body is an object containing all of ``keys``."""
    payload = expect_json(resp)
    assert isinstance(payload, dict), f"expected a JSON object\n{_summarize(resp)}"
    wanted = keys.keys() if isinstance(keys, Mapping) else keys
    missing = [k for k in wanted if k not in payload]
    assert not missing, f"missing JSON keys {missing}\n{_summarize(resp)}"
    return payload
