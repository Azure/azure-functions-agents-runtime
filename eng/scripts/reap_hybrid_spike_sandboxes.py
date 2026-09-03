#!/usr/bin/env python3
"""Delete only expired sandboxes carrying the private hybrid-spike label."""

from __future__ import annotations

import argparse
import asyncio

from azure_functions_agents.experimental.hybrid_config import HybridSandboxSettings
from azure_functions_agents.experimental.hybrid_reaper import reap_hybrid_orphans

_CONFIRMATION = "delete-hybrid-spike-sandboxes"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sandbox-group-resource-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--app-hash")
    parser.add_argument("--minimum-age-seconds", required=True, type=int)
    parser.add_argument("--confirm", required=True, choices=(_CONFIRMATION,))
    return parser


async def _run(arguments: argparse.Namespace) -> int:
    if arguments.minimum_age_seconds < 1:
        raise ValueError("--minimum-age-seconds must be positive")
    settings = HybridSandboxSettings(
        group_resource_id=arguments.sandbox_group_resource_id,
        region=arguments.region,
        allowed_hosts=(),
        sandbox_disk="python-3.13",
        create_timeout_seconds=90,
        ready_timeout_seconds=45,
        drain_timeout_seconds=10,
        auto_delete_seconds=1800,
        orphan_age_seconds=arguments.minimum_age_seconds,
    )
    return await reap_hybrid_orphans(settings=settings, app_hash=arguments.app_hash)


def main(argv: list[str] | None = None) -> int:
    """Run the bounded hybrid-spike reaper."""
    deleted = asyncio.run(_run(_parser().parse_args(argv)))
    print(f"Deleted {deleted} expired hybrid-spike sandbox(es).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
