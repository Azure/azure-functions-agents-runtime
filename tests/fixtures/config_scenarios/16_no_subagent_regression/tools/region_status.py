"""A single discoverable user tool for the ``16_no_subagent_regression`` fixture.

Exists so ``build_capabilities()``'s ``tools: false`` handling
(``resource_summary.agent.md``) has something real to strip in tests — proving
the distinction between "tools disabled for this agent" and "no tools were
ever discovered for any agent" (see ``test_config_fixtures.py`` and
``test_app.py``'s tests for this fixture).
"""


def get_region_status(region: str) -> str:
    """Return a canned operational status string for the given region."""
    return f"{region}: operational"
