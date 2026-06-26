"""Workflow incident-triage sample app.

Registers the four sample-specific workflow-safe tools
(``fetch_logs``, ``fetch_metrics``, ``fetch_deploys``,
``summarize_findings``) with the workflows engine before the agent app
is built. Once ``create_function_app()`` runs, it sees
``workflows.enabled: true`` plus the ``workflows.allowed_tools`` list in
``main.agent.md`` and wires the tools into the agent's plan validator
and system prompt.
"""

from incident_tools import register_with_engine

register_with_engine()

from azure_functions_agents import create_function_app  # noqa: E402

app = create_function_app()
