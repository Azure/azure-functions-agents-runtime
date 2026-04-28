"""Make authenticated requests to the Azure Resource Manager REST API.

Uses the function app's managed identity for authentication.
The path should be relative to https://management.azure.com.
Include api-version as a query parameter in the path.

Example paths:
  /subscriptions/{subscriptionId}/resources?api-version=2021-04-01
  /subscriptions/{subscriptionId}/resourcegroups?api-version=2021-04-01
"""

import json
from typing import Optional

import aiohttp
import jmespath
from azure.identity.aio import DefaultAzureCredential
from pydantic import BaseModel, Field

_credential = None
_session = None


class AzureRestParams(BaseModel):
    path: str = Field(
        description="ARM REST API path relative to https://management.azure.com. Must include api-version query parameter. Example: /subscriptions/{id}/resources?api-version=2021-04-01"
    )
    method: str = Field(
        default="GET",
        description="HTTP method (GET, POST, PUT, PATCH, DELETE). Defaults to GET.",
    )
    body: Optional[str] = Field(
        default=None,
        description="JSON request body for POST/PUT/PATCH requests.",
    )
    query: Optional[str] = Field(
        default=None,
        description="JMESPath query to filter the response (like az cli --query). Example: value[].{name: name, type: type, location: location}",
    )


async def azure_rest(params: AzureRestParams) -> str:
    """Make an authenticated request to the Azure Resource Manager REST API."""
    global _credential, _session

    if _credential is None:
        _credential = DefaultAzureCredential()
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()

    token = await _credential.get_token("https://management.azure.com/.default")

    url = f"https://management.azure.com{params.path}"
    headers = {
        "Authorization": f"Bearer {token.token}",
        "Content-Type": "application/json",
    }

    request_body = None
    if params.body:
        try:
            request_body = json.loads(params.body)
        except json.JSONDecodeError:
            return json.dumps({"error": "Invalid JSON in request body"})

    method = params.method.upper()
    async with _session.request(method, url, headers=headers, json=request_body) as resp:
        try:
            data = await resp.json()
        except Exception:
            data = {"raw": await resp.text()}

        if resp.status >= 400:
            return json.dumps({"error": f"HTTP {resp.status}", "body": data})

        if params.query:
            try:
                data = jmespath.search(params.query, data)
            except Exception as e:
                return json.dumps({"error": f"JMESPath query failed: {e}", "raw_keys": list(data.keys()) if isinstance(data, dict) else type(data).__name__})

        return json.dumps(data)
