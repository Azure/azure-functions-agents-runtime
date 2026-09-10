"""Experimental native A2A 1.0 registration for the P3 simple profile."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import AsyncGenerator, Mapping
from typing import TYPE_CHECKING, Any, Never

import azure.functions as func
from a2a.server.context import ServerCallContext
from a2a.server.events import Event
from a2a.server.request_handlers.request_handler import (
    RequestHandler,
    validate_request_params,
)
from a2a.server.request_handlers.response_helpers import build_error_response
from a2a.server.routes.common import (
    DefaultServerCallContextBuilder,
    ServerCallContextBuilder,
)
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.types import (
    AgentCapabilities as A2AAgentCapabilities,
)
from a2a.types import (
    AgentCard,
    AgentInterface,
    AgentSkill,
    APIKeySecurityScheme,
    CancelTaskRequest,
    DeleteTaskPushNotificationConfigRequest,
    GetExtendedAgentCardRequest,
    GetTaskPushNotificationConfigRequest,
    GetTaskRequest,
    HTTPAuthSecurityScheme,
    InternalError,
    InvalidAgentResponseError,
    InvalidParamsError,
    ListTaskPushNotificationConfigsRequest,
    ListTaskPushNotificationConfigsResponse,
    ListTasksRequest,
    ListTasksResponse,
    Role,
    SecurityRequirement,
    SecurityScheme,
    SendMessageRequest,
    StringList,
    SubscribeToTaskRequest,
    Task,
    TaskPushNotificationConfig,
    UnsupportedOperationError,
    VersionNotSupportedError,
)
from a2a.types import (
    Message as A2AMessage,
)
from agent_framework import (
    AgentRunInputs,
    AgentSession,
    ServiceSessionId,
)
from agent_framework import (
    Message as MAFMessage,
)
from agent_framework_hosting_a2a import AgentA2AAdapter
from azurefunctions.extensions.http.fastapi import Request, Response
from google.protobuf.json_format import MessageToDict  # type: ignore[import-untyped]

from .. import __version__
from .._logger import logger
from .._observability import FaultDomain, LifecycleStage, start_span
from .._source_marker import source_marker
from ..config import EndpointAuthConfig, ResolvedAgent
from ._auth import (
    authorize_entra_request,
    resolve_authorized_request_scope,
    resolve_endpoint_auth_level,
)
from ._handlers import _set_run_result_attributes
from ._naming import _safe_function_name
from .capabilities import AgentCapabilities
from .catalog import AgentCatalog
from .endpoints import _run_builtin_agent

if TYPE_CHECKING:
    from ..workflows.schema import WorkflowPlanPolicy

_MAX_IN_FLIGHT = 32
_MAX_REQUEST_BYTES = 256 * 1024
_MAX_PARTS = 16
_MAX_TEXT_PART_BYTES = 32 * 1024
_MAX_TOTAL_TEXT_BYTES = 64 * 1024
_MAX_RESPONSE_TEXT_BYTES = 256 * 1024

_AUTH_SCOPE_STATE = "azure_functions_agents.a2a.auth_scope"
_DURABLE_CLIENT_STATE = "azure_functions_agents.a2a.durable_client"


class _A2ACardMetadataTarget:
    """Minimal MAF agent-shaped metadata target used by the public card adapter."""

    def __init__(self, resolved: ResolvedAgent) -> None:
        self.id = resolved.slug
        self.name = resolved.name
        self.description = resolved.description
        self.context_providers: tuple[()] = ()

    def run(
        self,
        messages: AgentRunInputs | None = None,
        *,
        stream: bool = False,
        session: AgentSession | None = None,
        function_invocation_kwargs: Mapping[str, Any] | None = None,
        client_kwargs: Mapping[str, Any] | None = None,
    ) -> Never:
        del messages, stream, session, function_invocation_kwargs, client_kwargs
        raise RuntimeError("The A2A card metadata target is not an execution path.")

    def create_session(self, *, session_id: str | None = None) -> AgentSession:
        return AgentSession(session_id=session_id)

    def get_session(
        self,
        service_session_id: str | ServiceSessionId,
        *,
        session_id: str | None = None,
    ) -> AgentSession:
        return AgentSession(
            service_session_id=service_session_id,
            session_id=session_id,
        )


class _ExecutionLimiter:
    """Process-local, non-queuing execution bound for one agent."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._active = 0
        self._lock = threading.Lock()

    def try_enter(self) -> bool:
        with self._lock:
            if self._active >= self._limit:
                return False
            self._active += 1
            return True

    def exit(self) -> None:
        with self._lock:
            if self._active <= 0:
                raise RuntimeError("A2A execution limiter released without an active request.")
            self._active -= 1


