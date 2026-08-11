#!/usr/bin/env python3
"""Fail fast when the ACA smoke identity lacks data-plane access.

A control-plane ARM read can pass while the data-plane role is missing, so this
read-only probe issues one label-scoped ``list_sandboxes`` call and turns a
401/403 into an immediate named diagnosis. The Sandbox Group is read from
``AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID``.
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.aca_smoke_diagnostics import is_aca_authorization_failure
from tests.live.aca_smoke_support import ci_smoke_reaper_labels

from azure_functions_agents.transport.aca_sdk import AcaSandboxAdapter

_GROUP_RESOURCE_ID_ENV_VAR = "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID"
_IDENTITY_ENV_VAR = "AZURE_FUNCTIONS_AGENTS_ACA_PROBE_IDENTITY"
_DATA_OWNER_ROLE = "Container Apps SandboxGroup Data Owner"
_DATA_OWNER_ROLE_ID = "c24cf47c-5077-412d-a19c-45202126392c"
_PROBE_TIMEOUT_SECONDS = 30.0


def _authorization_failure_message(group_resource_id: str) -> str:
    """Name the missing data-plane role and scope for a 401/403 probe result."""

    identity = os.environ.get(_IDENTITY_ENV_VAR, "").strip()
    identity_sentence = f" Signed-in identity: {identity}." if identity else ""
    return (
        "ACA data-plane authorization failed: this identity can reach the Sandbox Group "
        "over ARM but is denied on the data plane. Assign the "
        f"'{_DATA_OWNER_ROLE}' role (id {_DATA_OWNER_ROLE_ID}), scoped to the Sandbox Group "
        f"{group_resource_id}.{identity_sentence}"
    )


async def _probe(group_resource_id: str) -> None:
    """Issue one read-only, label-scoped list call against the data plane."""

    adapter = await AcaSandboxAdapter.open(group_resource_id)
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
            await adapter.list_sandboxes(labels=ci_smoke_reaper_labels())
    finally:
        await adapter.close()


def main() -> int:
    group_resource_id = os.environ.get(_GROUP_RESOURCE_ID_ENV_VAR, "").strip()
    if not group_resource_id:
        print(
            f"{_GROUP_RESOURCE_ID_ENV_VAR} must be set to the Sandbox Group resource ID.",
            file=sys.stderr,
        )
        return 1
    try:
        asyncio.run(_probe(group_resource_id))
    except Exception as error:
        if is_aca_authorization_failure(error):
            print(_authorization_failure_message(group_resource_id), file=sys.stderr)
            return 1
        print(f"ACA data-plane reachability probe failed: {error!r}", file=sys.stderr)
        return 1
    print("ACA data-plane authorization probe succeeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
