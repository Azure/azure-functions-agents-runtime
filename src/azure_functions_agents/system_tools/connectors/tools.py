from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from urllib.parse import quote

from ..._function_tool import FunctionTool
from ..._logger import logger
from .arm import ArmClient, DataPlaneClient
from .connectors import ConnectionInfo, ParsedOperation, ParsedParameter

ToolArgs = dict[str, object]
JsonSchema = dict[str, object]


def _sanitize_name(name: str) -> str:
    """Sanitize parameter name to match ^[a-zA-Z0-9_.-]{1,64}$."""
    sanitized = re.sub(r"[^a-zA-Z0-9_.\-]", "_", name)
    return sanitized[:64]


def _to_snake_case(name: str) -> str:
    """Convert operationId to snake_case."""
    snake = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", snake)
    snake = re.sub(r"[^a-zA-Z0-9]", "_", snake)
    snake = re.sub(r"_+", "_", snake)
    return snake.strip("_").lower()


def _param_to_json_schema(param: ParsedParameter) -> JsonSchema:
    """Convert a ParsedParameter to a JSON Schema property."""
    type_map = {"integer": "integer", "number": "number", "boolean": "boolean"}
    schema: JsonSchema = {"type": type_map.get(param.type, "string")}
    if param.description:
        schema["description"] = param.description
    if param.enum:
        schema["enum"] = param.enum
    if param.default is not None:
        schema["default"] = param.default
    return schema


def _build_invoke_path(
    op: ParsedOperation,
    args: ToolArgs,
    all_params: list[ParsedParameter],
    *,
    url_encode: bool = True,
) -> str:
    """Build the invoke path by stripping /{connectionId} and substituting path params.

    When url_encode is False (V1 dynamicInvoke), path param values are inserted
    as-is since the path is a JSON field, not a real URL. When True (V2 data
    plane), values are percent-encoded for use in an HTTP URL.
    """
    path = re.sub(r"^/\{connectionId\}", "", op.path, flags=re.IGNORECASE)
    for param in all_params:
        if param.location == "path":
            sanitized = _sanitize_name(param.name)
            value = args.get(sanitized)
            if value is None:
                raise ValueError(f"Missing required path parameter: {param.name}")
            replacement = quote(str(value), safe="") if url_encode else str(value)
            path = path.replace(f"{{{param.name}}}", replacement)

    for param in op.internal_params:
        if param.location == "path" and param.default is not None:
            replacement = quote(str(param.default), safe="") if url_encode else str(param.default)
            path = path.replace(f"{{{param.name}}}", replacement)
    return path


def _set_nested_value(body: ToolArgs, dotted_name: str, value: object) -> None:
    head, tail = dotted_name.split(".", 1)
    nested = body.get(head)
    if not isinstance(nested, dict):
        nested = {}
        body[head] = nested
    nested[tail] = value


