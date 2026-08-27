import path from 'node:path'

export const AZURE_REST_DEPENDENCIES = ['aiohttp', 'azure-identity', 'jmespath']

export function normalizeDistributionName(name) {
  return String(name ?? '').trim().toLowerCase().replace(/[-_.]+/g, '-')
}

function requirementName(line) {
  const trimmed = line.trim()
  if (!trimmed || trimmed.startsWith('#') || trimmed.startsWith('-')) return ''
  const match = /^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\s*\[[^\]]*\])?(?:\s*@|\s*(?:===|==|~=|!=|<=|>=|<|>)|\s*;|\s*$)/.exec(trimmed)
  return match ? normalizeDistributionName(match[1]) : ''
}

export function mergeRequirements(content, dependencies = AZURE_REST_DEPENDENCIES) {
  const current = String(content ?? '')
  const lines = current.split(/\r\n|\n|\r/)
  const existing = new Set(lines.map(requirementName).filter(Boolean))
  const added = dependencies.filter((dependency) => !existing.has(normalizeDistributionName(dependency)))
  if (!added.length) return { content: current, added }

  const crlfCount = (current.match(/\r\n/g) ?? []).length
  const lfCount = (current.replace(/\r\n/g, '').match(/\n/g) ?? []).length
  const newline = crlfCount > 0 && lfCount === 0 ? '\r\n' : '\n'
  const separator = current.length > 0 && !/\r?\n$/.test(current) ? newline : ''
  return { content: `${current}${separator}${added.join(newline)}${newline}`, added }
}

