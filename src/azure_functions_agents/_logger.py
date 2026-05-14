"""Shared logger for the Azure Functions Agent Runtime.

All modules in this package use this single logger so that operators can
filter or configure the entire runtime's output in one place.

In Application Insights the ``customDimensions.Category`` field will be
``azure.functions.AgentRuntime`` — consistent with the ``azure.functions.*``
naming convention used by the Azure Functions Python SDK (e.g.
``azure.functions.AsgiMiddleware``).
"""

import logging

logger = logging.getLogger("azure.functions.AgentRuntime")
