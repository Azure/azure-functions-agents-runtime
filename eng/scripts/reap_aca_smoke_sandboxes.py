#!/usr/bin/env python3
"""Delete leftover ACA smoke sandboxes created by the CI-dedicated owner/app.

Label-scoped to the CI smoke owner/app hashes so it only ever reaps sandboxes
this pipeline created. Meant to run as an always() cleanup step after the smoke
job. The Sandbox Group is read from
``AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID``.

Usage:
    python eng/scripts/reap_aca_smoke_sandboxes.py
"""

import asyncio
import os
import sys
from pathlib import Path

# Repo root on sys.path so the CI label selector is imported from the test-support
# module rather than duplicated here.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.live.aca_smoke_support import ci_smoke_reaper_labels

from azure_functions_agents.transport.aca_sdk import AcaSandboxAdapter

_GROUP_RESOURCE_ID_ENV_VAR = "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID"


async def main() -> None:
    """Reap every CI smoke sandbox in the configured Sandbox Group."""

    adapter = await AcaSandboxAdapter.open(os.environ[_GROUP_RESOURCE_ID_ENV_VAR])
    try:
        sandboxes = await adapter.list_sandboxes(labels=ci_smoke_reaper_labels())
        for sandbox in sandboxes:
            await adapter.delete_sandbox(sandbox.sandbox_id)
        print(f"Reaped {len(sandboxes)} ACA smoke sandbox(es).")
    finally:
        await adapter.close()


if __name__ == "__main__":
    asyncio.run(main())
