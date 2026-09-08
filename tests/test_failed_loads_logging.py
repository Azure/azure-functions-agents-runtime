"""Test failed loads are included in indexing summary."""

import json
import logging
from pathlib import Path

import pytest


def test_indexing_log_includes_failed_loads(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Test that failed tool/skill/MCP loads are logged in the indexing summary."""
    from azure_functions_agents.app import create_function_app
    
    # Create a broken tool
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "broken.py").write_text("def broken(", encoding="utf-8")
    
    # Create a broken skill
    skills_dir = tmp_path / "skills" / "bad-skill"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text("---\nname: [unclosed\n---\n", encoding="utf-8")
    
    # Create a broken MCP config
    (tmp_path / "mcp.json").write_text(
        json.dumps({
            "servers": {
                "broken": {
                    "type": "http"
                    # Missing required 'url' field
                }
            }
        }),
        encoding="utf-8"
    )
    
    # Create a working agent so the app doesn't fail
    (tmp_path / "main.agent.md").write_text(
        """
---
name: Test
description: Test agent
builtin_endpoints:
    debug_chat_ui: true
---
Test
        """,
        encoding="utf-8"
    )
    
    with caplog.at_level(logging.INFO):
        create_function_app(tmp_path)
    
    # Find the indexing log
    indexing_logs = [r for r in caplog.records if "agent_runtime_indexed" in r.message]
    assert len(indexing_logs) == 1
    
    # Parse JSON
    log_message = indexing_logs[0].message
    json_start = log_message.index("{")
    log_json = json.loads(log_message[json_start:])
    
    # Verify failed_loads section exists
    assert "failed_loads" in log_json
    assert "user_tools" in log_json["failed_loads"]
    assert "skills" in log_json["failed_loads"]
    assert "mcp_servers" in log_json["failed_loads"]
    
    # Verify failures are logged
    assert len(log_json["failed_loads"]["user_tools"]) == 1
    assert "broken.py" in log_json["failed_loads"]["user_tools"][0]
    
    assert len(log_json["failed_loads"]["skills"]) == 1
    assert "bad-skill" in log_json["failed_loads"]["skills"][0]
    
    assert len(log_json["failed_loads"]["mcp_servers"]) == 1
    assert "broken" in log_json["failed_loads"]["mcp_servers"][0]
    assert "missing 'url'" in log_json["failed_loads"]["mcp_servers"][0]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
