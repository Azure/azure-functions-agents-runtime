from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import azure.functions as func

from azure_functions_agents.config.schema import DebugConfig, ResolvedAgent, ToolsFilter
from azure_functions_agents.registration.capabilities import AgentCapabilities
from azure_functions_agents.registration.endpoints import register_debug_endpoints


class FakeFunctionApp:
    def __init__(self) -> None:
        self.routes: list[dict[str, Any]] = []

    def route(self, **kwargs: Any) -> Any:
        def decorator(handler: Any) -> Any:
            self.routes.append({"handler": handler, **kwargs})
            return handler

        return decorator

    def function_name(self, *, name: str) -> Any:
        def decorator(handler: Any) -> Any:
            for route in self.routes:
                if route["handler"] is handler:
                    route["function_name"] = name
                    break
            return handler

        return decorator


def _resolved_agent(
    *,
    name: str,
    is_main: bool,
    debug: DebugConfig,
    source_file: str | Path | None = None,
) -> ResolvedAgent:
    source = source_file or Path(__file__).resolve()
    return ResolvedAgent(
        name=name,
        description="desc",
        trigger=None,
        instructions="Assist the user.",
        is_main=is_main,
        debug=debug,
        model=None,
        timeout=1.0,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        sandbox_config=None,
        connector_specs=[],
        input_schema=None,
        response_schema=None,
        response_example=None,
        metadata={},
        source_file=str(source) if source is not None else None,
    )


def _response_text(response: func.HttpResponse) -> str:
    body = response.body
    return body.decode("utf-8") if isinstance(body, bytes) else str(body)


def test_register_debug_endpoints_serves_agent_aware_chat_ui_for_non_main_agent(
    tmp_path: Path,
) -> None:
    app = FakeFunctionApp()
    source_file = tmp_path / "secondary_agent.agent.md"
    source_file.write_text("---\nname: Secondary Agent\n---\n", encoding="utf-8")
    resolved = _resolved_agent(
        name="Secondary Agent",
        is_main=False,
        debug=DebugConfig(chat=True),
        source_file=source_file,
    )

    register_debug_endpoints(app, resolved, AgentCapabilities())

    [chat_page_route] = app.routes
    assert chat_page_route["route"] == "agents/secondary_agent/"
    assert chat_page_route["methods"] == ["GET"]
    assert chat_page_route["auth_level"] == func.AuthLevel.ANONYMOUS

    response = chat_page_route["handler"](SimpleNamespace(path_params={}))

    assert response.status_code == 200
    html = _response_text(response)
    assert 'path.match(/^(\\/agents\\/[^/]+)$/)' in html
    assert 'return "/agent";' in html


def test_register_debug_endpoints_uses_filename_slug_for_duplicate_display_names(
    tmp_path: Path,
) -> None:
    app = FakeFunctionApp()
    source_a = tmp_path / "daily_report_a.agent.md"
    source_b = tmp_path / "daily_report_b.agent.md"
    source_a.write_text("---\nname: Daily Report\n---\n", encoding="utf-8")
    source_b.write_text("---\nname: Daily Report\n---\n", encoding="utf-8")

    register_debug_endpoints(
        app,
        _resolved_agent(
            name="Daily Report",
            is_main=False,
            debug=DebugConfig(chat=True),
            source_file=source_a,
        ),
        AgentCapabilities(),
    )
    register_debug_endpoints(
        app,
        _resolved_agent(
            name="Daily Report",
            is_main=False,
            debug=DebugConfig(chat=True),
            source_file=source_b,
        ),
        AgentCapabilities(),
    )

    assert [route["route"] for route in app.routes] == [
        "agents/daily_report_a/",
        "agents/daily_report_b/",
    ]