class _A2ACallContextBuilder(ServerCallContextBuilder):
    def __init__(self) -> None:
        self._default = DefaultServerCallContextBuilder()

    def build(self, request: Request) -> ServerCallContext:
        context = self._default.build(request)
        context.state[_AUTH_SCOPE_STATE] = request.scope.get(_AUTH_SCOPE_STATE)
        context.state[_DURABLE_CLIENT_STATE] = request.scope.get(_DURABLE_CLIENT_STATE)
        return context


def _unsupported(operation: str) -> Never:
    raise UnsupportedOperationError(
        message=f"{operation} is not supported by the A2A simple profile."
    )


def _runner_session_id(auth_scope: str, agent_slug: str, context_id: str) -> str:
    scoped_context = f"{auth_scope}\0{agent_slug}\0{context_id}"
    digest = hashlib.sha256(scoped_context.encode("utf-8")).hexdigest()
    return f"a2a-{digest}"


def _validate_simple_message(params: SendMessageRequest) -> None:
    message = params.message
    if message.role != Role.ROLE_USER:
        raise InvalidParamsError(message="A2A simple requests require a user Message.")
    if message.task_id or message.reference_task_ids:
        raise UnsupportedOperationError(
            message="Task continuation is not supported by the A2A simple profile."
        )
    if message.extensions:
        raise UnsupportedOperationError(
            message="Message extensions are not supported by the A2A simple profile."
        )
    if len(message.parts) > _MAX_PARTS:
        raise InvalidParamsError(message=f"A2A Messages may contain at most {_MAX_PARTS} Parts.")

    total_bytes = 0
    for part in message.parts:
        if part.WhichOneof("content") != "text":
            raise InvalidParamsError(
                message="A2A simple requests support text Parts only."
            )
        part_bytes = len(part.text.encode("utf-8"))
        if part_bytes > _MAX_TEXT_PART_BYTES:
            raise InvalidParamsError(
                message=f"Each A2A text Part is limited to {_MAX_TEXT_PART_BYTES} bytes."
            )
        total_bytes += part_bytes
    if total_bytes == 0:
        raise InvalidParamsError(message="A2A Messages require non-empty text.")
    if total_bytes > _MAX_TOTAL_TEXT_BYTES:
        raise InvalidParamsError(
            message=f"A2A Message text is limited to {_MAX_TOTAL_TEXT_BYTES} bytes in total."
        )

    if params.HasField("configuration"):
        configuration = params.configuration
        if configuration.HasField("task_push_notification_config"):
            raise UnsupportedOperationError(
                message="Push notifications are not supported by the A2A simple profile."
            )
        if configuration.accepted_output_modes and "text/plain" not in {
            mode.lower() for mode in configuration.accepted_output_modes
        }:
            raise InvalidParamsError(
                message="The A2A simple profile produces text/plain responses only."
            )


def _prompt_from_a2a(adapter: AgentA2AAdapter[_A2ACardMetadataTarget], message: A2AMessage) -> str:
    run_args = adapter.a2a_to_run(
        message,
        stream=False,
        validate_modes=False,
    )
    messages = run_args["messages"]
    if (
        not isinstance(messages, list)
        or len(messages) != 1
        or not isinstance(messages[0], MAFMessage)
        or not messages[0].text.strip()
    ):
        raise InvalidParamsError(message="A2A Message could not be converted to text input.")
    return messages[0].text


