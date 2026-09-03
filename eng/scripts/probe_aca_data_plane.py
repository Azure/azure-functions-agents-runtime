#!/usr/bin/env python3
"""Fail fast when the ACA smoke identity lacks data-plane access.

A control-plane ARM read can pass while the data-plane role is missing. The
data-plane SDK retries 403 for minutes, so this read-only probe issues one
``list_sandboxes`` call under its own short deadline and treats a 401/403 or that
deadline as the authorization failure. Group id comes from
``AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID`` and
``AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_REGION``.
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
_GROUP_REGION_ENV_VAR = "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_REGION"
_IDENTITY_ENV_VAR = "AZURE_FUNCTIONS_AGENTS_ACA_PROBE_IDENTITY"
_DATA_OWNER_ROLE = "Container Apps SandboxGroup Data Owner"
_DATA_OWNER_ROLE_ID = "c24cf47c-5077-412d-a19c-45202126392c"
# The data-plane SDK retries 403 for minutes; stay well under that so denial fails fast.
_PROBE_TIMEOUT_SECONDS = 30.0


def _authorization_failure_message(group_resource_id: str, *, slow: bool = False) -> str:
    """Name the missing data-plane role and scope for a denied probe result."""

    identity = os.environ.get(_IDENTITY_ENV_VAR, "").strip()
    identity_sentence = f" Signed-in identity: {identity}." if identity else ""
    slow_sentence = (
        f" The probe hit its {_PROBE_TIMEOUT_SECONDS:.0f}s deadline instead of returning; the "
        "data-plane SDK retries 403 for minutes, so a slow failure here is itself a symptom of "
        "repeated authorization denials."
        if slow
        else ""
    )
    return (
        "ACA data-plane authorization failed: this identity was denied by the "
        "Sandbox Group data plane. Assign the "
        f"'{_DATA_OWNER_ROLE}' role (id {_DATA_OWNER_ROLE_ID}), scoped to the Sandbox Group "
        f"{group_resource_id}.{identity_sentence}{slow_sentence}"
    )


async def _probe(group_resource_id: str, region: str) -> None:
    """Issue one read-only, label-scoped list call under an explicit deadline."""

    adapter = await AcaSandboxAdapter.open(group_resource_id, region=region)
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
    region = os.environ.get(_GROUP_REGION_ENV_VAR, "").strip()
    if not region:
        print(
            f"{_GROUP_REGION_ENV_VAR} must be set to the Sandbox Group region.",
            file=sys.stderr,
        )
        return 1
    try:
        asyncio.run(_probe(group_resource_id, region))
    except TimeoutError:
        print(_authorization_failure_message(group_resource_id, slow=True), file=sys.stderr)
        return 1
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
