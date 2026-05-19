from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .arm import ArmClient, DataPlaneClient

JsonObject = dict[str, Any]


@dataclass
class ParsedParameter:
    name: str
    location: str  # "path", "query", "header", "body"
    type: str
    required: bool
    description: str
    format: str | None = None
    enum: list[str] | None = None
    default: object = None


@dataclass
class ParsedOperation:
    operation_id: str
    method: str
    path: str
    summary: str
    description: str
    parameters: list[ParsedParameter] = field(default_factory=list)
    body_properties: list[ParsedParameter] = field(default_factory=list)
    body_required_fields: list[str] = field(default_factory=list)
    internal_params: list[ParsedParameter] = field(default_factory=list)


@dataclass
class ConnectionInfo:
    resource_id: str
    name: str
    api_name: str
    display_name: str
    status: str
    location: str
    operations: list[ParsedOperation] = field(default_factory=list)
    connection_runtime_url: str | None = None


def is_v2_connection(connection_id: str) -> bool:
    """Return True if the connection ID is a V2 (gateway) connection."""
    lower = connection_id.lower()
    return "/aigateways/" in lower or "/connectorgateways/" in lower


def _get_object(source: JsonObject, key: str) -> JsonObject:
    value = source.get(key)
    if isinstance(value, dict):
        return value
    return {}


def _get_string(source: JsonObject, key: str, default: str = "") -> str:
    value = source.get(key, default)
    return value if isinstance(value, str) else default


