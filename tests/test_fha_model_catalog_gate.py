from __future__ import annotations

import json
from pathlib import Path

import pytest

from azure_functions_agents.foundry_responses.fha_model_catalog_gate import (
    FhaModelCatalogError,
    compile_fha_v0_project,
    validate_fha_v0_catalog,
)
from azure_functions_agents.foundry_responses.fha_runtime_projection import (
    FHA_RUNTIME_PROJECTION_DIGEST_PREFIX,
    FhaProjectionCapabilities,
    FhaProjectionCatalogEntry,
    FhaProjectionMcpServer,
    FhaRuntimeProjection,
    FhaRuntimeProjectionError,
    compute_fha_runtime_projection_digest,
    parse_fha_runtime_projection,
    validate_fha_runtime_projection_match,
)

_PROJECT_ENDPOINT = "https://project.services.ai.azure.com/api/projects/demo"
_DEFAULT_MODEL = "gpt-fha-v0"


def _write_agent(root: Path, filename: str, frontmatter: str, body: str = "Assist.") -> None:
    (root / filename).write_text(
        f"---\n{frontmatter.strip()}\n---\n{body.strip()}\n",
        encoding="utf-8",
    )


def _write_safe_global_config(root: Path) -> None:
    (root / "agents.config.yaml").write_text(
        "system_tools:\n  web_request: false\n",
        encoding="utf-8",
    )


def _write_http_agent(root: Path, *, extra: str = "") -> None:
    _write_agent(
        root,
        "main.agent.md",
        f"""
name: Main
description: FHA V0 test agent.
trigger:
  type: http_trigger
  args:
    route: fha
{extra}
""",
    )


def _compile(root: Path, **kwargs: object):
    return compile_fha_v0_project(
        root,
        project_endpoint=_PROJECT_ENDPOINT,
        default_model=_DEFAULT_MODEL,
        **kwargs,
    )


def _safe_mcp() -> dict[str, object]:
    return {
        "servers": {
            "remote": {
                "type": "streamable-http",
                "url": "https://mcp.example.test/v1",
                "tools": ["search"],
                "auth": {"scope": "https://mcp.example.test/.default"},
                "headers": {
                    "Accept": "application/json",
                    "User-Agent": "fha-v0-tests",
                },
            }
        }
    }


def test_fha_v0_compiler_allows_tools_skills_remote_mcp_and_one_level_specialist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_MODEL", "ambient-model")
    monkeypatch.delenv(
        "AZURE_FUNCTIONS_AGENTS_SANDBOXENV_AZURE_FUNCTIONS_AGENTS_MODEL",
        raising=False,
    )
    _write_safe_global_config(tmp_path)
    _write_agent(
        tmp_path,
        "main.agent.md",
        """
name: Coordinator
description: Delegates to the specialist.
trigger:
  type: http_trigger
  args:
    route: coordinator
subagents:
  - agent: specialist
""",
    )
    _write_agent(
        tmp_path,
        "specialist.agent.md",
        """
name: Specialist
description: Internal-only specialist.
""",
    )
    tool = tmp_path / "tools" / "lookup.py"
    tool.parent.mkdir()
    tool.write_text(
        'def lookup(query: str) -> str:\n    """Look up a value."""\n    return query\n',
        encoding="utf-8",
    )
    skill = tmp_path / "skills" / "reader" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: reader\ndescription: Reads project context.\n---\nUse the context.\n",
        encoding="utf-8",
    )
    (tmp_path / "mcp.json").write_text(json.dumps(_safe_mcp()), encoding="utf-8")

    compilation = _compile(tmp_path)

    coordinator = compilation.catalog["main"]
    specialist = compilation.catalog["specialist"]
    assert [tool.name for tool in coordinator.capabilities.filtered_user_tools or []] == [
        "lookup"
    ]
    assert [tool.name for tool in coordinator.capabilities.filtered_mcp_tools or []] == ["remote"]
    assert [path.name for path in coordinator.capabilities.enabled_skill_paths] == ["reader"]
    assert specialist.resolved.trigger is None
    assert coordinator.resolved.model == _DEFAULT_MODEL
    assert specialist.resolved.model == _DEFAULT_MODEL
    assert compilation.projection.catalog[0].capabilities.subagents == ("specialist",)
    [mcp] = compilation.projection.mcp_servers
    assert mcp.name == "remote"
    assert mcp.allowed_tools == ("search",)
    assert mcp.auth_scope == "https://mcp.example.test/.default"
    assert mcp.headers == (
        ("Accept", "application/json"),
        ("User-Agent", "fha-v0-tests"),
    )
    validate_fha_v0_catalog(compilation.catalog)


@pytest.mark.parametrize(
    "frontmatter",
    [
        """
name: Queue
description: Queue-triggered FHA agent.
trigger:
  type: service_bus_queue_trigger
""",
        """
name: Chat
description: Built-in chat FHA agent.
builtin_endpoints:
  chat_api: true
""",
    ],
)
def test_fha_v0_allows_queue_and_builtin_chat_entrypoints(
    tmp_path: Path,
    frontmatter: str,
) -> None:
    _write_safe_global_config(tmp_path)
    _write_agent(tmp_path, "main.agent.md", frontmatter)

    compilation = _compile(tmp_path)

    assert tuple(compilation.catalog) == ("main",)