class _SimpleA2ARequestHandler(RequestHandler):
    def __init__(
        self,
        *,
        adapter: AgentA2AAdapter[_A2ACardMetadataTarget],
        resolved: ResolvedAgent,
        capabilities: AgentCapabilities,
        workflows_enabled: bool,
        workflow_system_addendum: str | None,
        catalog: AgentCatalog | None,
        workflow_policy: WorkflowPlanPolicy | None,
    ) -> None:
        self._adapter = adapter
        self._resolved = resolved
        self._capabilities = capabilities
        self._workflows_enabled = workflows_enabled
        self._workflow_system_addendum = workflow_system_addendum
        self._catalog = catalog
        self._workflow_policy = workflow_policy
        self._limiter = _ExecutionLimiter(_MAX_IN_FLIGHT)

    @validate_request_params
    async def on_message_send(
        self,
        params: SendMessageRequest,
        context: ServerCallContext,
    ) -> Task | A2AMessage:
        _validate_simple_message(params)

        auth_scope = context.state.get(_AUTH_SCOPE_STATE)
        if not isinstance(auth_scope, str) or not auth_scope:
            raise InternalError(message="Authorized A2A request scope is unavailable.")

        context_id = params.message.context_id or uuid.uuid4().hex
        session_id = _runner_session_id(
            auth_scope,
            self._resolved.slug,
            context_id,
        )
        prompt = _prompt_from_a2a(self._adapter, params.message)

        if not self._limiter.try_enter():
            raise UnsupportedOperationError(
                message=(
                    f"The agent already has {_MAX_IN_FLIGHT} A2A executions in flight; "
                    "retry later."
                )
            )

        with start_span(
            f"agent.run {self._resolved.slug}",
            lifecycle_stage=LifecycleStage.AGENT_RUN,
            attributes={
                "af.agent.name": self._resolved.slug,
                "af.agent.display_name": self._resolved.name,
                "af.agent.trigger_type": "builtin_a2a",
                "af.agent.session_id": session_id,
                "af.agent.model": self._resolved.model,
            },
        ) as span:
            try:
                try:
                    result = await _run_builtin_agent(
                        prompt,
                        resolved=self._resolved,
                        capabilities=self._capabilities,
                        session_id=session_id,
                        workflows_enabled=self._workflows_enabled,
                        workflow_system_addendum=self._workflow_system_addendum,
                        durable_client=context.state.get(_DURABLE_CLIENT_STATE),
                        catalog=self._catalog,
                        workflow_policy=self._workflow_policy,
                    )
                except Exception as exc:
                    logger.exception(
                        "A2A agent execution failed: source_file=%s",
                        source_marker(self._resolved.source_file),
                    )
                    span.set_attribute("af.agent.outcome", "error")
                    span.record_exception(exc, fault_domain=FaultDomain.UNKNOWN)
                    raise InternalError(message="Agent execution failed.") from exc

                response_text = result.content
                if len(response_text.encode("utf-8")) > _MAX_RESPONSE_TEXT_BYTES:
                    span.set_attribute("af.agent.outcome", "error")
                    span.set_error(
                        "A2A response exceeded the simple profile limit.",
                        fault_domain=FaultDomain.APP,
                    )
                    raise InvalidAgentResponseError(
                        message=(
                            "Agent response exceeded the "
                            f"{_MAX_RESPONSE_TEXT_BYTES}-byte A2A simple profile limit."
                        )
                    )

                maf_message = MAFMessage("assistant", [response_text])
                parts = self._adapter.a2a_from_run(
                    maf_message,
                    validate_modes=False,
                )
                if not parts:
                    raise InvalidAgentResponseError(
                        message="Agent response did not contain A2A-compatible text."
                    )
                for part in parts:
                    part.ClearField("metadata")

                _set_run_result_attributes(span, result)
                span.set_attribute("af.agent.outcome", "success")
                return A2AMessage(
                    message_id=uuid.uuid4().hex,
                    context_id=context_id,
                    role=Role.ROLE_AGENT,
                    parts=parts,
                )
            finally:
                self._limiter.exit()

    async def on_get_task(
        self,
        params: GetTaskRequest,
        context: ServerCallContext,
    ) -> Task | None:
        del params, context
        _unsupported("GetTask")

    async def on_list_tasks(
        self,
        params: ListTasksRequest,
        context: ServerCallContext,
    ) -> ListTasksResponse:
        del params, context
        _unsupported("ListTasks")

    async def on_cancel_task(
        self,
        params: CancelTaskRequest,
        context: ServerCallContext,
    ) -> Task | None:
        del params, context
        _unsupported("CancelTask")

    async def on_message_send_stream(
        self,
        params: SendMessageRequest,
        context: ServerCallContext,
    ) -> AsyncGenerator[Event]:
        del params, context
        _unsupported("SendStreamingMessage")
        yield

    async def on_create_task_push_notification_config(
        self,
        params: TaskPushNotificationConfig,
        context: ServerCallContext,
    ) -> TaskPushNotificationConfig:
        del params, context
        _unsupported("CreateTaskPushNotificationConfig")

    async def on_get_task_push_notification_config(
        self,
        params: GetTaskPushNotificationConfigRequest,
        context: ServerCallContext,
    ) -> TaskPushNotificationConfig:
        del params, context
        _unsupported("GetTaskPushNotificationConfig")

    async def on_subscribe_to_task(
        self,
        params: SubscribeToTaskRequest,
        context: ServerCallContext,
    ) -> AsyncGenerator[Event]:
        del params, context
        _unsupported("SubscribeToTask")
        yield

    async def on_list_task_push_notification_configs(
        self,
        params: ListTaskPushNotificationConfigsRequest,
        context: ServerCallContext,
    ) -> ListTaskPushNotificationConfigsResponse:
        del params, context
        _unsupported("ListTaskPushNotificationConfigs")

    async def on_delete_task_push_notification_config(
        self,
        params: DeleteTaskPushNotificationConfigRequest,
        context: ServerCallContext,
    ) -> None:
        del params, context
        _unsupported("DeleteTaskPushNotificationConfig")

    async def on_get_extended_agent_card(
        self,
        params: GetExtendedAgentCardRequest,
        context: ServerCallContext,
    ) -> AgentCard:
        del params, context
        _unsupported("GetExtendedAgentCard")


