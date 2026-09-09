"""Structural tests for the private durable agent-loop sample application."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import yaml

from azure_functions_agents.config.loader import load_agent_specs, load_global_config

SAMPLE_SRC = (
    Path(__file__).resolve().parents[1] / "samples" / "durable-agent-loop-spike" / "src"
)
SAMPLE_ROOT = SAMPLE_SRC.parent
TOOLS_DIR = SAMPLE_SRC / "sandbox_bundle" / "tools"

CUSTOM_TOOLS = {
    "adaptive_probe",
    "chain_probe",
    "customer_probe",
    "delayed_probe",
    "prepare_demo_workspace",
    "read_demo_workspace",
    "unsafe_write_probe",
}
GENERIC_TOOLS = {"read_file", "run_shell", "search_files", "write_file"}
REMOTE_TOOLS = {
    "microsoft_code_sample_search",
    "microsoft_docs_fetch",
    "microsoft_docs_search",
}


def _frontmatter() -> tuple[dict[str, object], str]:
    source = (SAMPLE_SRC / "main.agent.md").read_text(encoding="utf-8")
    opening, raw_metadata, instructions = source.split("---", maxsplit=2)
    assert opening == ""
    metadata = yaml.safe_load(raw_metadata)
    assert isinstance(metadata, dict)
    return metadata, instructions.strip()


def _module_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _public_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and not node.name.startswith("_")
    ]


def test_sample_application_config_is_explicit_and_private() -> None:
    assert (SAMPLE_SRC / "function_app.py").read_text(encoding="utf-8") == (
        "from azure_functions_agents import create_function_app\n\n"
        "app = create_function_app()\n"
    )

    metadata, instructions = _frontmatter()
    assert metadata == {
        "name": "Durable Agent Loop Qualification",
        "description": (
            "Exercises the private durable model/tool loop under deterministic "
            "qualification protocols."
        ),
        "builtin_endpoints": {
            "debug_chat_ui": False,
            "chat_api": True,
            "mcp": False,
            "http_auth": "function",
        },
    }
    [spec] = load_agent_specs(SAMPLE_SRC, strict=True)
    assert spec.builtin_endpoints.chat_api is True
    assert spec.builtin_endpoints.debug_chat_ui is False
    assert spec.builtin_endpoints.mcp is False
    assert spec.builtin_endpoints.http_auth.mode == "function"
    assert spec.trigger is None
    assert spec.workflows is None

    normalized_instructions = " ".join(instructions.split())
    required_instruction_terms = (
        "run only in ACA Sandbox",
        "remain worker-side",
        "only when the user explicitly requests",
        "Never claim that a tool ran unless you called it",
        "Never expose credentials",
        "one adaptive probe call per model step",
        "step-5 terminal evidence marker",
        "one chain probe call per model step",
        "step-16 terminal marker",
        "Emit it as the only tool call",
        "Never mix it with another call",
        "generic `write_file`",
        "`prepare_demo_workspace` exactly once",
        "`read_demo_workspace` exactly once",
        "`microsoft_docs_search` exactly once",
    )
    for term in required_instruction_terms:
        assert term in normalized_instructions


def test_global_config_has_only_supported_background_settings() -> None:
    raw = yaml.safe_load((SAMPLE_SRC / "agents.config.yaml").read_text(encoding="utf-8"))
    assert raw == {
        "model": "$AZURE_FUNCTIONS_AGENTS_APIM_MODEL",
        "timeout": 1800,
        "system_tools": {"web_request": False},
    }
    resolved = load_global_config(SAMPLE_SRC)
    assert resolved.timeout == 1800
    assert resolved.system_tools.web_request is False
    assert resolved.system_tools.dynamic_sessions_code_interpreter is None
    assert resolved.session_runtime is None

    metadata, _ = _frontmatter()
    forbidden = {
        "dynamic_sessions_code_interpreter",
        "session_runtime",
        "workflows",
    }
    assert forbidden.isdisjoint(raw)
    assert forbidden.isdisjoint(metadata)


def test_retained_demo_pins_resume_readiness_budget() -> None:
    local_settings = json.loads(
        (SAMPLE_SRC / "local.settings.template.json").read_text(encoding="utf-8")
    )["Values"]
    setting = "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_HYBRID_READY_TIMEOUT_SECONDS"

    assert local_settings[setting] == "120"
    assert f"{setting}: '120'" in (
        SAMPLE_ROOT / "infra" / "modules" / "function-app.bicep"
    ).read_text(encoding="utf-8")
    assert f"| `{setting}` | `120` |" in (SAMPLE_ROOT / "README.md").read_text(
        encoding="utf-8"
    )


def test_microsoft_learn_mcp_is_remote_http_only() -> None:
    document = json.loads((SAMPLE_SRC / "mcp.json").read_text(encoding="utf-8"))
    assert document == {
        "servers": {
            "microsoft-learn-through-apim": {
                "type": "http",
                "url": "$AZURE_FUNCTIONS_AGENTS_APIM_MCP_URL",
                "headers": {
                    "api-key": "$AZURE_FUNCTIONS_AGENTS_APIM_SUBSCRIPTION_KEY"
                },
                "tools": [
                    "microsoft_docs_search",
                    "microsoft_code_sample_search",
                    "microsoft_docs_fetch",
                ],
            }
        }
    }
    server = document["servers"]["microsoft-learn-through-apim"]
    assert "command" not in server
    assert server["type"] not in {"local", "stdio"}
    assert set(server["tools"]) == REMOTE_TOOLS


def test_tool_policy_is_complete_strict_and_fail_closed() -> None:
    policy = json.loads(
        (SAMPLE_SRC / "durable-loop-tools.json").read_text(encoding="utf-8")
    )
    assert set(policy) == {"schema_version", "tools"}
    assert policy["schema_version"] == "1"

    tools = policy["tools"]
    assert set(tools) == CUSTOM_TOOLS | GENERIC_TOOLS | REMOTE_TOOLS
    assert "request_human_input" not in tools
    for name, descriptor in tools.items():
        assert set(descriptor) == {"provenance", "behavior", "parallel_safe"}
        if name in REMOTE_TOOLS:
            assert descriptor == {
                "provenance": "remote",
                "behavior": "read_only",
                "parallel_safe": True,
            }
        else:
            assert descriptor["provenance"] == "local"
            assert descriptor["parallel_safe"] is False

    assert tools["read_file"]["behavior"] == "read_only"
    assert tools["search_files"]["behavior"] == "read_only"
    assert tools["write_file"]["behavior"] == "idempotent_write"
    assert tools["prepare_demo_workspace"]["behavior"] == "idempotent_write"
    assert tools["read_demo_workspace"]["behavior"] == "read_only"
    assert tools["run_shell"]["behavior"] == "mutating"
    assert tools["unsafe_write_probe"]["behavior"] == "mutating"
    for name in CUSTOM_TOOLS - {"prepare_demo_workspace", "unsafe_write_probe"}:
        assert tools[name]["behavior"] == "read_only"


def test_bundle_tools_are_guarded_and_one_per_module() -> None:
    modules = sorted(TOOLS_DIR.glob("*.py"))
    assert {path.stem for path in modules} == CUSTOM_TOOLS
    for path in modules:
        source = path.read_text(encoding="utf-8")
        tree = _module_tree(path)
        public_functions = _public_functions(tree)
        assert [function.name for function in public_functions] == [path.stem]
        assert (
            'if os.environ.get("AZURE_FUNCTIONS_AGENTS_SANDBOX") != "1":'
            in source
        )
        assert "raise RuntimeError" in source


def test_probe_sources_pin_bounded_deterministic_protocols() -> None:
    adaptive = (TOOLS_DIR / "adaptive_probe.py").read_text(encoding="utf-8")
    assert "not isinstance(step, int)" in adaptive
    assert "0 <= step <= 5" in adaptive
    assert '"direction_changed": direction_changed' in adaptive
    assert "direction_changed = step == 2" in adaptive
    assert '"next_action"' in adaptive
    assert '"adaptive-probe-terminal-5"' in adaptive

    chain = (TOOLS_DIR / "chain_probe.py").read_text(encoding="utf-8")
    assert "not isinstance(step, int)" in chain
    assert "0 <= step <= 16" in chain
    assert '"next_action"' in chain
    assert '"chain-probe-terminal-16"' in chain

    delayed = (TOOLS_DIR / "delayed_probe.py").read_text(encoding="utf-8")
    assert "0 <= seconds <= 30" in delayed
    assert "time.sleep(seconds)" in delayed
    assert "time.monotonic()" in delayed
    assert "time.time(" not in delayed

    unsafe = (TOOLS_DIR / "unsafe_write_probe.py").read_text(encoding="utf-8")
    assert "^[A-Za-z0-9._-]{1,64}$" in unsafe
    assert '"write_count": write_count' in unsafe
    assert "temporary.replace(_SIDE_EFFECT_PATH)" in unsafe
    assert '"marker": marker' not in unsafe
    assert "http" not in unsafe.lower()

    customer = (TOOLS_DIR / "customer_probe.py").read_text(encoding="utf-8")
    assert "1 <= len(message) <= 256" in customer
    assert "1 <= repeat <= 20" in customer
    assert '"sandbox_marker": True' in customer
    assert '"process_id": os.getpid()' in customer

    prepare = (TOOLS_DIR / "prepare_demo_workspace.py").read_text(
        encoding="utf-8"
    )
    assert '"demo-context.txt"' in prepare
    assert '"www.example.com"' in prepare
    assert '"durable-demo-tool-process"' in prepare
    assert "time.sleep(180)" in prepare
    assert "start_new_session=True" in prepare
    assert "1 <= len(content) <= 512" in prepare

    read = (TOOLS_DIR / "read_demo_workspace.py").read_text(
        encoding="utf-8"
    )
    assert '"demo-context.txt"' in read
    assert '"content": content' in read


def test_bundle_contains_no_deployment_or_credential_material() -> None:
    forbidden_fragments = (
        "api-key",
        "authorization",
        "azurewebjobsstorage",
        "bearer ",
        "client_secret",
        "password",
        "subscription_key",
        "/subscriptions/",
        "https://",
        "azure_functions_agents_apim",
    )
    source_files = sorted(
        path
        for path in TOOLS_DIR.parent.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )
    assert source_files == sorted(TOOLS_DIR / f"{name}.py" for name in CUSTOM_TOOLS)
    for path in source_files:
        source = path.read_text(encoding="utf-8").lower()
        for fragment in forbidden_fragments:
            assert fragment not in source
