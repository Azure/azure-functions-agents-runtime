from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import azure.functions as func
import pytest
from azurefunctions.extensions.http.fastapi import JSONResponse, Request
from pydantic import BaseModel

from azure_functions_agents import HostedSkillFunctionApp
from azure_functions_agents._tool_descriptor import ToolDescriptor


class InvoiceInput(BaseModel):
    invoice_id: str
    recipient: str


def _write_agent(tmp_path: Path, filename: str) -> None:
    body = textwrap.dedent(
        f"""
        ---
        name: {filename.split(".")[0]}
        description: Desc
        ---
        Assist the user.
        """
    ).lstrip()
    (tmp_path / filename).write_text(body, encoding="utf-8")


@pytest.fixture
def app(tmp_path: Path) -> Any:
    _write_agent(tmp_path, "billing.agent.md")
    _write_agent(tmp_path, "support.agent.md")
    return HostedSkillFunctionApp(tmp_path)


def _tools_for(app: Any, slug: str) -> dict[str, ToolDescriptor]:
    capabilities = app._hosted_skill_catalog[slug].capabilities
    return {tool.name: tool for tool in capabilities.execution_user_tools() or ()}


@pytest.mark.asyncio
async def test_service_bus_tool_runs_handler_with_built_message(app: Any) -> None:
    seen: list[Any] = []

    @app.service_bus_queue_trigger(arg_name="message", queue_name="q", connection="SB")
    @app.tool(name="process_invoice", schema=InvoiceInput)
    def process_invoice(message: func.ServiceBusMessage) -> None:
        """Process an invoice and email its receipt."""
        seen.append(json.loads(message.get_body()))

    tool = _tools_for(app, "billing")["process_invoice"]
    assert tool.description == "Process an invoice and email its receipt."
    assert set(tool.parameters()["properties"]) == {"invoice_id", "recipient"}

    result = await tool.invoke(arguments={"invoice_id": "INV-1", "recipient": "a@b.c"})

    assert result == "completed"
    assert seen == [{"invoice_id": "INV-1", "recipient": "a@b.c"}]
    assert [f.get_function_name() for f in app.get_functions()] == ["process_invoice"]


@pytest.mark.asyncio
async def test_http_tool_declared_above_route_serializes_response(app: Any) -> None:
    class Query(BaseModel):
        order_id: str

    @app.tool(schema=Query)
    @app.route(route="orders")
    async def get_order(req: Request) -> JSONResponse:
        """Look up an order."""
        order_id = (await req.json())["order_id"]
        return JSONResponse({"order": order_id}, status_code=200)

    result = await _tools_for(app, "billing")["get_order"].invoke(arguments={"order_id": "7"})

    assert result == {"status_code": 200, "body": '{"order":"7"}'}
    assert [f.get_function_name() for f in app.get_functions()] == ["get_order"]


@pytest.mark.asyncio
async def test_standalone_typed_tool_derives_schema(app: Any) -> None:
    @app.tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    tool = _tools_for(app, "support")["add"]

    assert tool.parameters()["required"] == ["a", "b"]
    assert await tool.invoke(arguments={"a": 1, "b": 2}) == 3


def test_agents_scopes_tool_visibility(app: Any) -> None:
    @app.tool(agents=["billing"])
    def only_billing() -> str:
        """Billing only."""
        return "ok"

    @app.tool
    def everyone() -> str:
        """Everyone."""
        return "ok"

    assert set(_tools_for(app, "billing")) == {"only_billing", "everyone"}
    assert set(_tools_for(app, "support")) == {"everyone"}


def test_unknown_agent_slug_fails_fast(app: Any) -> None:
    with pytest.raises(ValueError, match="Unknown agent slug"):

        @app.tool(agents=["nope"])
        def fn() -> str:
            """Doc."""
            return "x"


def test_trigger_backed_tool_requires_schema(app: Any) -> None:
    with pytest.raises(TypeError, match="schema="):

        @app.tool
        def fn(message: func.ServiceBusMessage) -> None:
            """Doc."""


def test_tool_requires_description(app: Any) -> None:
    with pytest.raises(ValueError, match="description"):

        @app.tool
        def fn() -> str:
            return "x"


def test_duplicate_tool_name_rejected(app: Any) -> None:
    @app.tool(name="dup")
    def first() -> str:
        """First."""
        return "x"

    with pytest.raises(ValueError, match=r"Duplicate app\.tool"):

        @app.tool(name="dup")
        def second() -> str:
            """Second."""
            return "y"
