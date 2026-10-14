from __future__ import annotations

from pathlib import Path

from azure_functions_agents.harness._harness_binding import AppHarness, HarnessKind
from azure_functions_agents.harness.copilot_sdk._copilot_preview import (
    CopilotHarnessAdapter,
    copilot_adapter,
)
from azure_functions_agents.harness.copilot_sdk._copilot_providers import (
    CopilotProvider,
    OpenAIProvider,
)
from azure_functions_agents.harness.copilot_sdk._copilot_session_identity import (
    StorageRoute,
    resolve_route,
)


def make_copilot_harness(
    app_root: Path,
    *,
    storage_root: Path | None = None,
    default_model: str = "gpt-4.1-mini",
    provider: CopilotProvider | None = None,
    session_storage: StorageRoute | None = None,
) -> AppHarness:
    return AppHarness(
        HarnessKind.COPILOT,
        app_root,
        CopilotHarnessAdapter(
            storage_root=storage_root or app_root / "state",
            default_model=default_model,
            provider=provider or OpenAIProvider("not-a-credential"),
            session_storage=session_storage or resolve_route(app_root),
        ),
    )


def copilot_state(harness: AppHarness) -> CopilotHarnessAdapter:
    return copilot_adapter(harness)
