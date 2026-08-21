from __future__ import annotations

from pathlib import Path

from azure_functions_agents.project_composition import compose_project


def _write_agent(root: Path, name: str, frontmatter: str, body: str) -> None:
    (root / name).write_text(
        f"---\n{frontmatter.strip()}\n---\n{body.strip()}\n",
        encoding="utf-8",
    )


def test_compose_project_preserves_normal_environment_substitution(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PROJECT_COMPOSITION_MODEL", "gpt-normal")
    monkeypatch.setenv("PROJECT_COMPOSITION_INSTRUCTION", "customer context")
    _write_agent(
        tmp_path,
        "main.agent.md",
        """
name: Normal composition
description: Uses ordinary substitutions.
model: $PROJECT_COMPOSITION_MODEL
trigger:
  type: http_trigger
  args:
    route: normal
""",
        "Use $PROJECT_COMPOSITION_INSTRUCTION.",
    )

    composition = compose_project(tmp_path)

    [resolved] = composition.resolved_agents
    assert resolved.model == "gpt-normal"
    assert resolved.instructions == "Use customer context."
    assert tuple(composition.catalog) == ("main",)
    assert composition.catalog["main"].resolved is resolved


def test_compose_project_keeps_normal_non_strict_agent_discovery(tmp_path: Path) -> None:
    (tmp_path / "broken.agent.md").write_text("---\nname: [\n---\n", encoding="utf-8")
    _write_agent(
        tmp_path,
        "healthy.agent.md",
        """
name: Healthy
description: A valid agent.
trigger:
  type: timer_trigger
  args:
    schedule: "0 0 * * * *"
""",
        "Run normally.",
    )

    composition = compose_project(tmp_path)

    assert tuple(composition.catalog) == ("healthy",)
