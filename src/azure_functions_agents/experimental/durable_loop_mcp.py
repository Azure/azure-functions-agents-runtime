"""Worker-side privileged remote MCP lane through the configured APIM frontend."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from agent_framework import Content, MCPStreamableHTTPTool

from ..strict_json import canonical_json_bytes
from .durable_loop_activities import (
    DurableContentStore,
    get_protocol_model,
    put_protocol_model,
)
from .durable_loop_observability import (
    DurableLoopOutcome,
    DurableLoopPhase,
    DurableLoopTimer,
)
from .durable_loop_protocol import (
    ErrorDisposition,
    ErrorEnvelopeV1,
    FrozenToolDescriptorV1,
    ToolBehavior,
    ToolProvenance,
    ToolRequestV1,
    ToolResultStatus,
    ToolResultV1,
)
from .durable_loop_receipts import (
    ActivityReceiptStatus,
    ActivityReceiptV1,
    DurableKeyedDocumentStore,
    create_activity_receipt,
    read_activity_receipt,
    replace_activity_receipt,
)
from .hybrid_apim import HybridApimClientManager


class DurableRemoteMcpError(RuntimeError):
    """One remote MCP call failed without exposing response content."""


class ConfiguredMcpServer(Protocol):
    """The discovery metadata reused to construct an APIM MCP session."""

    name: str
    allowed_tools: Collection[str] | None


class RemoteMcpSession(Protocol):
    """The narrow initialized MCP session used by the durable lane."""

    name: str

    @property
    def functions(self) -> Sequence[Any]:
        """Return loaded model-visible functions."""

    async def connect(self, *, reset: bool = False) -> None:
        """Initialize the MCP session and load tools."""

    async def call_tool(
        self,
        tool_name: str,
        **kwargs: Any,
    ) -> str | list[Content]:
        """Invoke one remote tool."""

    async def close(self) -> None:
        """Close the MCP session."""


type RemoteMcpSessionFactory = Callable[
    [ConfiguredMcpServer, Any],
    RemoteMcpSession,
]


class DurableRemoteMcpLane:
    """Initialize, catalog, and invoke remote MCP tools in the trusted worker."""

    def __init__(
        self,
        *,
        manager: HybridApimClientManager,
        base_url: str,
        configured: Sequence[ConfiguredMcpServer],
        content: DurableContentStore,
        receipts: DurableKeyedDocumentStore,
        session_factory: RemoteMcpSessionFactory | None = None,
    ) -> None:
        self._manager = manager
        self._base_url = _validate_base_url(base_url)
        self._configured = tuple(configured)
        self._content = content
        self._receipts = receipts
        self._lock = asyncio.Lock()
        self._connected = False
        self._servers: list[RemoteMcpSession] = []
        self._routes: dict[str, tuple[RemoteMcpSession, str]] = {}
        self._server_locks: dict[int, asyncio.Lock] = {}
        self._session_factory = session_factory or self._default_session

    async def discover(self) -> tuple[FrozenToolDescriptorV1, ...]:
        """Initialize APIM MCP sessions and return exact model-visible schemas."""
        await self._ensure_connected()
        return tuple(
            FrozenToolDescriptorV1(
                name=name,
                description=function.description,
                parameters=dict(function.parameters()),
                provenance=ToolProvenance.REMOTE,
                behavior=ToolBehavior.MUTATING,
                parallel_safe=False,
            )
            for name, (_server, _remote, function) in sorted(
                (
                    name,
                    (server, remote, _function_by_name(server, name)),
                )
                for name, (server, remote) in self._routes.items()
            )
        )

    async def dispatch(  # noqa: PLR0912
        self,
        request: ToolRequestV1,
    ) -> ToolResultV1:
        """Invoke one bounded remote MCP call with request-bound receipts."""
        if request.provenance is not ToolProvenance.REMOTE:
            raise DurableRemoteMcpError("remote MCP lane received a non-remote tool")
        receipt_key = f"activities/remote-mcp/{request.call_key}"
        existing = await read_activity_receipt(self._receipts, receipt_key)
        if existing is not None:
            receipt, _revision = existing
            _validate_receipt(receipt, request)
            if receipt.result_ref is not None:
                return (
                    await get_protocol_model(
                        self._content,
                        receipt.result_ref,
                        ToolResultV1,
                    )
                ).model_copy(update={"deduplicated": True})
            if request.behavior is ToolBehavior.MUTATING:
                return _ambiguous_result(request)
        else:
            started = ActivityReceiptV1(
                operation_key=request.call_key,
                request_hash=request.request_hash,
                kind="remote_mcp",
                status=ActivityReceiptStatus.STARTED,
                attempt=1,
                updated_at=datetime.now(UTC),
            )
            if not await create_activity_receipt(
                self._receipts,
                receipt_key,
                started,
            ):
                return await self.dispatch(request)

        await self._ensure_connected()
        route = self._routes.get(request.tool_name)
        if route is None:
            return await self._commit(
                receipt_key,
                request,
                _failed_result(
                    request,
                    code="remote_mcp_tool_not_found",
                    classification="policy",
                ),
            )
        server, remote_name = route
        timer = DurableLoopTimer(DurableLoopPhase.MCP_CALL, provenance="remote")
        try:
            if request.behavior is ToolBehavior.READ_ONLY:
                result = await server.call_tool(remote_name, **request.arguments)
            else:
                async with self._server_locks[id(server)]:
                    _ACTIVE_CALL_KEYS[str(id(server))] = request.call_key
                    try:
                        result = await server.call_tool(
                            remote_name,
                            **request.arguments,
                        )
                    finally:
                        _ACTIVE_CALL_KEYS[str(id(server))] = None
            value = _mcp_value(result)
            if len(canonical_json_bytes(value)) > request.result_byte_limit:
                response = _failed_result(
                    request,
                    code="remote_mcp_result_too_large",
                    classification="budget",
                )
            else:
                response = ToolResultV1(
                    run_id=request.run_id,
                    step_index=request.step_index,
                    call_ordinal=request.call_ordinal,
                    provider_call_id=request.provider_call_id,
                    call_key=request.call_key,
                    request_hash=request.request_hash,
                    tool_name=request.tool_name,
                    status=ToolResultStatus.SUCCEEDED,
                    value=value,
                    elapsed_ms=0.0,
                )
            committed = await self._commit(receipt_key, request, response)
            timer.finish(
                DurableLoopOutcome.COMPLETED
                if committed.status is ToolResultStatus.SUCCEEDED
                else DurableLoopOutcome.FAILED
            )
            return committed
        except asyncio.CancelledError:
            timer.finish(DurableLoopOutcome.CANCELLED)
            response = (
                _ambiguous_result(request)
                if request.behavior is ToolBehavior.MUTATING
                else _failed_result(
                    request,
                    code="remote_mcp_cancelled",
                    classification="cancellation",
                    status=ToolResultStatus.CANCELLED,
                )
            )
            await self._commit(receipt_key, request, response)
            raise
        except Exception:
            timer.finish(DurableLoopOutcome.FAILED)
            response = (
                _ambiguous_result(request)
                if request.behavior is ToolBehavior.MUTATING
                else _failed_result(
                    request,
                    code="remote_mcp_failed",
                    classification="connector",
                    retryable=request.behavior is ToolBehavior.READ_ONLY,
                )
            )
            return await self._commit(receipt_key, request, response)

    async def close(self) -> None:
        """Close all worker-side MCP sessions."""
        for server in self._servers:
            await server.close()
        self._servers.clear()
        self._routes.clear()
        self._connected = False

    async def _ensure_connected(self) -> None:
        if self._connected:
            return
        async with self._lock:
            if self._connected:
                return
            routes: dict[str, tuple[RemoteMcpSession, str]] = {}
            servers: list[RemoteMcpSession] = []
            for configured in self._configured:
                active_headers: dict[str, str | None] = {"call_key": None}
                http_client = _build_http_client(
                    self._manager,
                    self._base_url,
                    active_headers,
                )
                server = self._session_factory(configured, http_client)
                _ACTIVE_CALL_KEYS[str(id(server))] = None
                self._server_locks[id(server)] = asyncio.Lock()
                active_headers["server_id"] = str(id(server))
                await server.connect()
                for function in server.functions:
                    remote_name = _remote_name(function)
                    if function.name in routes:
                        await server.close()
                        raise DurableRemoteMcpError(
                            "remote MCP tool names are not collision-free"
                        )
                    routes[function.name] = (server, remote_name)
                servers.append(server)
            self._routes = routes
            self._servers = servers
            self._connected = True

    def _default_session(
        self,
        configured: ConfiguredMcpServer,
        http_client: Any,
    ) -> RemoteMcpSession:
        return MCPStreamableHTTPTool(
            name=configured.name,
            url=self._base_url,
            allowed_tools=configured.allowed_tools,
            load_tools=True,
            load_prompts=False,
            terminate_on_close=False,
            http_client=http_client,
        )

    async def _commit(
        self,
        receipt_key: str,
        request: ToolRequestV1,
        result: ToolResultV1,
    ) -> ToolResultV1:
        result_ref = await put_protocol_model(
            self._content,
            kind="tool-result",
            model=result,
        )
        current = await read_activity_receipt(self._receipts, receipt_key)
        if current is None:
            raise DurableRemoteMcpError("remote MCP receipt disappeared")
        receipt, revision = current
        _validate_receipt(receipt, request)
        updated = receipt.model_copy(
            update={
                "result_ref": result_ref,
                "status": (
                    ActivityReceiptStatus.SUCCEEDED
                    if result.status is ToolResultStatus.SUCCEEDED
                    else (
                        ActivityReceiptStatus.AMBIGUOUS
                        if result.status is ToolResultStatus.AMBIGUOUS
                        else ActivityReceiptStatus.FAILED
                    )
                ),
                "updated_at": datetime.now(UTC),
            }
        )
        if not await replace_activity_receipt(
            self._receipts,
            receipt_key,
            updated,
            revision=revision,
        ):
            replay = await read_activity_receipt(self._receipts, receipt_key)
            if replay is None or replay[0].result_ref is None:
                raise DurableRemoteMcpError("remote MCP receipt fence changed")
            return (
                await get_protocol_model(
                    self._content,
                    replay[0].result_ref,
                    ToolResultV1,
                )
            ).model_copy(update={"deduplicated": True})
        return result


def _build_http_client(
    manager: HybridApimClientManager,
    base_url: str,
    active: dict[str, str | None],
) -> Any:
    from httpx import URL, AsyncClient, Request, Timeout

    origin = _origin(URL(base_url))

    async def inject(request: Request) -> None:
        if _origin(request.url) != origin:
            return
        extra: dict[str, str] = {}
        server_id = active.get("server_id")
        if server_id is not None:
            call_key = _ACTIVE_CALL_KEYS.get(server_id)
            if call_key is not None:
                extra["x-af-call-key"] = call_key
        request.headers.update(await manager.request_headers(extra))

    return AsyncClient(
        follow_redirects=False,
        timeout=Timeout(30.0, read=120.0),
        event_hooks={"request": [inject]},
    )


_ACTIVE_CALL_KEYS: dict[str, str | None] = {}


def _origin(url: Any) -> tuple[str, str, int]:
    port = url.port or (443 if url.scheme == "https" else 80)
    return str(url.scheme), str(url.host), int(port)


def _validate_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("durable MCP APIM base must be a credential-free HTTPS URL")
    return value.rstrip("/")


def _remote_name(function: Any) -> str:
    properties = function.additional_properties or {}
    remote = properties.get("_mcp_remote_name")
    return remote if isinstance(remote, str) and remote else function.name


def _function_by_name(server: RemoteMcpSession, name: str) -> Any:
    for function in server.functions:
        if function.name == name:
            return function
    raise DurableRemoteMcpError("remote MCP catalog changed")


def _mcp_value(result: str | list[Content]) -> object:
    if isinstance(result, str):
        return result
    return [content.to_dict() for content in result]


def _validate_receipt(
    receipt: ActivityReceiptV1,
    request: ToolRequestV1,
) -> None:
    if receipt.operation_key != request.call_key or receipt.request_hash != request.request_hash:
        raise DurableRemoteMcpError("remote MCP receipt request changed")


def _failed_result(
    request: ToolRequestV1,
    *,
    code: str,
    classification: str,
    status: ToolResultStatus = ToolResultStatus.FAILED,
    retryable: bool = False,
) -> ToolResultV1:
    return ToolResultV1(
        run_id=request.run_id,
        step_index=request.step_index,
        call_ordinal=request.call_ordinal,
        provider_call_id=request.provider_call_id,
        call_key=request.call_key,
        request_hash=request.request_hash,
        tool_name=request.tool_name,
        status=status,
        elapsed_ms=0.0,
        error=ErrorEnvelopeV1(
            code=code,
            classification=classification,
            retryable=retryable,
            phase="tool_step",
            step_index=request.step_index,
            call_key=request.call_key,
        ),
    )


def _ambiguous_result(request: ToolRequestV1) -> ToolResultV1:
    return ToolResultV1(
        run_id=request.run_id,
        step_index=request.step_index,
        call_ordinal=request.call_ordinal,
        provider_call_id=request.provider_call_id,
        call_key=request.call_key,
        request_hash=request.request_hash,
        tool_name=request.tool_name,
        status=ToolResultStatus.AMBIGUOUS,
        elapsed_ms=0.0,
        error=ErrorEnvelopeV1(
            code="remote_mcp_effect_unreconciled",
            classification="connector",
            retryable=False,
            disposition=ErrorDisposition.AMBIGUOUS,
            possibly_committed=True,
            phase="tool_step",
            step_index=request.step_index,
            call_key=request.call_key,
        ),
    )