def _get_string_list(source: JsonObject, key: str) -> list[str]:
    value = source.get(key, [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _resolve_ref(ref: str, root: JsonObject) -> JsonObject:
    """Resolve a $ref pointer like '#/definitions/Foo' against the swagger root."""
    parts = ref.lstrip("#/").split("/")
    result = root
    for part in parts:
        next_value = result.get(part, {})
        if not isinstance(next_value, dict):
            return {}
        result = next_value
    return result


def _resolve_schema(schema: JsonObject, swagger: JsonObject) -> JsonObject:
    """Resolve a schema, following $ref if present."""
    ref = schema.get("$ref")
    if isinstance(ref, str):
        return _resolve_ref(ref, swagger)
    return schema


def _extract_body_properties(
    body_schema: JsonObject,
    swagger: JsonObject,
    max_depth: int = 2,
    depth: int = 0,
) -> tuple[list[ParsedParameter], list[str], list[ParsedParameter]]:
    """Flatten body schema properties into a list of ParsedParameters."""
    resolved = _resolve_schema(body_schema, swagger)
    properties = _get_object(resolved, "properties")
    required_fields = _get_string_list(resolved, "required")
    params: list[ParsedParameter] = []
    internal_params: list[ParsedParameter] = []

    for prop_name, prop_schema_value in properties.items():
        if not isinstance(prop_name, str) or not isinstance(prop_schema_value, dict):
            continue

        prop_resolved = _resolve_schema(prop_schema_value, swagger)
        visibility = _get_string(prop_resolved, "x-ms-visibility")
        if visibility == "internal":
            if prop_resolved.get("default") is not None:
                internal_params.append(
                    ParsedParameter(
                        name=prop_name,
                        location="body",
                        type=_get_string(prop_resolved, "type", "string"),
                        required=False,
                        description="",
                        default=prop_resolved.get("default"),
                    )
                )
            continue

        prop_type = _get_string(prop_resolved, "type", "string")

        if prop_type == "object" and depth < max_depth:
            nested_props = _get_object(prop_resolved, "properties")
            nested_required = _get_string_list(prop_resolved, "required")
            if nested_props:
                for nested_name, nested_schema_value in nested_props.items():
                    if not isinstance(nested_name, str) or not isinstance(
                        nested_schema_value,
                        dict,
                    ):
                        continue
                    nested_resolved = _resolve_schema(nested_schema_value, swagger)
                    nested_visibility = _get_string(nested_resolved, "x-ms-visibility")
                    if nested_visibility == "internal":
                        continue
                    nested_type = _get_string(nested_resolved, "type", "string")
                    if nested_type in ("object", "array") and depth + 1 >= max_depth:
                        nested_type = "string"
                    flat_name = f"{prop_name}.{nested_name}"
                    params.append(
                        ParsedParameter(
                            name=flat_name,
                            location="body",
                            type=nested_type,
                            required=nested_name in nested_required,
                            description=_get_string(
                                nested_resolved,
                                "description",
                                _get_string(
                                    nested_resolved,
                                    "x-ms-summary",
                                    _get_string(nested_resolved, "title"),
                                ),
                            ),
                            format=_get_string(nested_resolved, "format") or None,
                            enum=_get_string_list(nested_resolved, "enum") or None,
                            default=nested_resolved.get("default"),
                        )
                    )
                    if nested_name in nested_required:
                        required_fields.append(flat_name)
                continue

        if prop_type in ("object", "array") and depth >= max_depth:
            prop_type = "string"

        params.append(
            ParsedParameter(
                name=prop_name,
                location="body",
                type=prop_type,
                required=prop_name in required_fields,
                description=_get_string(
                    prop_resolved,
                    "description",
                    _get_string(
                        prop_resolved,
                        "x-ms-summary",
                        _get_string(prop_resolved, "title"),
                    ),
                ),
                format=_get_string(prop_resolved, "format") or None,
                enum=_get_string_list(prop_resolved, "enum") or None,
                default=prop_resolved.get("default"),
            )
        )

    return params, required_fields, internal_params


async def _resolve_dynamic_schema(
    arm: ArmClient,
    resource_id: str,
    swagger: JsonObject,
    dynamic_schema: JsonObject,
    op: JsonObject,
    *,
    data_plane_client: DataPlaneClient | None = None,
    connection_runtime_url: str | None = None,
) -> JsonObject | None:
    """Resolve an x-ms-dynamic-schema by calling the referenced operation."""
    del op
    op_id = _get_string(dynamic_schema, "operationId")
    if not op_id:
        return None

    schema_path: str | None = None
    schema_method: str | None = None
    paths = _get_object(swagger, "paths")
    for path, methods_value in paths.items():
        if not isinstance(path, str) or not isinstance(methods_value, dict):
            continue
        for method, operation_value in methods_value.items():
            if not isinstance(method, str) or not isinstance(operation_value, dict):
                continue
            if _get_string(operation_value, "operationId") == op_id:
                schema_path = path
                schema_method = method
                break
        if schema_path:
            break

    if not schema_path or not schema_method:
        return None

    invoke_path = re.sub(r"^/\{connectionId\}", "", schema_path, flags=re.IGNORECASE)

    params = _get_object(dynamic_schema, "parameters")
    for param_name, param_value in params.items():
        if not isinstance(param_name, str):
            continue
        if isinstance(param_value, dict) and "parameter" in param_value:
            ref_param = param_value.get("parameter")
            defaults = {
                "poster": "User",
                "location": "Channel",
                "recipientType": "Channel",
            }
            param_value = defaults.get(ref_param, "") if isinstance(ref_param, str) else ""
        invoke_path = invoke_path.replace(f"{{{param_name}}}", str(param_value))

    try:
        if data_plane_client and connection_runtime_url:
            url = f"{connection_runtime_url.rstrip('/')}{invoke_path}"
            result = await data_plane_client.request(schema_method.upper(), url)
            value_path = _get_string(dynamic_schema, "value-path", "schema")
            value = result.get(value_path, result)
            return value if isinstance(value, dict) else result

        result = await arm.post(
            f"{resource_id}/dynamicInvoke",
            body={
                "request": {
                    "method": schema_method.upper(),
                    "path": invoke_path,
                }
            },
        )
        response = _get_object(result, "response")
        body = _get_object(response, "body")
        value_path = _get_string(dynamic_schema, "value-path", "schema")
        value = body.get(value_path, body)
        return value if isinstance(value, dict) else body
    except Exception:
        return None


async def _parse_operations(
    swagger: JsonObject,
    arm: ArmClient,
    resource_id: str,
    *,
    data_plane_client: DataPlaneClient | None = None,
    connection_runtime_url: str | None = None,
) -> list[ParsedOperation]:
    """Parse Swagger paths into a list of ParsedOperations."""
    paths = _get_object(swagger, "paths")
    operations: list[ParsedOperation] = []
    seen_families: dict[str, tuple[ParsedOperation, int]] = {}

    for path, methods_value in paths.items():
        if not isinstance(path, str) or "$subscriptions" in path:
            continue
        if not isinstance(methods_value, dict):
            continue

        for method, operation_value in methods_value.items():
            if method in ("parameters", "x-ms-notification-content"):
                continue
            if not isinstance(method, str) or not isinstance(operation_value, dict):
                continue

            if operation_value.get("x-ms-trigger") or operation_value.get("deprecated"):
                continue
            if method.lower() == "delete":
                continue

            visibility = _get_string(operation_value, "x-ms-visibility")
            if visibility == "internal":
                continue

            operation_id = _get_string(operation_value, "operationId", f"{method}_{path}")
            if operation_id.startswith("mcp_") or operation_id == "HttpRequest":
                continue

            params: list[ParsedParameter] = []
            internal_params: list[ParsedParameter] = []
            body_props: list[ParsedParameter] = []
            body_required: list[str] = []

            parameters_value = operation_value.get("parameters", [])
            if isinstance(parameters_value, list):
                for param_value in parameters_value:
                    if not isinstance(param_value, dict):
                        continue

                    ref = param_value.get("$ref")
                    if isinstance(ref, str):
                        param_value = _resolve_ref(ref, swagger)

                    param_in = _get_string(param_value, "in")
                    if param_in == "body":
                        schema = _get_object(param_value, "schema")
                        resolved_schema = _resolve_schema(schema, swagger)
                        dynamic_value = resolved_schema.get("x-ms-dynamic-schema")
                        dynamic = dynamic_value if isinstance(dynamic_value, dict) else None
                        if dynamic and not _get_object(resolved_schema, "properties"):
                            dyn_schema = await _resolve_dynamic_schema(
                                arm,
                                resource_id,
                                swagger,
                                dynamic,
                                operation_value,
                                data_plane_client=data_plane_client,
                                connection_runtime_url=connection_runtime_url,
                            )
                            source_schema = (
                                {
                                    "properties": _get_object(dyn_schema, "properties"),
                                    "required": _get_string_list(dyn_schema, "required"),
                                }
                                if dyn_schema
                                else schema
                            )
                            (
                                body_props,
                                body_required,
                                body_internal,
                            ) = _extract_body_properties(source_schema, swagger)
                        else:
                            (
                                body_props,
                                body_required,
                                body_internal,
                            ) = _extract_body_properties(schema, swagger)
                        internal_params.extend(body_internal)
                        continue

                    if _get_string(param_value, "name") == "connectionId":
                        continue

                    param_visibility = _get_string(param_value, "x-ms-visibility")
                    if param_visibility == "internal":
                        if param_value.get("default") is not None:
                            internal_params.append(
                                ParsedParameter(
                                    name=_get_string(param_value, "name"),
                                    location=param_in,
                                    type=_get_string(param_value, "type", "string"),
                                    required=False,
                                    description="",
                                    default=param_value.get("default"),
                                )
                            )
                        continue

                    params.append(
                        ParsedParameter(
                            name=_get_string(param_value, "name"),
                            location=param_in,
                            type=_get_string(param_value, "type", "string"),
                            required=bool(param_value.get("required", False)),
                            description=_get_string(
                                param_value,
                                "description",
                                _get_string(param_value, "x-ms-summary"),
                            ),
                            format=_get_string(param_value, "format") or None,
                            enum=_get_string_list(param_value, "enum") or None,
                            default=param_value.get("default"),
                        )
                    )

            parsed = ParsedOperation(
                operation_id=operation_id,
                method=method.upper(),
                path=path,
                summary=_get_string(operation_value, "summary"),
                description=_get_string(operation_value, "description"),
                parameters=params,
                body_properties=body_props,
                body_required_fields=body_required,
                internal_params=internal_params,
            )

            annotation = _get_object(operation_value, "x-ms-api-annotation")
            family = _get_string(annotation, "family") or None
            revision_value = annotation.get("revision", 0)
            try:
                new_revision = int(revision_value)
            except (TypeError, ValueError):
                new_revision = 0

            if family:
                existing = seen_families.get(family)
                if existing is None:
                    seen_families[family] = (parsed, new_revision)
                    operations.append(parsed)
                else:
                    existing_op, existing_revision = existing
                    if new_revision > existing_revision:
                        operations.remove(existing_op)
                        seen_families[family] = (parsed, new_revision)
                        operations.append(parsed)
            else:
                operations.append(parsed)

    return operations


def _parse_resource_id(resource_id: str) -> dict[str, str]:
    """Extract subscription, resource group, and name from a V1 connection resource ID."""
    pattern = (
        r"/subscriptions/(?P<subscription>[^/]+)"
        r"/resourceGroups/(?P<resource_group>[^/]+)"
        r"/providers/Microsoft\.Web/connections/(?P<name>[^/]+)"
    )
    match = re.search(pattern, resource_id, re.IGNORECASE)
    if not match:
        raise ValueError(f"Invalid V1 connection resource ID: {resource_id}")
    return {key: value for key, value in match.groupdict().items() if value is not None}


def _parse_v2_resource_id(resource_id: str) -> dict[str, str]:
    """Extract subscription, resource group, gateway type, gateway, and name from a V2 connection resource ID."""
    pattern = (
        r"/subscriptions/(?P<subscription>[^/]+)"
        r"/resourceGroups/(?P<resource_group>[^/]+)"
        r"/providers/Microsoft\.Web/(?P<gateway_type>aigateways|connectorGateways)/(?P<gateway>[^/]+)"
        r"/connections/(?P<name>[^/]+)"
    )
    match = re.search(pattern, resource_id, re.IGNORECASE)
    if not match:
        raise ValueError(f"Invalid V2 connection resource ID: {resource_id}")
    return {key: value for key, value in match.groupdict().items() if value is not None}


_V2_API_VERSIONS = {
    "aigateways": "2026-03-01-preview",
    "connectorgateways": "2026-05-01-preview",
}


async def load_connection(
    arm: ArmClient,
    resource_id: str,
    *,
    data_plane_client: DataPlaneClient | None = None,
) -> ConnectionInfo:
    """Fetch connection metadata and Swagger spec, return a ConnectionInfo with parsed operations.

    Automatically detects V1 vs V2 connections based on the resource ID format.
    """
    if is_v2_connection(resource_id):
        return await _load_v2_connection(
            arm,
            resource_id,
            data_plane_client=data_plane_client,
        )
    return await _load_v1_connection(arm, resource_id)


async def _load_v1_connection(arm: ArmClient, resource_id: str) -> ConnectionInfo:
    """Load a V1 connection (Microsoft.Web/connections)."""
    conn_data = await arm.get(resource_id)
    props = _get_object(conn_data, "properties")
    api_data = _get_object(props, "api")
    statuses_value = props.get("statuses")
    statuses = statuses_value if isinstance(statuses_value, list) else [{}]
    first_status = statuses[0] if statuses and isinstance(statuses[0], dict) else {}

    api_name = _get_string(api_data, "name")
    display_name = _get_string(props, "displayName")
    status = _get_string(
        props,
        "overallStatus",
        _get_string(first_status, "status", "Unknown"),
    )
    location = _get_string(conn_data, "location")

    parts = _parse_resource_id(resource_id)
    swagger_path = (
        f"/subscriptions/{parts['subscription']}"
        f"/providers/Microsoft.Web/locations/{location}"
        f"/managedApis/{api_name}"
    )
    api_response = await arm.get(swagger_path, params={"export": "true"})
    swagger = _get_object(_get_object(api_response, "properties"), "swagger")
    if not _get_object(swagger, "paths"):
        swagger = api_response

    operations = await _parse_operations(swagger, arm, resource_id)

    return ConnectionInfo(
        resource_id=resource_id,
        name=parts["name"],
        api_name=api_name,
        display_name=display_name,
        status=status,
        location=location,
        operations=operations,
    )


async def _load_v2_connection(
    arm: ArmClient,
    resource_id: str,
    *,
    data_plane_client: DataPlaneClient | None = None,
) -> ConnectionInfo:
    """Load a V2 connection (Microsoft.Web/aigateways or connectorGateways)."""
    parts = _parse_v2_resource_id(resource_id)
    gateway_type = parts["gateway_type"]
    api_version = _V2_API_VERSIONS.get(gateway_type.lower(), "2026-05-01-preview")

    conn_data = await arm.get(resource_id, api_version=api_version)
    props = _get_object(conn_data, "properties")
    api_name = _get_string(props, "connectorName")
    display_name = _get_string(props, "displayName")
    status = _get_string(props, "overallStatus", "Unknown")
    connection_runtime_url = _get_string(props, "connectionRuntimeUrl")

    gateway_path = (
        f"/subscriptions/{parts['subscription']}"
        f"/resourceGroups/{parts['resource_group']}"
        f"/providers/Microsoft.Web/{gateway_type}/{parts['gateway']}"
    )
    gateway_data = await arm.get(gateway_path, api_version=api_version)
    location = _get_string(gateway_data, "location")

    swagger_path = (
        f"/subscriptions/{parts['subscription']}"
        f"/providers/Microsoft.Web/locations/{location}"
        f"/managedApis/{api_name}"
    )
    api_response = await arm.get(swagger_path, params={"export": "true"})
    swagger = _get_object(_get_object(api_response, "properties"), "swagger")
    if not _get_object(swagger, "paths"):
        swagger = api_response

    operations = await _parse_operations(
        swagger,
        arm,
        resource_id,
        data_plane_client=data_plane_client,
        connection_runtime_url=connection_runtime_url,
    )

    return ConnectionInfo(
        resource_id=resource_id,
        name=parts["name"],
        api_name=api_name,
        display_name=display_name,
        status=status,
        location=location,
        operations=operations,
        connection_runtime_url=connection_runtime_url,
    )