def generate_tools(
    arm: ArmClient,
    connection: ConnectionInfo,
    prefix: str | None = None,
    data_plane_client: DataPlaneClient | None = None,
) -> list[FunctionTool]:
    """Generate MAF :class:`FunctionTool` objects for each operation in a connection.

    The tools' parameter schemas are built as raw OpenAPI-style dicts and passed
    to :class:`FunctionTool` via ``input_model=``. MAF surfaces them to the LLM
    using its standard function-calling envelope.

    Tool names are ``{effective_prefix}_{api_name}_{operation_id}`` where:

    - ``prefix`` from frontmatter overrides the default
    - Default prefix is the connection resource name (from ARM ID)
    - If effective_prefix == api_name, collapse to ``{api_name}_{operation_id}``
    - Truncated to 64 chars (prefix shrinks first to preserve operation clarity)
    """
    tools: list[FunctionTool] = []
    api_name = connection.api_name
    effective_prefix = (
        _sanitize_name(_to_snake_case(prefix))
        if prefix
        else _sanitize_name(_to_snake_case(connection.name))
    )

    for op in connection.operations:
        snake_op = _to_snake_case(op.operation_id)
        tool_name = (
            f"{api_name}_{snake_op}"
            if effective_prefix == api_name
            else f"{effective_prefix}_{api_name}_{snake_op}"
        )

        if len(tool_name) > 64:
            suffix = f"_{snake_op}" if effective_prefix == api_name else f"_{api_name}_{snake_op}"
            prefix_budget = 64 - len(suffix)
            tool_name = (
                f"{effective_prefix[:prefix_budget]}{suffix}"
                if prefix_budget > 0
                else tool_name[:64]
            )
            logger.warning("Tool name truncated to 64 chars: '%s'", tool_name)

        tool_name = tool_name[:64]

        properties: dict[str, JsonSchema] = {}
        required: list[str] = []
        all_params = op.parameters + op.body_properties

        for param in op.parameters:
            key = _sanitize_name(param.name)
            properties[key] = _param_to_json_schema(param)
            if param.required:
                required.append(key)

        for param in op.body_properties:
            key = _sanitize_name(param.name)
            properties[key] = _param_to_json_schema(param)
            if param.required or param.name in op.body_required_fields:
                required.append(key)

        parameters_schema: JsonSchema = {
            "type": "object",
            "properties": properties,
        }
        if required:
            parameters_schema["required"] = required

        desc_parts = [op.summary or op.operation_id]
        if op.description and op.description != op.summary:
            desc_parts.append(op.description)
        desc_parts.append(f"(via {connection.display_name})")
        if connection.status != "Connected":
            desc_parts.append(f"Connection status: {connection.status}")
        description = " — ".join(desc_parts)

        def make_handler(
            op: ParsedOperation = op,
            connection: ConnectionInfo = connection,
            all_params: list[ParsedParameter] = all_params,
        ) -> Callable[..., Awaitable[str]]:
            async def handler(**args: object) -> str:
                is_v2 = bool(data_plane_client and connection.connection_runtime_url)
                invoke_path = _build_invoke_path(
                    op,
                    args,
                    all_params,
                    url_encode=is_v2,
                )

                queries: ToolArgs = {}
                for param in op.parameters:
                    if param.location == "query":
                        key = _sanitize_name(param.name)
                        if key in args:
                            queries[param.name] = args[key]

                for param in op.internal_params:
                    if (
                        param.location == "query"
                        and param.default is not None
                        and param.name not in queries
                    ):
                        queries[param.name] = param.default

                body: ToolArgs = {}
                for param in op.body_properties:
                    key = _sanitize_name(param.name)
                    if key in args:
                        value: object = args[key]
                        if param.type in ("object", "array") and isinstance(value, str):
                            with suppress(json.JSONDecodeError, ValueError):
                                value = json.loads(value)
                        if "." in param.name:
                            _set_nested_value(body, param.name, value)
                        else:
                            body[param.name] = value

                for param in op.internal_params:
                    if (
                        param.location == "body"
                        and param.default is not None
                        and param.name not in body
                    ):
                        body[param.name] = param.default

                try:
                    if data_plane_client and connection.connection_runtime_url:
                        url = f"{connection.connection_runtime_url.rstrip('/')}{invoke_path}"
                        result = await data_plane_client.request(
                            op.method,
                            url,
                            params=queries or None,
                            body=body or None,
                        )
                        return json.dumps(result, indent=2, default=str)

                    request_payload: ToolArgs = {
                        "method": op.method,
                        "path": invoke_path,
                    }
                    if queries:
                        request_payload["queries"] = queries
                    if body:
                        request_payload["body"] = body

                    result = await arm.post(
                        f"{connection.resource_id}/dynamicInvoke",
                        body={"request": request_payload},
                    )
                    response = result.get("response", {})
                    if not isinstance(response, dict):
                        response = {}
                    response_body = response.get("body", result)
                    raw_status = response.get("statusCode", 200)
                    try:
                        status_code = int(raw_status)
                    except (ValueError, TypeError):
                        status_str = str(raw_status).lower()
                        status_code = {
                            "notfound": 404,
                            "badrequest": 400,
                            "unauthorized": 401,
                            "forbidden": 403,
                            "internalservererror": 500,
                            "created": 201,
                            "ok": 200,
                            "accepted": 200,
                            "nocontent": 200,
                        }.get(status_str, 500)

                    if status_code >= 400:
                        return f"Error ({status_code}): {json.dumps(response_body)}"

                    return json.dumps(response_body, indent=2, default=str)
                except Exception as exc:
                    error_type = type(exc).__name__
                    return f"Error invoking {op.operation_id}: {error_type}: {exc}"

            return handler

        tool: FunctionTool = FunctionTool(
            name=tool_name,
            description=description,
            func=make_handler(),
            input_model=parameters_schema,
        )
        tools.append(tool)

    return tools
