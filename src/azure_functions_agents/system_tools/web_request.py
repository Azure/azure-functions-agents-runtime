"""``web_request`` — built-in, default-on outbound HTTP(S) system tool.

Lets an agent make a single outbound HTTP(S) request to a **public** host
without generating and running code in the sandbox. Guarded by an always-on
SSRF security floor (global-unicast-only IP validation + DNS-rebind IP
pinning) that no configuration can disable.

v1 is deliberately minimal: a public, *unauthenticated* fetch. No redirect
following, no per-host auth injection, no wildcard host matching — see
``docs/frds/0005-web-request-system-tool.md`` for the full (v2+) design.

Tools are built **once per agent** at registration (stateless, unlike the
sandbox which needs the runtime session id); each invocation performs its own
validated request.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import urllib.parse
from dataclasses import dataclass
from typing import Any, Literal

import aiohttp
import aiohttp.abc
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .._function_tool import FunctionTool, tool
from .._logger import logger
from .._observability import (
    FaultDomain,
    LifecycleStage,
    record_web_request,
    start_span,
)
from ..config.schema import WebRequestConfig

# ---------------------------------------------------------------------------
# Absolute operational ceilings (worker resource-safety; never exceeded even
# if an operator configures a larger value). Documented in
# docs/frds/0005-web-request-system-tool.md §3/C7 and
# docs/front-matter-spec.md under system_tools.web_request.
#
#   timeout_seconds   — ceiling 120 s  (default 30 s)
#   max_response_bytes — ceiling 10 MB  (default 5 MB)
#   max_request_bytes  — ceiling 10 MB  (default 1 MB)
# ---------------------------------------------------------------------------

_MAX_TIMEOUT_SECONDS = 120.0
_MAX_RESPONSE_BYTES = 10_000_000
_MAX_REQUEST_BYTES = 10_000_000

_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_RESPONSE_BYTES = 5_000_000
_DEFAULT_MAX_REQUEST_BYTES = 1_000_000

_MIN_TIMEOUT_SECONDS = 0.1

_CHUNK_SIZE = 65536

_IMDS_ADDRESS = ipaddress.ip_address("169.254.169.254")
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_ULA_NETWORK = ipaddress.ip_network("fc00::/7")

# Request headers a caller cannot set — they could break framing or hijack
# routing/proxying.
_REQUEST_HEADER_DENYLIST = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "upgrade",
        "te",
        "trailer",
        "proxy-authorization",
        "proxy-connection",
    }
)

# Response headers stripped before the result reaches the model — hop-by-hop,
# cookie, and auth headers.
_RESPONSE_HEADER_REDACT = frozenset(
    {
        "set-cookie",
        "authorization",
        "proxy-authorization",
        "www-authenticate",
        "proxy-authenticate",
        "cookie",
    }
)

# A real hostname's last label (the TLD) is never all-digits. Any host whose
# every label is purely numeric or hex is almost certainly a non-canonical
# IPv4 literal (decimal/octal/hex/short-form) that some resolvers accept as an
# alternate address encoding — reject it rather than let it slip past the
# allowlist as a "hostname".
_ALL_NUMERIC_LABEL_RE = re.compile(r"^(0x[0-9a-fA-F]+|[0-9]+)$")

_WEB_REQUEST_DESCRIPTION = (
    "Make a single outbound HTTP(S) request to a public host and return a"
    " structured JSON result (status, headers, body).\n"
    "\n"
    "- `method` defaults to GET.\n"
    "- `body` (raw string) and `json` (any JSON value) are mutually"
    " exclusive; `json` sets Content-Type: application/json.\n"
    "- `query` is a dict of additional query-string parameters merged onto"
    " `url`.\n"
    "- The response `body` is parsed JSON when the content-type is JSON,"
    " otherwise raw text. Binary responses omit `body` (see"
    " `body_omitted_reason`). Oversized responses are truncated, never a hard"
    " error (`body_truncated: true`).\n"
    "- Only public, internet-routable hosts are reachable; internal/private"
    " network destinations are always blocked.\n"
    "- Redirects are not followed — a 3xx response is returned as-is."
)


# ---------------------------------------------------------------------------
# Pydantic param schema
# ---------------------------------------------------------------------------


class WebRequestParams(BaseModel):
    """Parameters the model supplies for a single ``web_request`` call."""

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"] = "GET"
    url: str = Field(description="Absolute http(s) URL to request.")
    headers: dict[str, str] | None = Field(
        default=None, description="Extra request headers (some are always dropped)."
    )
    query: dict[str, str] | None = Field(
        default=None, description="Additional query-string parameters merged onto `url`."
    )
    body: str | None = Field(default=None, description="Raw request body. Exclusive with `json`.")
    json_body: Any | None = Field(
        default=None,
        alias="json",
        description="JSON request body (sets Content-Type: application/json). Exclusive with `body`.",
    )

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def _check_body_json_exclusive(self) -> WebRequestParams:
        if self.body is not None and self.json_body is not None:
            raise ValueError("`body` and `json` are mutually exclusive")
        return self


# ---------------------------------------------------------------------------
# SSRF validator
# ---------------------------------------------------------------------------


class SSRFValidationError(Exception):
    """Raised when a URL fails the SSRF validator. ``reason`` is a short, safe-to-log category."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message or reason)
        self.reason = reason