def _security_metadata(
    auth: EndpointAuthConfig,
) -> tuple[str | None, SecurityScheme | None]:
    if auth.mode == "anonymous":
        return None, None
    if auth.mode == "entra":
        return (
            "entra",
            SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(
                    description="Microsoft Entra ID bearer token validated by App Service Authentication.",
                    scheme="bearer",
                    bearer_format="JWT",
                )
            ),
        )
    return (
        "function_key",
        SecurityScheme(
            api_key_security_scheme=APIKeySecurityScheme(
                description="Azure Functions host key.",
                location="header",
                name="x-functions-key",
            )
        ),
    )


async def _card_response(
    adapter: AgentA2AAdapter[_A2ACardMetadataTarget],
    auth: EndpointAuthConfig,
) -> Response:
    card = await adapter.get_card()
    security_name, security_scheme = _security_metadata(auth)
    if security_name is not None and security_scheme is not None:
        card.security_schemes[security_name].CopyFrom(security_scheme)
        requirement = SecurityRequirement()
        requirement.schemes[security_name].CopyFrom(StringList())
        card.security_requirements.append(requirement)
    body = MessageToDict(
        card,
        preserving_proto_field_name=False,
        always_print_fields_with_no_presence=True,
    )
    return Response(
        json.dumps(body, ensure_ascii=False),
        media_type="application/json",
    )


def _json_http_error(message: str, status_code: int) -> Response:
    return Response(
        json.dumps({"error": message}),
        status_code=status_code,
        media_type="application/json",
    )


async def _preflight_request(req: Request) -> Response | None:
    body = await req.body()
    if len(body) > _MAX_REQUEST_BYTES:
        return _json_http_error(
            f"A2A request bodies are limited to {_MAX_REQUEST_BYTES} bytes.",
            413,
        )
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _json_http_error("A2A request body must be valid UTF-8 JSON.", 400)
    if isinstance(payload, dict) and req.headers.get("a2a-version") != "1.0":
        request_id_value = payload.get("id")
        request_id = (
            request_id_value
            if isinstance(request_id_value, str)
            or (
                isinstance(request_id_value, int)
                and not isinstance(request_id_value, bool)
            )
            else None
        )
        error = VersionNotSupportedError(
            message="This endpoint supports A2A wire version '1.0' only."
        )
        return Response(
            json.dumps(build_error_response(request_id, error)),
            media_type="application/json",
        )
    return None