def test_fha_v0_compiler_refreshes_discovery_from_raw_authoring(tmp_path: Path) -> None:
    _write_safe_global_config(tmp_path)
    _write_http_agent(tmp_path)
    (tmp_path / "mcp.json").write_text(json.dumps(_safe_mcp()), encoding="utf-8")
    _compile(tmp_path)
    replacement = _safe_mcp()
    servers = replacement["servers"]
    assert isinstance(servers, dict)
    servers["replacement"] = servers.pop("remote")
    (tmp_path / "mcp.json").write_text(json.dumps(replacement), encoding="utf-8")

    compilation = _compile(tmp_path)

    assert [server.name for server in compilation.projection.mcp_servers] == ["replacement"]


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        (
            "main.agent.md",
            """---
name: Main
description: FHA V0 test agent.
trigger:
  type: http_trigger
  args:
    route: fha
---
Use $INSTRUCTION.
""",
        ),
        (
            "main.agent.md",
            """---
name: Main
description: $DESCRIPTION
trigger:
  type: http_trigger
  args:
    route: fha
---
Assist.
""",
        ),
        (
            "agents.config.yaml",
            "model: $MODEL\nsystem_tools:\n  web_request: false\n",
        ),
        (
            "mcp.json",
            '{"servers":{"remote":{"url":"https://mcp.example.test/$PATH"}}}',
        ),
        (
            "skills/reader/SKILL.md",
            "---\nname: reader\ndescription: $SKILL_DESCRIPTION\n---\nUse the skill.\n",
        ),
    ],
)
def test_fha_v0_rejects_raw_substitutions_before_loader_composition(
    tmp_path: Path,
    filename: str,
    content: str,
) -> None:
    _write_safe_global_config(tmp_path)
    _write_http_agent(tmp_path)
    source = tmp_path / filename
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(content, encoding="utf-8")

    with pytest.raises(FhaModelCatalogError, match="environment substitution"):
        _compile(tmp_path)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("default_web_request", "web_request"),
        ("dynamic_sessions", "Dynamic Sessions"),
        ("workflows", "workflows"),
        ("builtin_mcp", "built-in MCP"),
        ("unsupported_trigger", "unsupported"),
    ],
)
def test_fha_v0_rejects_excluded_runtime_surfaces(
    tmp_path: Path,
    kind: str,
    expected: str,
) -> None:
    if kind == "default_web_request":
        _write_http_agent(tmp_path)
    elif kind == "dynamic_sessions":
        (tmp_path / "agents.config.yaml").write_text(
            """
system_tools:
  web_request: false
  dynamic_sessions_code_interpreter:
    endpoint: https://sessions.example.test
""",
            encoding="utf-8",
        )
        _write_http_agent(tmp_path)
    elif kind == "workflows":
        _write_safe_global_config(tmp_path)
        _write_http_agent(tmp_path, extra="workflows:\n  enabled: false")
    elif kind == "builtin_mcp":
        _write_safe_global_config(tmp_path)
        _write_http_agent(tmp_path, extra="builtin_endpoints:\n  mcp: true")
    else:
        _write_safe_global_config(tmp_path)
        _write_agent(
            tmp_path,
            "main.agent.md",
            """
name: Main
description: FHA V0 test agent.
trigger:
  type: timer_trigger
  args:
    schedule: "0 0 * * * *"
""",
        )

    with pytest.raises(FhaModelCatalogError, match=expected):
        _compile(tmp_path)


def test_fha_v0_rejects_discovered_workflow_tools(tmp_path: Path) -> None:
    _write_safe_global_config(tmp_path)
    _write_http_agent(tmp_path)
    tool = tmp_path / "tools" / "workflow.py"
    tool.parent.mkdir()
    tool.write_text(
        """
from azure_functions_agents import workflow_tool


@workflow_tool
def send_report() -> None:
    return None
""",
        encoding="utf-8",
    )

    with pytest.raises(FhaModelCatalogError, match="discovered workflow tools"):
        _compile(tmp_path)


@pytest.mark.parametrize(
    "server",
    [
        {"type": "stdio", "command": "python"},
        {"type": "sse", "url": "https://mcp.example.test"},
        {"url": "https://mcp.example.test", "headers": {"Authorization": "Bearer value"}},
        {"url": "https://mcp.example.test", "headers": {"Cookie": "id=value"}},
        {"url": "https://mcp.example.test", "headers": {"X-Trace": "value"}},
        {"url": "https://mcp.example.test", "headers": {"Accept": "secret-value"}},
        {"url": "https://mcp.example.test?api-key=value"},
        {"url": "https://mcp.example.test?key=value"},
        {"url": "https://mcp.example.test", "auth": {"scope": "scope", "token": "value"}},
        {"url": "https://mcp.example.test", "unexpected": True},
    ],
)
def test_fha_v0_rejects_unsafe_mcp_config(tmp_path: Path, server: dict[str, object]) -> None:
    _write_safe_global_config(tmp_path)
    _write_http_agent(tmp_path)
    (tmp_path / "mcp.json").write_text(
        json.dumps({"servers": {"remote": server}}),
        encoding="utf-8",
    )

    with pytest.raises(FhaModelCatalogError):
        _compile(tmp_path)


