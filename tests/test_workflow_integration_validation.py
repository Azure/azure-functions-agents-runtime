"""Shape-checking tests for the ``workflows`` frontmatter block.

These tests focus on the cheap fail-fast guards that
``build_workflow_integration`` runs at app start regardless of whether
workflows are enabled — non-mapping shape, unknown / unsupported keys,
and strict-boolean ``enabled``. They guarantee that obvious frontmatter
mistakes (typos, YAML foot-guns, unsupported knobs) surface with a
clear error pointing at the offending field, instead of silently
degrading to defaults.

The Durable execution backend (Azure Storage vs Durable Task
Scheduler) is configured entirely via ``host.json``'s ``storageProvider``
block and matching app settings. The library never reads or routes on
backend, so there is no ``workflows.backend`` field — these tests
explicitly assert that asserting one in frontmatter is rejected as an
unknown key.
"""

from __future__ import annotations

import pytest

from azure_functions_agents._function_tool import WorkflowTool
from azure_functions_agents.workflows import integration, registry


@pytest.fixture(autouse=True)
def _reset_registry():
    """Restore registry state around every test.

    ``build_workflow_integration`` mutates ``registry._APP_ALLOWLIST``
    on the enabled path; isolate that side-effect so test order can't
    leak the allowlist between cases.
    """
    saved_entries = dict(registry._REGISTRY)
    saved_allow = registry.get_app_config()
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved_entries)
    if saved_allow is not None:
        registry.set_app_config(saved_allow)
    else:
        registry._APP_ALLOWLIST = None


class _FakeApp:
    """Minimal stand-in for ``func.FunctionApp`` so we can drive
    ``build_workflow_integration`` without spinning up the real
    Functions host."""

    def __init__(self):
        self.blueprints = []

    def register_blueprint(self, bp):
        self.blueprints.append(bp)


# ---------------------------------------------------------------------------
# block-shape rejection
# ---------------------------------------------------------------------------


def test_workflows_block_rejects_non_mapping():
    with pytest.raises(RuntimeError, match="workflows must be a mapping"):
        integration.build_workflow_integration(
            _FakeApp(), {"workflows": "yes please"}
        )


def test_workflows_block_rejects_non_mapping_list():
    with pytest.raises(RuntimeError, match="workflows must be a mapping"):
        integration.build_workflow_integration(
            _FakeApp(), {"workflows": ["enabled"]}
        )


# ---------------------------------------------------------------------------
# unknown-key rejection
# ---------------------------------------------------------------------------


def test_workflows_block_rejects_unknown_key_typo():
    """Typos like ``enabld`` should fail loudly at app start."""
    with pytest.raises(RuntimeError) as excinfo:
        integration.build_workflow_integration(
            _FakeApp(),
            {"workflows": {"enabld": True}},
        )
    msg = str(excinfo.value)
    assert "unknown key" in msg
    assert "enabld" in msg
    # Plain typo: must NOT include the host.json hint — that hint is
    # reserved for the two real-Durable-concept keys (backend,
    # task_hub) and would mislead a contributor who just fat-fingered.
    assert "host.json" not in msg
    assert "storageProvider" not in msg


def test_workflows_block_rejects_backend_key():
    """``workflows.backend`` is intentionally NOT a frontmatter
    knob — backend selection is done in host.json via the Durable
    extension's ``storageProvider`` block. A frontmatter declaration
    would be a parallel assertion that can drift from the truth, so
    we reject it explicitly with a hint pointing at host.json.
    """
    with pytest.raises(RuntimeError) as excinfo:
        integration.build_workflow_integration(
            _FakeApp(),
            {"workflows": {"enabled": True, "backend": "dts"}},
        )
    msg = str(excinfo.value)
    assert "unknown key" in msg
    assert "backend" in msg
    # The hint should help a confused author find the right place.
    assert "host.json" in msg
    assert "storageProvider" in msg


