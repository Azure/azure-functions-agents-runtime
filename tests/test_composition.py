from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from azure_functions_agents.composition import (
    compose_binding_target,
    load_project_snapshot,
)


def _write_agent(root: Path, filename: str, metadata: str, body: str = "Instructions") -> Path:
    source = root / filename
    source.write_text(
        f"---\n{textwrap.dedent(metadata).strip()}\n---\n{textwrap.dedent(body).strip()}\n",
        encoding="utf-8",
    )
    return source


def test_binding_snapshot_ignores_non_binding_frontmatter(tmp_path: Path) -> None:
    source = _write_agent(
        tmp_path,
        "order-fulfillment.agent.md",
        """
        name: Order Processor
        description: Processes orders
        trigger: definitely-not-a-valid-trigger
        builtin_endpoints: [invalid, shape]
        model: 42
        tools: invalid
        workflows: invalid
        subagents: invalid
        """,
    )

    snapshot = load_project_snapshot(tmp_path)
    entry = compose_binding_target(snapshot, "order-fulfillment")

    assert entry.definition.source_file == source.resolve()
    assert entry.definition.name == "Order Processor"
    assert entry.definition.description == "Processes orders"
    assert entry.definition.instructions.strip() == "Instructions"
    assert entry.definition.slug == "order_fulfillment"


def test_binding_target_accepts_normalized_slug(tmp_path: Path) -> None:
    _write_agent(
        tmp_path,
        "order-fulfillment.agent.md",
        "name: Order Processor\ndescription: Processes orders",
    )

    snapshot = load_project_snapshot(tmp_path)

    assert compose_binding_target(snapshot, "order_fulfillment").definition.name == (
        "Order Processor"
    )


@pytest.mark.parametrize("field", ["name", "description"])
def test_binding_definition_requires_minimal_string_fields(
    field: str,
    tmp_path: Path,
) -> None:
    metadata = "description: Present" if field == "name" else "name: Present"
    source = _write_agent(tmp_path, "missing.agent.md", metadata)
    snapshot = load_project_snapshot(tmp_path)

    with pytest.raises(ValueError, match=rf"{field}.*non-empty string"):
        compose_binding_target(snapshot, "missing")

    assert source.exists()


def test_binding_snapshot_rejects_duplicate_normalized_slugs(tmp_path: Path) -> None:
    metadata = "name: Agent\ndescription: Agent description"
    _write_agent(tmp_path, "order-fulfillment.agent.md", metadata)
    _write_agent(tmp_path, "order_fulfillment.agent.md", metadata)

    with pytest.raises(ValueError, match=r"Duplicate agent slug 'order_fulfillment'"):
        load_project_snapshot(tmp_path)


def test_binding_lookup_diagnostic_lists_available_identities(tmp_path: Path) -> None:
    _write_agent(
        tmp_path,
        "order-fulfillment.agent.md",
        "name: Display Name\ndescription: Processes orders",
    )
    snapshot = load_project_snapshot(tmp_path)

    with pytest.raises(ValueError, match=r"order-fulfillment \(order_fulfillment\)"):
        compose_binding_target(snapshot, "Display Name")


def test_binding_snapshot_rejects_invalid_yaml_with_source_path(tmp_path: Path) -> None:
    source = tmp_path / "broken.agent.md"
    source.write_text(
        "---\nname: Broken\ndescription: [unterminated\n---\nInstructions\n",
        encoding="utf-8",
    )

    snapshot = load_project_snapshot(tmp_path)

    with pytest.raises(ValueError, match=r"broken\.agent\.md.*invalid YAML"):
        compose_binding_target(snapshot, "broken")


def test_binding_target_ignores_unrelated_invalid_definition(tmp_path: Path) -> None:
    _write_agent(
        tmp_path,
        "selected.agent.md",
        "name: Selected\ndescription: Selected agent",
    )
    (tmp_path / "broken.agent.md").write_text(
        "---\nname: Broken\ndescription: [unterminated\n---\nInstructions\n",
        encoding="utf-8",
    )

    snapshot = load_project_snapshot(tmp_path)

    assert compose_binding_target(snapshot, "selected").definition.name == "Selected"


def test_binding_snapshot_retains_app_level_configuration(tmp_path: Path) -> None:
    _write_agent(
        tmp_path,
        "main.agent.md",
        "name: Main\ndescription: Main agent",
    )
    (tmp_path / "agents.config.yaml").write_text(
        "model: gpt-test\ntimeout: 42\n",
        encoding="utf-8",
    )

    snapshot = load_project_snapshot(tmp_path)

    assert snapshot.config.model == "gpt-test"
    assert snapshot.config.timeout == 42