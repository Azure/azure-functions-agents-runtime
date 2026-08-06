---
name: authoring-custom-tools
description: How to write a custom Python tool for an azure-functions-agents-runtime agent — a file in tools/ with a single Pydantic-model parameter, auto-discovered by the runtime and callable by the agent.
---

# Authoring custom tools

A custom tool is **real Python code** (unlike triggers, which are declarative).
Tools live in the app's `tools/` directory, are **auto-discovered** by the
runtime, and are offered to the agent's model as callable functions.

## The contract

A tool is a module-level function in `tools/*.py` that takes **exactly one**
parameter typed as a Pydantic `BaseModel` subclass. That alone is enough to be
discovered — no decorator required.

```python
from pydantic import BaseModel, Field


class GetWeatherParams(BaseModel):
    city: str = Field(description="City name, e.g. 'Seattle'.")
    units: str = Field(default="metric", description="'metric' or 'imperial'.")


async def get_weather(params: GetWeatherParams) -> str:
    """Return the current weather for a city as a short summary."""
    ...
    return "..."
```

- **Function name** = the tool name the model calls (`get_weather`).
- **Docstring** = the tool description the model reads. Make it clear and specific.
- Each `Field(description=...)` becomes part of the JSON schema the model sees —
  describe every field.
- The function may be **sync or async** and returns a `str` (often JSON) or a `dict`.

### Optional explicit decorator

Use `@tool` to override the name / description or supply a separate schema:

```python
from azure_functions_agents import tool
from pydantic import BaseModel, Field


class Params(BaseModel):
    query: str = Field(description="Search text.")


@tool(name="search_docs", description="Search the docs index.")
async def search(params: Params) -> str:
    ...
```

## Calling Azure / HTTP

Authenticate with the app's **managed identity** — never hardcode secrets:

```python
from azure.identity.aio import DefaultAzureCredential

_credential = None


async def _token(scope: str) -> str:
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential()
    return (await _credential.get_token(scope)).token
```

Use `aiohttp` or `httpx` for HTTP. Create clients lazily as module-level
singletons and reuse them. Keep each tool a single, self-contained file.

## Rules and pitfalls

- Exactly **one** parameter, and it must be a `BaseModel` subclass — otherwise the
  function is skipped by discovery.
- Do **not** use Azure Functions decorators (`@app.route`, etc.) in `tools/` —
  those are for triggers, and triggers are declarative here anyway.
- Read configuration from environment / app settings (`os.environ`), not literals.
- Return machine-readable output (a JSON string or a dict) so the agent can chain
  the result into the next step.

## Tool vs MCP vs trigger

- **Custom tool** = Python in `tools/` (this skill).
- **MCP server** = remote tools declared in `mcp.json` (configuration, no code).
- **Trigger** = declarative `.agent.md` front matter (see the `authoring-triggers`
  skill).