def test_workflows_block_rejects_task_hub_key():
    """Same reasoning as ``backend`` — task hub selection is done in
    host.json via ``extensions.durableTask.hubName``, not in agent
    frontmatter. The hint points at the right place (NOT
    storageProvider, which is for backend selection).
    """
    with pytest.raises(RuntimeError) as excinfo:
        integration.build_workflow_integration(
            _FakeApp(),
            {"workflows": {"enabled": True, "task_hub": "myhub"}},
        )
    msg = str(excinfo.value)
    assert "unknown key" in msg
    assert "task_hub" in msg
    assert "hubName" in msg
    # task_hub is unrelated to storage provider; that hint would
    # be wrong here.
    assert "storageProvider" not in msg


def test_workflows_block_lists_supported_keys_in_error():
    """Error message must list the actually-supported keys so the
    contributor knows what was expected."""
    with pytest.raises(RuntimeError) as excinfo:
        integration.build_workflow_integration(
            _FakeApp(),
            {"workflows": {"bogus": 1}},
        )
    msg = str(excinfo.value)
    assert "enabled" in msg
    assert "exclude" in msg
    assert "allowed_tools" not in msg


def test_workflows_block_rejects_allowed_tools():
    with pytest.raises(RuntimeError) as excinfo:
        integration.build_workflow_integration(
            _FakeApp(),
            {"workflows": {"enabled": True, "allowed_tools": ["fetch_url"]}},
            workflow_tools=[],
        )
    msg = str(excinfo.value)
    assert "unknown key" in msg
    assert "allowed_tools" in msg


# ---------------------------------------------------------------------------
# strict-boolean ``enabled``
# ---------------------------------------------------------------------------


def test_workflows_block_rejects_non_bool_enabled_string():
    """``enabled: "false"`` in YAML is a foot-gun — without strict
    parsing the string is truthy and workflows would silently activate.
    """
    with pytest.raises(RuntimeError, match=r"workflows\.enabled must be a boolean"):
        integration.build_workflow_integration(
            _FakeApp(), {"workflows": {"enabled": "false"}}
        )


def test_workflows_block_rejects_non_bool_enabled_truthy_string():
    with pytest.raises(RuntimeError, match=r"workflows\.enabled must be a boolean"):
        integration.build_workflow_integration(
            _FakeApp(), {"workflows": {"enabled": "true"}}
        )


def test_workflows_block_rejects_non_bool_enabled_int():
    with pytest.raises(RuntimeError, match=r"workflows\.enabled must be a boolean"):
        integration.build_workflow_integration(
            _FakeApp(), {"workflows": {"enabled": 1}}
        )


def test_workflows_block_rejects_none_enabled():
    """``enabled: null`` (YAML) — distinct from "field omitted" and
    not a valid bool.
    """
    with pytest.raises(RuntimeError, match=r"workflows\.enabled must be a boolean"):
        integration.build_workflow_integration(
            _FakeApp(), {"workflows": {"enabled": None}}
        )


# ---------------------------------------------------------------------------
# validation runs on the disabled path too
# ---------------------------------------------------------------------------


def test_validation_runs_on_disabled_path_unknown_key():
    """A typo'd key with ``enabled: false`` should still fail at app
    start — otherwise the typo survives until the user flips enable
    on, at which point they have to debug a build that used to work.
    """
    with pytest.raises(RuntimeError, match="unknown key"):
        integration.build_workflow_integration(
            _FakeApp(),
            {"workflows": {"enabled": False, "bogus": 1}},
        )


def test_validation_runs_on_disabled_path_non_bool_enabled():
    with pytest.raises(RuntimeError, match=r"workflows\.enabled must be a boolean"):
        integration.build_workflow_integration(
            _FakeApp(),
            {"workflows": {"enabled": "no"}},
        )


def test_validation_runs_on_disabled_path_malformed_exclude():
    """``exclude`` shape must surface even when ``enabled: false``."""
    with pytest.raises(
        RuntimeError, match=r"workflows\.exclude must be a list of non-empty strings"
    ):
        integration.build_workflow_integration(
            _FakeApp(),
            {"workflows": {"enabled": False, "exclude": "fetch_url"}},
        )