export function customToolSlug(name) {
  const slug = String(name ?? '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 64)
  if (!slug || !/^[a-z_][a-z0-9_]*$/.test(slug)) throw new Error('Tool name must form a valid Python identifier.')
  return slug
}

export function customToolPath(name) {
  return path.posix.join('tools', `${customToolSlug(name)}.py`)
}

export function azureRoleScope(subscription, scopeType, resourceGroup) {
    const subscriptionId = String(subscription ?? '').trim()
    if (!/^[0-9a-f-]{36}$/i.test(subscriptionId)) throw new Error('A valid subscription ID is required.')
    if (scopeType === 'subscription') return `/subscriptions/${subscriptionId}`
    const group = String(resourceGroup ?? '').trim()
    if (scopeType !== 'resourceGroup' || !/^[A-Za-z0-9._()-]{1,90}$/.test(group)) {
        throw new Error('A valid resource group is required for resource-group scope.')
    }
    return `/subscriptions/${subscriptionId}/resourceGroups/${group}`
}

export function resolveConfiguredModelSettings(settings) {
    const value = (name) => String(settings?.[name] ?? '').trim()
    let provider = value('AZURE_FUNCTIONS_AGENTS_PROVIDER').toLowerCase()
    if (provider && !['openai', 'azure_openai', 'foundry'].includes(provider)) {
        throw Object.assign(new Error(`Unsupported configured provider: ${provider}.`), {
            status: 422,
            portalCode: 'configured_model_invalid',
        })
    }
    if (!provider) {
        provider = value('AZURE_OPENAI_ENDPOINT')
            ? 'azure_openai'
            : value('FOUNDRY_PROJECT_ENDPOINT')
                ? 'foundry'
                : value('OPENAI_API_KEY')
                    ? 'openai'
                    : ''
    }
    if (!provider) {
        throw Object.assign(new Error('This Function App has no configured model provider.'), {
            status: 409,
            portalCode: 'configured_model_missing',
        })
    }
    const runtimeModel = value('AZURE_FUNCTIONS_AGENTS_MODEL')
    const model = provider === 'azure_openai'
        ? value('AZURE_OPENAI_DEPLOYMENT') || runtimeModel || 'gpt-4o-mini'
        : provider === 'foundry'
            ? value('FOUNDRY_MODEL') || runtimeModel || 'gpt-4o-mini'
            : runtimeModel || 'gpt-4o-mini'
    const endpoint = provider === 'azure_openai'
        ? value('AZURE_OPENAI_ENDPOINT')
        : provider === 'foundry'
            ? value('FOUNDRY_PROJECT_ENDPOINT')
            : 'https://api.openai.com/v1/'
    const apiKey = provider === 'openai' ? value('OPENAI_API_KEY') : value('AZURE_OPENAI_API_KEY')
    if (!endpoint || (provider === 'openai' && !apiKey)) {
        throw Object.assign(new Error(`The ${provider} provider configuration is incomplete.`), {
            status: 422,
            portalCode: 'configured_model_invalid',
        })
    }
    return { provider, model, endpoint, apiKey }
}

export function renderAzureRestTool(name = 'azure_rest') {
  const functionName = customToolSlug(name)
  return `"""Make authenticated requests to the Azure Resource Manager REST API."""

import json
from typing import Literal
from urllib.parse import parse_qs, urlsplit

import aiohttp
import jmespath
from azure.identity.aio import DefaultAzureCredential
from azure_functions_agents import tool
from pydantic import BaseModel, Field

_credential: DefaultAzureCredential | None = None
_session: aiohttp.ClientSession | None = None
_ARM_ORIGIN = "https://management.azure.com"


class AzureRestParams(BaseModel):
    path: str = Field(
        description=(
            "ARM REST API path relative to https://management.azure.com. "
            "Must start with / and include an api-version query parameter."
        )
    )
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = Field(
        default="GET",
        description="HTTP method. Defaults to GET.",
    )
    body: str | None = Field(
        default=None,
        description="Optional JSON request body for POST, PUT, or PATCH requests.",
    )
    query: str | None = Field(
        default=None,
        description="Optional JMESPath expression used to filter the response.",
    )


def _error(message: str, **details: object) -> str:
    return json.dumps({"error": message, **details})


@tool(schema=AzureRestParams)
async def ${functionName}(params: AzureRestParams) -> str:
    """Make an authenticated request to the Azure Resource Manager REST API."""
    global _credential, _session

    request_path = params.path.strip()
    parsed = urlsplit(request_path)
    if not request_path.startswith("/") or request_path.startswith("//") or parsed.scheme or parsed.netloc:
        return _error("path must be relative to management.azure.com and start with one /")
    api_versions = [value for key, values in parse_qs(parsed.query, keep_blank_values=True).items() if key.lower() == "api-version" for value in values]
    if not any(value.strip() for value in api_versions):
        return _error("path must include a non-empty api-version query parameter")

    request_body = None
    if params.body:
        try:
            request_body = json.loads(params.body)
        except json.JSONDecodeError:
            return _error("body must be valid JSON")

    if _credential is None:
        _credential = DefaultAzureCredential()
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()

    token = await _credential.get_token("https://management.azure.com/.default")
    headers = {"Authorization": f"Bearer {token.token}", "Content-Type": "application/json"}
    async with _session.request(params.method, f"{_ARM_ORIGIN}{request_path}", headers=headers, json=request_body) as response:
        response_text = await response.text()
        try:
            data = json.loads(response_text)
        except json.JSONDecodeError:
            data = {"raw": response_text}

        if response.status >= 400:
            return _error(f"HTTP {response.status}", body=data if len(response_text) <= 8192 else response_text[:8192])

        if params.query:
            try:
                data = jmespath.search(params.query, data)
            except Exception as error:
                return _error(f"JMESPath query failed: {error}")

        return json.dumps(data)
`
}

export function validateAzureRestSource(source, name) {
    const content = String(source ?? '')
    const functionName = customToolSlug(name)
    if (!content.trim() || content.length > 200_000) throw new Error('Python source must be between 1 and 200,000 characters.')
    const required = [
        'from azure_functions_agents import tool',
        'DefaultAzureCredential',
        'https://management.azure.com/.default',
        '@tool(schema=AzureRestParams)',
        `async def ${functionName}(`,
    ]
    for (const marker of required) {
        if (!content.includes(marker)) throw new Error(`Python source is missing required Azure REST marker: ${marker}`)
    }
    if (/\b(?:eval|exec|compile|__import__)\s*\(/.test(content) || /\bsubprocess\b/.test(content)) {
        throw new Error('Python source contains execution APIs that are not allowed in the Azure REST recipe.')
    }
    if (/\b(?:password|secret|api_key|access_key|connection_string)\s*=\s*["'][^"']{8,}/i.test(content)) {
        throw new Error('Python source appears to contain a hardcoded secret.')
    }
    return content
}