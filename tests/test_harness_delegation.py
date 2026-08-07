from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from azure_functions_agents.harness import SANDBOX_MARKER_ENV_VAR
from azure_functions_agents.harness.delegation import (
    DelegationReconstructionError,
    rebuild_agent_catalog,
    validate_delegation_graph,
)

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "config_scenarios" / "15_multi_agent_delegation"


def test_rebuild_agent_catalog_uses_delivered_agent_files(monkeypatch) -> None:
    monkeypatch.setenv(SANDBOX_MARKER_ENV_VAR, "1")

    catalog = rebuild_agent_catalog(_FIXTURE_ROOT)

    assert set(catalog) == {"billing", "coordinator", "shipping"}
    assert [reference.agent for reference in catalog["coordinator"].resolved.subagents] == [
        "billing",
        "shipping",
    ]
    assert catalog["billing"].resolved.subagents == []


def test_static_graph_rejects_cycles_and_deeper_delegation() -> None:
    cycle = [
        SimpleNamespace(slug="one", subagents=[SimpleNamespace(agent="two")]),
        SimpleNamespace(slug="two", subagents=[SimpleNamespace(agent="one")]),
    ]
    nested = [
        SimpleNamespace(slug="one", subagents=[SimpleNamespace(agent="two")]),
        SimpleNamespace(slug="two", subagents=[SimpleNamespace(agent="three")]),
        SimpleNamespace(slug="three", subagents=[]),
    ]

    with pytest.raises(DelegationReconstructionError):
        validate_delegation_graph(cycle)  # type: ignore[arg-type]
    with pytest.raises(DelegationReconstructionError):
        validate_delegation_graph(nested)  # type: ignore[arg-type]