# ---------------------------------------------------------------------------
# exclude shape (moved from registry tests so it lives next to
# the other validators)
# ---------------------------------------------------------------------------


def test_exclude_rejects_non_list():
    with pytest.raises(
        RuntimeError, match=r"workflows\.exclude must be a list of non-empty strings"
    ):
        integration.build_workflow_integration(
            _FakeApp(),
            {"workflows": {"enabled": True, "exclude": "not-a-list"}},
        )


def test_exclude_rejects_empty_string_in_list():
    with pytest.raises(
        RuntimeError, match=r"workflows\.exclude must be a list of non-empty strings"
    ):
        integration.build_workflow_integration(
            _FakeApp(),
            {"workflows": {"enabled": True, "exclude": ["fetch_url", ""]}},
        )


def test_exclude_rejects_non_string_in_list():
    with pytest.raises(
        RuntimeError, match=r"workflows\.exclude must be a list of non-empty strings"
    ):
        integration.build_workflow_integration(
            _FakeApp(),
            {"workflows": {"enabled": True, "exclude": ["fetch_url", 42]}},
        )


# ---------------------------------------------------------------------------
# happy paths — nothing wrong with the block
# ---------------------------------------------------------------------------


def test_no_workflows_key_at_all_is_fine():
    """Agents without any workflows declaration must keep working."""
    result = integration.build_workflow_integration(_FakeApp(), {})
    assert result.workflow_tools == []
    assert result.chat_system_addendum is None
    assert result.trigger_system_addendum is None
    assert result.enabled is False


def test_result_preserves_legacy_two_value_unpacking():
    result = integration.build_workflow_integration(_FakeApp(), {})

    workflow_tools, system_addendum = result

    assert workflow_tools == []
    assert system_addendum is None


def test_disabled_explicitly_is_a_noop():
    result = integration.build_workflow_integration(
        _FakeApp(), {"workflows": {"enabled": False}}
    )
    assert result.workflow_tools == []
    assert result.chat_system_addendum is None
    assert result.trigger_system_addendum is None
    assert result.enabled is False


def test_enabled_with_no_other_keys_works():
    result = integration.build_workflow_integration(
        _FakeApp(), {"workflows": {"enabled": True}}, workflow_tools=[]
    )
    assert result.workflow_tools
    assert result.chat_system_addendum is not None
    assert result.trigger_system_addendum is not None
    assert result.enabled is True


def test_enabled_with_exclude_filters_workflow_tools():
    workflow_tools = [
        WorkflowTool("keep", "Keep tool", lambda args: {"args": args}),
        WorkflowTool("skip", "Skip tool", lambda args: {"args": args}),
    ]

    result = integration.build_workflow_integration(
        _FakeApp(),
        {"workflows": {"enabled": True, "exclude": ["skip"]}},
        workflow_tools=workflow_tools,
    )
    assert result.workflow_tools
    assert "`keep`" in result.chat_system_addendum
    assert "`skip`" not in result.chat_system_addendum
    assert "`keep`" in result.trigger_system_addendum
    assert "`skip`" not in result.trigger_system_addendum


def test_enabled_builds_channel_specific_workflow_guidance():
    result = integration.build_workflow_integration(
        _FakeApp(),
        {"workflows": {"enabled": True}},
        workflow_tools=[
            WorkflowTool("publish_result", "Publish the final result.", lambda args: args)
        ],
    )

    assert result.enabled is True
    assert "`publish_result`" in result.chat_system_addendum
    assert "`publish_result`" in result.trigger_system_addendum
    assert "<workflow-notification>" in result.chat_system_addendum
    assert "There is no built-in chat poller" in result.trigger_system_addendum
    assert "<workflow-id>" not in result.trigger_system_addendum
    assert "do not poll" in result.trigger_system_addendum.lower()
    assert "terminal" in result.trigger_system_addendum.lower()
    assert "sink" in result.trigger_system_addendum.lower()
    assert "final tool task" in result.trigger_system_addendum
    assert "Activity" not in result.trigger_system_addendum
    assert "Durable timers" not in result.trigger_system_addendum