@pytest.mark.parametrize(
    ("agents", "expected"),
    [
        (
            {
                "main.agent.md": """
name: Main
description: Main.
trigger:
  type: http_trigger
  args:
    route: main
subagents:
  - agent: specialist
""",
                "specialist.agent.md": """
name: Specialist
description: Specialist.
subagents:
  - agent: leaf
""",
                "leaf.agent.md": """
name: Leaf
description: Leaf.
""",
            },
            "nested delegation",
        ),
        (
            {
                "main.agent.md": """
name: Main
description: Main.
trigger:
  type: http_trigger
  args:
    route: main
subagents:
  - agent: specialist
""",
                "specialist.agent.md": """
name: Specialist
description: Specialist.
subagents:
  - agent: main
""",
            },
            "cyclic",
        ),
    ],
)
def test_fha_v0_rejects_nested_and_cyclic_delegation(
    tmp_path: Path,
    agents: dict[str, str],
    expected: str,
) -> None:
    _write_safe_global_config(tmp_path)
    for filename, frontmatter in agents.items():
        _write_agent(tmp_path, filename, frontmatter)

    with pytest.raises(FhaModelCatalogError, match=expected):
        _compile(tmp_path)


def test_fha_v0_rejects_mcp_failed_load_evidence(tmp_path: Path) -> None:
    _write_safe_global_config(tmp_path)
    _write_http_agent(tmp_path)
    compilation = _compile(tmp_path)
    compilation.composition.mcp_result.failed_loads.append(("remote", "failed"))

    with pytest.raises(FhaModelCatalogError, match="failed-load evidence"):
        validate_fha_v0_catalog(
            compilation.catalog,
            composition=compilation.composition,
            mcp_servers=(),
        )


def _projection(default_model: str = _DEFAULT_MODEL) -> FhaRuntimeProjection:
    return FhaRuntimeProjection.create(
        project_endpoint="https://PROJECT.services.ai.azure.com/api/projects/demo/",
        default_model=default_model,
        catalog=(
            FhaProjectionCatalogEntry.create(
                slug="main",
                model=default_model,
                trigger="http_trigger",
                builtin_endpoints=("chat_api",),
                capabilities=FhaProjectionCapabilities.create(
                    user_tools=("lookup",),
                    skills=("reader",),
                    mcp=("remote",),
                    subagents=("specialist",),
                ),
            ),
        ),
        mcp_servers=(
            FhaProjectionMcpServer.create(
                name="remote",
                url="https://MCP.example.test/v1",
                allowed_tools=("search",),
                auth_scope="https://mcp.example.test/.default",
                headers={"User-Agent": "fha-v0-tests"},
            ),
        ),
    )


def test_fha_runtime_projection_is_canonical_and_digest_stable() -> None:
    projection = _projection()

    serialized = projection.serialize()
    parsed = parse_fha_runtime_projection(serialized)

    assert parsed == projection
    assert serialized == parse_fha_runtime_projection(serialized.encode("utf-8")).serialize()
    assert " " not in serialized
    assert projection.project_endpoint == _PROJECT_ENDPOINT
    assert projection.mcp_servers[0].url == "https://mcp.example.test/v1"
    assert projection.digest == compute_fha_runtime_projection_digest(projection)
    assert projection.digest.startswith(FHA_RUNTIME_PROJECTION_DIGEST_PREFIX)


def test_fha_runtime_projection_rejects_noncanonical_or_mismatched_inputs() -> None:
    projection = _projection()
    pretty = json.dumps(json.loads(projection.serialize()), indent=2)

    with pytest.raises(FhaRuntimeProjectionError):
        parse_fha_runtime_projection(pretty)
    with pytest.raises(FhaRuntimeProjectionError, match="does not match"):
        validate_fha_runtime_projection_match(projection, _projection("other-model"))


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Authorization": "Bearer value"}, "allowlisted"),
        ({"X-Custom": "value"}, "allowlisted"),
        ({"Accept": "$TOKEN"}, "unsafe"),
        ({"Accept": "secret-value"}, "unsafe"),
        ({"Accept": "key=value"}, "unsafe"),
        ({"Accept": "application/json\r\nunsafe"}, "invalid"),
    ],
)
def test_fha_runtime_projection_rejects_unsafe_static_headers(
    headers: dict[str, str],
    expected: str,
) -> None:
    with pytest.raises(FhaRuntimeProjectionError, match=expected):
        FhaProjectionMcpServer.create(
            name="remote",
            url="https://mcp.example.test",
            allowed_tools=("search",),
            headers=headers,
        )
