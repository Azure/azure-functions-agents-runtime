"""A discovered tool with no file, network, or retry side effects."""

from azure_functions_agents import tool


@tool
def correct_report_format(value: str) -> str:
    """Normalize a report format: known aliases markdwon/markdown and txt/text, or case.

    Return the accepted format, or an empty string when no safe correction is known.
    This tool does not generate reports or retry a Function.
    """
    normalized = value.lower()
    aliases = {"markdwon": "markdown", "txt": "text"}
    return normalized if normalized in ("text", "markdown") else aliases.get(normalized, "")