#!/usr/bin/env python3
"""Delete leftover ACA smoke sandboxes created by this CI run.

Label-scoped to the CI smoke owner/app hashes so it only ever considers sandboxes
this pipeline created, then narrowed to the current run's per-run token so a reaper
never deletes a concurrently running job's live sandboxes. Meant to run as an
always() cleanup step after the smoke job. The Sandbox Group is read from
``AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID`` and the run token from
``AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_RUN_ID``.

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

from tests.live.aca_smoke_support import (
    aca_smoke_run_id,
    ci_smoke_reaper_labels,
    session_belongs_to_run,
)

from azure_functions_agents.transport.aca_sdk import AcaSandboxAdapter

_GROUP_RESOURCE_ID_ENV_VAR = "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID"


async def main() -> None:
    """Reap this run's CI smoke sandboxes in the configured Sandbox Group."""

    run_id = aca_smoke_run_id()
    adapter = await AcaSandboxAdapter.open(os.environ[_GROUP_RESOURCE_ID_ENV_VAR])
    try:
        sandboxes = await adapter.list_sandboxes(labels=ci_smoke_reaper_labels())
        reaped = 0
        for sandbox in sandboxes:
            if not session_belongs_to_run(sandbox.labels, run_id):
                continue
            await adapter.delete_sandbox(sandbox.sandbox_id)
            reaped += 1
        print(f"Reaped {reaped} ACA smoke sandbox(es) for run {run_id}.")
    finally:
        await adapter.close()


if __name__ == "__main__":
    asyncio.run(main())
