"""Tests for _source_marker utility."""

from pathlib import Path

from azure_functions_agents._source_marker import source_marker


def test_source_marker_returns_unknown_for_none() -> None:
    assert source_marker(None) == "<unknown>"


def test_source_marker_returns_filename_for_non_agents_path() -> None:
    assert source_marker("/path/to/main.agent.md") == "main.agent.md"
    assert source_marker("C:\\project\\src\\daily.agent.md") == "daily.agent.md"


def test_source_marker_includes_agents_prefix_for_agents_directory() -> None:
    assert source_marker("/path/to/agents/report.agent.md") == "agents_report.agent.md"
    assert source_marker("C:\\project\\agents\\main.agent.md") == "agents_main.agent.md"


def test_source_marker_nested_directories_not_supported() -> None:
    """Nested subdirectories under agents/ are not recognized as agents files."""
    # These should return just the filename since nested dirs aren't supported
    assert source_marker("/path/to/agents/daily/report.agent.md") == "report.agent.md"
    assert source_marker("/path/agents/sub1/sub2/file.agent.md") == "file.agent.md"
    assert source_marker("C:\\project\\agents\\weekly\\summary.agent.md") == "summary.agent.md"


def test_source_marker_case_insensitive_agents_folder() -> None:
    assert source_marker("/path/to/Agents/report.agent.md") == "Agents_report.agent.md"
    assert source_marker("/path/to/AGENTS/report.agent.md") == "AGENTS_report.agent.md"


def test_source_marker_handles_path_objects() -> None:
    path = Path("/path/to/agents/report.agent.md")
    assert source_marker(str(path)) == "agents_report.agent.md"
