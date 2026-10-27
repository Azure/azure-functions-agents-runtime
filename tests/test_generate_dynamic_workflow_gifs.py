from __future__ import annotations

from pathlib import Path


def test_dynamic_workflow_gif_uses_canonical_runtime_tool_label() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "eng"
        / "scripts"
        / "generate_dynamic_workflow_gifs.py"
    )
    content = script.read_text(encoding="utf-8")

    assert 'WORKFLOW_START_TOOL_LABEL: Final = "fhs_start_workflow(plan)"' in content
    assert '"start_workflow(plan)"' not in content
