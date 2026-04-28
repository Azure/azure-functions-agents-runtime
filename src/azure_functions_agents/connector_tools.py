from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote

from copilot.tools import Tool, ToolInvocation, ToolResult

from .arm import ArmClient, DataPlaneClient
from .connectors import ConnectionInfo, ParsedOperation, ParsedParameter


def _sanitize_name(name: str) -> str:
    """Sanitize parameter name to match ^[a-zA-Z0-9_.-]{1,64}$."""
    sanitized = re.sub(r"[^a-zA-Z0-9_.\-]", "_", name)
    return sanitized[:64]


def _to_snake_case(name: str) -> str:
    """Convert operationId to snake_case."""
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = re.sub(r"[^a-zA-Z0-9]", "_", s)
    s = re.sub(r"_+", "_", s)
    return s.strip("_").lower()


def _param_to_json_schema(param: ParsedParameter) -> dict:
    """Convert a ParsedParameter to a JSON Schema property."""
    type_map = {"integer": "integer", "number": "number", "boolean": "boolean"}
    schema: dict = {"type": type_map.get(param.type, "string")}
    if param.description:
        schema["description"] = param.description
    if param.enum:
        schema["enum"] = param.enum
    if param.default is not None:
        schema["default"] = param.default
    return schema


def _build_invoke_path(op: ParsedOperation, args: dict, all_params: list[ParsedParameter], url_encode: bool = True) -> str:
    """Build the invoke path by stripping /{connectionId} and substituting path params.

    When url_encode is False (V1 dynamicInvoke), path param values are inserted
    as-is since the path is a JSON field, not a real URL.  When True (V2 data
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
    # Substitute internal path params with their defaults
    for param in op.internal_params:
        if param.location == "path" and param.default is not None:
            replacement = quote(str(param.default), safe="") if url_encode else str(param.default)
            path = path.replace(f"{{{param.name}}}", replacement)
    return path


def generate_tools(
    arm: ArmClient, connection: ConnectionInfo,
    prefix: str | None = None,
    data_plane_client: DataPlaneClient | None = None,
) -> list[Tool]:
    """Generate Copilot SDK Tool objects for each operation in a connection.

    Tool names are ``{effective_prefix}_{api_name}_{operation_id}`` where:
    - ``prefix`` from frontmatter overrides the default
    - Default prefix is the connection resource name (from ARM ID)
    - If effective_prefix == api_name, collapse to ``{api_name}_{operation_id}``
    - Truncated to 64 chars (prefix shrinks first to preserve operation clarity)
    """
    tools = []
    api_name = connection.api_name

    # Determine effective prefix
    if prefix:
        effective_prefix = _sanitize_name(_to_snake_case(prefix))
    else:
        effective_prefix = _sanitize_name(_to_snake_case(connection.name))

    for op in connection.operations:
        snake_op = _to_snake_case(op.operation_id)

        # Build tool name: collapse prefix when it matches api_name
        if effective_prefix == api_name:
            tool_name = f"{api_name}_{snake_op}"
        else:
            tool_name = f"{effective_prefix}_{api_name}_{snake_op}"

        # Smart truncation: shrink prefix first to preserve operation name
        if len(tool_name) > 64:
            suffix = f"_{api_name}_{snake_op}" if effective_prefix != api_name else f"_{snake_op}"
            prefix_budget = 64 - len(suffix)
            if prefix_budget > 0:
                tool_name = f"{effective_prefix[:prefix_budget]}{suffix}"
            else:
                tool_name = tool_name[:64]
            logging.warning(f"Tool name truncated to 64 chars: '{tool_name}'")

        tool_name = tool_name[:64]

        # Build JSON schema for parameters
        properties: dict = {}
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

        parameters_schema: dict = {"type": "object", "properties": properties}
        if required:
            parameters_schema["required"] = required

        # Build description
        desc_parts = [op.summary or op.operation_id]
        if op.description and op.description != op.summary:
            desc_parts.append(op.description)
        desc_parts.append(f"(via {connection.display_name})")
        if connection.status != "Connected":
            desc_parts.append(f"Connection status: {connection.status}")
        description = " — ".join(desc_parts)

        def make_handler(op=op, connection=connection, all_params=all_params):
            async def handler(invocation: ToolInvocation) -> ToolResult:
                args = invocation.arguments or {}

                # V2 uses direct HTTP URLs (need encoding); V1 uses JSON path field (no encoding)
                is_v2 = bool(data_plane_client and connection.connection_runtime_url)
                invoke_path = _build_invoke_path(op, args, all_params, url_encode=is_v2)

                queries = {}
                for param in op.parameters:
                    if param.location == "query":
                        key = _sanitize_name(param.name)
                        if key in args:
                            queries[param.name] = args[key]

                # Inject internal query params with defaults
                for param in op.internal_params:
                    if param.location == "query" and param.default is not None:
                        if param.name not in queries:
                            queries[param.name] = param.default

                body = {}
                for param in op.body_properties:
                    key = _sanitize_name(param.name)
                    if key in args:
                        value = args[key]
                        if param.type in ("object", "array") and isinstance(value, str):
                            try:
                                value = json.loads(value)
                            except (json.JSONDecodeError, ValueError):
                                pass
                        # Handle dot-separated names as nested objects
                        if "." in param.name:
                            parts = param.name.split(".", 1)
                            if parts[0] not in body:
                                body[parts[0]] = {}
                            body[parts[0]][parts[1]] = value
                        else:
                            body[param.name] = value

                # Inject internal body params with defaults
                for param in op.internal_params:
                    if param.location == "body" and param.default is not None:
                        if param.name not in body:
                            body[param.name] = param.default

                try:
                    if data_plane_client and connection.connection_runtime_url:
                        # V2: direct HTTP to data plane
                        url = f"{connection.connection_runtime_url.rstrip('/')}{invoke_path}"
                        result = await data_plane_client.request(
                            op.method,
                            url,
                            params=queries or None,
                            body=body or None,
                        )
                        return ToolResult(
                            text_result_for_llm=json.dumps(result, indent=2, default=str),
                            result_type="success",
                        )
                    else:
                        # V1: dynamicInvoke via ARM
                        request_body: dict = {
                            "request": {
                                "method": op.method,
                                "path": invoke_path,
                            }
                        }
                        if queries:
                            request_body["request"]["queries"] = queries
                        if body:
                            request_body["request"]["body"] = body

                        result = await arm.post(
                            f"{connection.resource_id}/dynamicInvoke",
                            body=request_body,
                        )
                        response = result.get("response", {})
                        response_body = response.get("body", result)
                        raw_status = response.get("statusCode", 200)
                        try:
                            status_code = int(raw_status)
                        except (ValueError, TypeError):
                            # statusCode can be a string like "NotFound", "Created", etc.
                            status_str = str(raw_status).lower()
                            if status_str in ("notfound",):
                                status_code = 404
                            elif status_str in ("badrequest",):
                                status_code = 400
                            elif status_str in ("unauthorized",):
                                status_code = 401
                            elif status_str in ("forbidden",):
                                status_code = 403
                            elif status_str in ("internalservererror",):
                                status_code = 500
                            elif status_str in ("created",):
                                status_code = 201
                            elif status_str in ("ok", "accepted", "nocontent"):
                                status_code = 200
                            else:
                                status_code = 500  # unknown status, treat as error

                        if status_code >= 400:
                            return ToolResult(
                                text_result_for_llm=f"Error ({status_code}): {json.dumps(response_body)}",
                                result_type="error",
                            )

                        return ToolResult(
                            text_result_for_llm=json.dumps(response_body, indent=2, default=str),
                            result_type="success",
                        )
                except Exception as e:
                    error_type = type(e).__name__
                    return ToolResult(
                        text_result_for_llm=f"Error invoking {op.operation_id}: {error_type}: {e}",
                        result_type="error",
                    )

            return handler

        tools.append(Tool(
            name=tool_name,
            description=description,
            parameters=parameters_schema,
            handler=make_handler(),
        ))

    return tools
