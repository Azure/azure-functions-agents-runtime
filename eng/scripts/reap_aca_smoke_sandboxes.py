#!/usr/bin/env python3
"""Delete leftover ACA smoke sandboxes created by this CI run.

Label-scoped to the current Function App smoke identity and the low-level CI smoke
owner/app hashes. The latter is narrowed to the current run's per-run token so a
reaper never deletes a concurrently running job's live sandboxes. Meant to run as an
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

from tests.aca_smoke_diagnostics import AcaSmokeEnvironmentError
from tests.live.aca_smoke_support import (
    aca_smoke_run_id,
    ci_smoke_reaper_labels,
    production_smoke_reaper_labels,
    reap_labelled_sandbox_family,
    session_belongs_to_run,
)

from azure_functions_agents.transport.aca_sdk import AcaSandboxAdapter

_GROUP_RESOURCE_ID_ENV_VAR = "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID"


async def main() -> None:
    """Reap this run's CI smoke sandboxes in the configured Sandbox Group."""

    run_id = aca_smoke_run_id()
    adapter = await AcaSandboxAdapter.open(os.environ[_GROUP_RESOURCE_ID_ENV_VAR])
    try:
        reaped = 0
        cleanup_errors: list[str] = []
        for family, labels, matches in (
            ("production", production_smoke_reaper_labels(), None),
            (
                "current-run-ci",
                ci_smoke_reaper_labels(),
                lambda labels: session_belongs_to_run(labels, run_id),
            ),
        ):
            try:
                reaped += await reap_labelled_sandbox_family(
                    adapter,
                    labels,
                    matches=matches,
                )
            except Exception as error:
                cleanup_errors.append(
                    f"{family}:{type(error).__name__}"
                )
        if cleanup_errors:
            raise AcaSmokeEnvironmentError(
                "ACA smoke cleanup could not be confirmed: "
                f"operation-errors={','.join(cleanup_errors)}."
            ) from None
        print(f"Reaped {reaped} ACA smoke sandbox(es) for run {run_id}.")
    finally:
        await adapter.close()


if __name__ == "__main__":
    asyncio.run(main())