@dataclass(frozen=True)
class ValidatedTarget:
    """Result of a successful SSRF validation, ready to pin a connection to."""

    scheme: str
    host: str
    port: int
    path: str
    query: str
    ips: tuple[str, ...]
    stripped_url: str


def _looks_like_disguised_ip(host: str) -> bool:
    """Return ``True`` when every label of ``host`` is purely numeric/hex.

    Catches decimal (``2130706433``), octal (``0177.0.0.1``), hex
    (``0x7f000001``), and short-form (``127.1``) IPv4 literal encodings that
    :func:`ipaddress.ip_address` rejects but some platform resolvers still
    accept via ``inet_aton``-style parsing.
    """
    labels = host.split(".")
    return bool(labels) and all(_ALL_NUMERIC_LABEL_RE.match(label) for label in labels)


def _parse_url(url: str) -> urllib.parse.SplitResult:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise SSRFValidationError("malformed_url", f"could not parse URL: {exc}") from exc
    if not parsed.scheme or not parsed.hostname:
        raise SSRFValidationError("malformed_url", "URL must include a scheme and host")
    return parsed


def _reject_userinfo(parsed: urllib.parse.SplitResult) -> None:
    if parsed.username is not None or parsed.password is not None:
        raise SSRFValidationError("userinfo_not_allowed", "URL must not contain embedded userinfo")


def _validate_scheme(parsed: urllib.parse.SplitResult, *, require_https: bool) -> str:
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise SSRFValidationError("unsupported_scheme", f"unsupported scheme '{scheme}'")
    if require_https and scheme != "https":
        raise SSRFValidationError(
            "https_required", "http is disabled; set require_https: false to allow it"
        )
    return scheme


def _validate_port(parsed: urllib.parse.SplitResult, scheme: str) -> int:
    try:
        port = parsed.port
    except ValueError as exc:
        raise SSRFValidationError("invalid_port", "invalid port") from exc
    if port is None:
        port = 443 if scheme == "https" else 80
    if not (0 < port <= 65535):
        raise SSRFValidationError("invalid_port", "port out of range")
    return port


def _normalize_host(hostname: str) -> str:
    host = hostname.strip().lower()
    if host.endswith("."):
        host = host[:-1]
    if not host:
        raise SSRFValidationError("malformed_url", "empty host")
    return host


def _enforce_allowlist(host: str, allowed_hosts: tuple[str, ...] | None) -> None:
    if allowed_hosts is None:
        return
    if host not in allowed_hosts:
        raise SSRFValidationError("not_in_allowlist", f"host '{host}' is not in allowed_hosts")