def register_a2a_endpoints(
    app: func.FunctionApp,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    *,
    slug: str,
    workflows_enabled: bool = False,
    workflow_system_addendum: str | None = None,
    catalog: AgentCatalog | None = None,
    workflow_policy: WorkflowPlanPolicy | None = None,
) -> None:
    """Register one per-agent card and JSON-RPC route for the P3 simple profile."""
    a2a_config = resolved.builtin_endpoints.a2a
    if a2a_config is None:
        raise ValueError("A2A endpoint registration requires builtin_endpoints.a2a.")

    auth = resolved.builtin_endpoints.http_auth
    adapter: AgentA2AAdapter[_A2ACardMetadataTarget] = AgentA2AAdapter(
        _A2ACardMetadataTarget(resolved),
        version=__version__,
        supported_interfaces=[
            AgentInterface(
                url=a2a_config.url,
                protocol_binding="JSONRPC",
                protocol_version="1.0",
            )
        ],
        skills=[
            AgentSkill(
                id=slug,
                name=resolved.name,
                description=resolved.description,
                tags=["agent"],
                examples=[],
                input_modes=["text/plain"],
                output_modes=["text/plain"],
            )
        ],
        infer_skills=False,
        capabilities=A2AAgentCapabilities(
            streaming=False,
            push_notifications=False,
            extended_agent_card=False,
        ),
        default_input_modes=("text/plain",),
        default_output_modes=("text/plain",),
    )
    handler = _SimpleA2ARequestHandler(
        adapter=adapter,
        resolved=resolved,
        capabilities=capabilities,
        workflows_enabled=workflows_enabled,
        workflow_system_addendum=workflow_system_addendum,
        catalog=catalog,
        workflow_policy=workflow_policy,
    )
    sdk_routes = create_jsonrpc_routes(
        handler,
        f"/agents/{slug}/a2a",
        context_builder=_A2ACallContextBuilder(),
        enable_v0_3_compat=False,
    )
    sdk_rpc_endpoint = sdk_routes[0].endpoint

    async def handle_card(req: Request) -> Response:
        auth_error = authorize_entra_request(req.headers.get, auth)
        if auth_error is not None:
            return _json_http_error(auth_error.message, auth_error.status_code)
        return await _card_response(adapter, auth)

    async def handle_rpc(req: Request, durable_client: Any | None) -> Response:
        auth_error, auth_scope = resolve_authorized_request_scope(req.headers.get, auth)
        if auth_error is not None or auth_scope is None:
            error = auth_error or RuntimeError("Authorized A2A request scope is unavailable.")
            return _json_http_error(
                str(error) if isinstance(error, RuntimeError) else error.message,
                500 if isinstance(error, RuntimeError) else error.status_code,
            )

        preflight_error = await _preflight_request(req)
        if preflight_error is not None:
            return preflight_error

        req.scope[_AUTH_SCOPE_STATE] = auth_scope
        req.scope[_DURABLE_CLIENT_STATE] = durable_client
        response = await sdk_rpc_endpoint(req)
        if not isinstance(response, Response):
            raise TypeError("The A2A SDK JSON-RPC route returned an invalid response type.")
        return response

    async def handle_rpc_without_client(req: Request) -> Response:
        return await handle_rpc(req, None)

    async def handle_rpc_with_client(req: Request, client: Any) -> Response:
        return await handle_rpc(req, client)

    base_function_name = _safe_function_name(f"agent_{slug}_a2a")
    handle_card.__name__ = f"{base_function_name}_card"
    handle_rpc_without_client.__name__ = f"{base_function_name}_rpc"
    handle_rpc_with_client.__name__ = f"{base_function_name}_rpc"
    decorated_card = app.route(
        route=f"agents/{slug}/.well-known/agent-card.json",
        methods=["GET"],
        auth_level=resolve_endpoint_auth_level(auth),
    )(handle_card)
    app.function_name(name=f"{base_function_name}_card")(decorated_card)

    decorated_rpc: Any
    if workflows_enabled:
        decorated_rpc = app.durable_client_input(client_name="client")(
            handle_rpc_with_client
        )
    else:
        decorated_rpc = handle_rpc_without_client
    decorated_rpc = app.route(
        route=f"agents/{slug}/a2a",
        methods=["POST"],
        auth_level=resolve_endpoint_auth_level(auth),
    )(decorated_rpc)
    app.function_name(name=f"{base_function_name}_rpc")(decorated_rpc)