def _unwrap_ipv4_mapped(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            return mapped
    return ip


def _classify_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Return a block-reason category for a non-global-unicast address, else ``None``."""
    ip = _unwrap_ipv4_mapped(ip)

    if ip == _IMDS_ADDRESS:
        return "imds"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link_local"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified"
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_NETWORK:
        return "cgnat"
    if isinstance(ip, ipaddress.IPv6Address) and ip in _ULA_NETWORK:
        return "ula"
    if ip.is_private:
        return "private"
    if ip.is_reserved:
        return "reserved"
    if not ip.is_global:
        return "reserved"
    return None


async def _default_resolve_host(host: str) -> list[str]:
    """Resolve ``host`` to its A/AAAA addresses using the platform resolver."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    seen: set[str] = set()
    ips: list[str] = []
    for _family, _socktype, _proto, _canonname, sockaddr in infos:
        ip = sockaddr[0]
        if ip not in seen:
            seen.add(ip)
            ips.append(ip)
    return ips


# Injectable module-level resolver — tests monkeypatch this name directly so
# the SSRF validator never touches real DNS/network.
_resolve_host = _default_resolve_host


def _format_netloc(host: str, port: int, scheme: str) -> str:
    display_host = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    if port == default_port:
        return display_host
    return f"{display_host}:{port}"


async def _validate_target(
    url: str,
    *,
    allowed_hosts: tuple[str, ...] | None,
    require_https: bool,
) -> ValidatedTarget:
    """Run the full SSRF validator and return a target pinned to validated IP(s)."""
    parsed = _parse_url(url)
    _reject_userinfo(parsed)
    scheme = _validate_scheme(parsed, require_https=require_https)
    port = _validate_port(parsed, scheme)
    host = _normalize_host(parsed.hostname or "")
    _enforce_allowlist(host, allowed_hosts)

    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None

    if literal_ip is None and _looks_like_disguised_ip(host):
        raise SSRFValidationError(
            "non_canonical_ip", f"host '{host}' looks like a disguised IP literal"
        )

    if literal_ip is not None:
        candidate_ips: list[str] = [str(literal_ip)]
    else:
        try:
            candidate_ips = await _resolve_host(host)
        except SSRFValidationError:
            raise
        except Exception as exc:
            raise SSRFValidationError("dns_resolution_failed", str(exc)) from exc
        if not candidate_ips:
            raise SSRFValidationError("dns_resolution_failed", "no addresses resolved")

    validated_ips: list[str] = []
    for raw_ip in candidate_ips:
        try:
            ip_obj = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise SSRFValidationError(
                "dns_resolution_failed", f"resolver returned invalid address '{raw_ip}'"
            ) from exc
        reason = _classify_blocked_ip(ip_obj)
        if reason is not None:
            raise SSRFValidationError(reason, f"destination address blocked ({reason})")
        validated_ips.append(str(ip_obj))

    path = parsed.path or "/"
    netloc = _format_netloc(host, port, scheme)
    stripped_url = urllib.parse.urlunsplit((scheme, netloc, path, "", ""))

    return ValidatedTarget(
        scheme=scheme,
        host=host,
        port=port,
        path=path,
        query=parsed.query,
        ips=tuple(validated_ips),
        stripped_url=stripped_url,
    )


# ---------------------------------------------------------------------------
# Pinned DNS resolver / connector (defeats DNS rebinding)
# ---------------------------------------------------------------------------


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    """An ``aiohttp`` resolver that always returns the pre-validated IP(s).

    Connections are pinned to exactly the IP(s) the SSRF validator checked —
    a later DNS answer (rebind) can never be used, and DNS caching is
    disabled on the connector so no stale decision can outlive this request.
    """

    def __init__(self, ips: tuple[str, ...]) -> None:
        self._ips = ips

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[aiohttp.abc.ResolveResult]:
        results: list[aiohttp.abc.ResolveResult] = []
        for ip in self._ips:
            ip_family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            results.append(
                aiohttp.abc.ResolveResult(
                    hostname=host,
                    host=ip,
                    port=port,
                    family=ip_family,
                    proto=0,
                    flags=0,
                )
            )
        return results

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Header policy
# ---------------------------------------------------------------------------


def _sanitize_request_headers(headers: dict[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {key: value for key, value in headers.items() if key.lower() not in _REQUEST_HEADER_DENYLIST}


def _redact_response_headers(headers: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in _RESPONSE_HEADER_REDACT:
            continue
        result[key] = value
    return result


# ---------------------------------------------------------------------------
# Response shaping
# ---------------------------------------------------------------------------


def _content_type_mime(content_type: str | None) -> str:
    if not content_type:
        return ""
    return content_type.split(";", 1)[0].strip().lower()


def _is_json_content_type(content_type: str | None) -> bool:
    mime = _content_type_mime(content_type)
    return mime == "application/json" or mime == "text/json" or mime.endswith("+json")


def _is_binary_content_type(content_type: str | None) -> bool:
    mime = _content_type_mime(content_type)
    if not mime:
        return False
    if mime.startswith("text/"):
        return False
    textual_hints = (
        "json",
        "xml",
        "javascript",
        "ecmascript",
        "x-www-form-urlencoded",
        "csv",
        "yaml",
        "graphql",
    )
    return not any(hint in mime for hint in textual_hints)


async def _read_capped(response: aiohttp.ClientResponse, max_bytes: int) -> tuple[bytes, bool]:
    """Read up to ``max_bytes`` of the response body; report whether more remained."""
    chunks: list[bytes] = []
    total = 0
    truncated = False
    async for chunk in response.content.iter_chunked(_CHUNK_SIZE):
        remaining = max_bytes - total
        if remaining <= 0:
            truncated = True
            break
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            total += remaining
            truncated = True
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), truncated


def _shape_body(
    *,
    method: str,
    body_bytes: bytes,
    content_type: str | None,
    truncated: bool,
) -> tuple[Any, str | None]:
    if method == "HEAD":
        return None, "head"
    if _is_binary_content_type(content_type):
        return None, "binary"

    text = body_bytes.decode("utf-8", errors="replace")
    if truncated:
        # A truncated body is never parsed — it may be malformed JSON.
        return text, None
    if _is_json_content_type(content_type):
        try:
            return json.loads(text), None
        except (json.JSONDecodeError, ValueError):
            return text, None
    return text, None


# ---------------------------------------------------------------------------
# Config clamping / normalization
# ---------------------------------------------------------------------------


def _clamp_config(config: WebRequestConfig) -> tuple[float, int, int]:
    timeout = config.timeout_seconds if config.timeout_seconds is not None else _DEFAULT_TIMEOUT_SECONDS
    timeout = max(_MIN_TIMEOUT_SECONDS, min(timeout, _MAX_TIMEOUT_SECONDS))

    max_response = (
        config.max_response_bytes
        if config.max_response_bytes is not None
        else _DEFAULT_MAX_RESPONSE_BYTES
    )
    max_response = max(1, min(max_response, _MAX_RESPONSE_BYTES))

    max_request = (
        config.max_request_bytes if config.max_request_bytes is not None else _DEFAULT_MAX_REQUEST_BYTES
    )
    max_request = max(1, min(max_request, _MAX_REQUEST_BYTES))

    return timeout, max_response, max_request


def _normalize_allowed_hosts(hosts: list[str] | None) -> tuple[str, ...] | None:
    if hosts is None:
        return None
    normalized: list[str] = []
    for raw_host in hosts:
        try:
            normalized.append(_normalize_host(raw_host))
        except SSRFValidationError:
            logger.warning("web_request: ignoring invalid allowed_hosts entry '%s'", raw_host)
    return tuple(normalized)


def _encode_query(query: dict[str, str] | None) -> str:
    if not query:
        return ""
    return urllib.parse.urlencode(query)


# ---------------------------------------------------------------------------
# Factory: create the (build-once, per-agent) web_request tool
# ---------------------------------------------------------------------------


def create_web_request_tools(config: WebRequestConfig) -> list[FunctionTool]:
    """Create the ``web_request`` tool bound to a resolved agent's config.

    Built once per agent at registration; each invocation performs its own
    SSRF-validated, IP-pinned request. Always returns exactly one tool — v1
    has no config shape that makes the tool itself invalid.
    """
    timeout_seconds, max_response_bytes, max_request_bytes = _clamp_config(config)
    allowed_hosts = _normalize_allowed_hosts(config.allowed_hosts)
    require_https = config.require_https

    @tool(name="web_request", description=_WEB_REQUEST_DESCRIPTION, schema=WebRequestParams)
    async def web_request(params: WebRequestParams) -> str:
        method = params.method
        with start_span(
            "web_request",
            fault_domain=FaultDomain.WEB_REQUEST,
            lifecycle_stage=LifecycleStage.TOOL_EXECUTION,
            attributes={"http.request.method": method},
        ) as span:
            try:
                target = await _validate_target(
                    params.url,
                    allowed_hosts=allowed_hosts,
                    require_https=require_https,
                )
            except SSRFValidationError as exc:
                span.set_attribute("af.web_request.blocked_reason", exc.reason)
                span.set_error(
                    f"web_request blocked: {exc.reason}", fault_domain=FaultDomain.WEB_REQUEST
                )
                record_web_request(error=True)
                logger.warning("web_request: blocked (%s)", exc.reason)
                return json.dumps({"error": f"Request blocked: {exc.reason}"})

            span.set_attribute("server.address", target.host)
            span.set_attribute("url.scheme", target.scheme)

            request_headers = _sanitize_request_headers(params.headers)
            request_body: bytes | None = None
            if params.json_body is not None:
                request_body = json.dumps(params.json_body).encode("utf-8")
                request_headers["Content-Type"] = "application/json"
            elif params.body is not None:
                request_body = params.body.encode("utf-8")

            if request_body is not None and len(request_body) > max_request_bytes:
                record_web_request(error=True)
                span.set_error(
                    "request body exceeds max_request_bytes", fault_domain=FaultDomain.WEB_REQUEST
                )
                return json.dumps(
                    {"error": f"request body exceeds max_request_bytes ({max_request_bytes})"}
                )

            query_parts = [part for part in (target.query, _encode_query(params.query)) if part]
            request_url = urllib.parse.urlunsplit(
                (
                    target.scheme,
                    _format_netloc(target.host, target.port, target.scheme),
                    target.path,
                    "&".join(query_parts),
                    "",
                )
            )

            connector = aiohttp.TCPConnector(
                resolver=_PinnedResolver(target.ips),
                use_dns_cache=False,
                ttl_dns_cache=0,
                force_close=True,
            )
            try:
                async with aiohttp.ClientSession(connector=connector) as session, session.request(
                    method,
                    request_url,
                    headers=request_headers,
                    data=request_body,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                ) as response:
                    if method == "HEAD":
                        body_bytes, truncated = b"", False
                    else:
                        body_bytes, truncated = await _read_capped(response, max_response_bytes)
                    status = response.status
                    content_type = response.headers.get("Content-Type")
                    response_headers = _redact_response_headers(response.headers)
            except TimeoutError:
                record_web_request(error=True)
                span.set_error("web_request timed out", fault_domain=FaultDomain.WEB_REQUEST)
                logger.warning("web_request: timed out after %ss", timeout_seconds)
                return json.dumps({"error": "request timed out"})
            except aiohttp.ClientError as exc:
                record_web_request(error=True)
                # Use type name only — aiohttp exception messages can embed the
                # full request URL (including query string) which would leak
                # secrets or confidential query parameters back to the model.
                safe_msg = type(exc).__name__
                span.set_error(safe_msg, fault_domain=FaultDomain.WEB_REQUEST)
                logger.warning("web_request: transport error (%s)", safe_msg)
                return json.dumps({"error": f"request failed: {safe_msg}"})

            body, body_omitted_reason = _shape_body(
                method=method,
                body_bytes=body_bytes,
                content_type=content_type,
                truncated=truncated,
            )

            span.set_attribute("http.response.status_code", status)
            span.set_attribute("af.web_request.response_bytes", len(body_bytes))
            span.set_attribute("af.web_request.body_truncated", truncated)
            record_web_request(error=False)

            result: dict[str, Any] = {
                "status": status,
                "url": target.stripped_url,
                "content_type": content_type,
                "redirect_count": 0,
                "response_headers": response_headers,
                "body": body,
                "body_truncated": truncated,
                "body_omitted_reason": body_omitted_reason,
            }
            if body_omitted_reason == "binary":
                # Body is omitted for binary content, but the caller still
                # needs to know how large the (unreturned) payload was.
                result["response_bytes"] = len(body_bytes)

            return json.dumps(result)

    return [web_request]
