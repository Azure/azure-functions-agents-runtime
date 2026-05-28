from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

type SecretPredicate = Callable[[str], bool]

DEFAULT_SECRET_NAMES: frozenset[str] = frozenset(
    {
        "AZURE_TENANT_ID",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
        "FOUNDRY_PROJECT_ENDPOINT",
        "O365_MCP_SERVER_URL",
        "O365_MCP_CLIENT_ID",
        "ACA_SESSION_POOL_ENDPOINT",
    }
)


@dataclass(frozen=True)
class EnvCheckResult:
    """Summarize whether a sample's required environment variables are available."""

    sample_name: str
    missing: tuple[str, ...]
    present: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """Return True when every required environment variable is non-empty."""

        return not self.missing


def check_required_env(
    *,
    sample_name: str,
    required_env_vars: tuple[str, ...],
    env: dict[str, str] | None = None,
) -> EnvCheckResult:
    """Check that each required environment variable is present and non-empty."""

    resolved_env = _resolve_env(env)
    present: list[str] = []
    missing: list[str] = []

    for name in required_env_vars:
        if _normalized_value(resolved_env, name):
            present.append(name)
        else:
            missing.append(name)

    return EnvCheckResult(
        sample_name=sample_name,
        missing=tuple(missing),
        present=tuple(present),
    )


def write_redacted_settings(
    *,
    sample_name: str,
    env_var_names: tuple[str, ...],
    artifacts_dir: Path,
    env: dict[str, str] | None = None,
    secret_predicate: SecretPredicate | None = None,
) -> Path:
    """Write a deterministic, redacted JSON view of resolved environment variables."""

    resolved_env = _resolve_env(env)
    is_secret = secret_predicate or _default_secret_predicate
    output_path = artifacts_dir / sample_name / "settings.redacted.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload: dict[str, dict[str, bool | int | str]] = {}
    for name in sorted(env_var_names):
        value = _normalized_value(resolved_env, name)
        present = bool(value)
        payload[name] = {
            "present": present,
            "length": len(value) if present else 0,
            "preview": _preview_value(name=name, value=value, secret_predicate=is_secret),
        }

    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_path


def _resolve_env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _normalized_value(env: Mapping[str, str], name: str) -> str:
    return env.get(name, "").strip()


def _default_secret_predicate(name: str) -> bool:
    return name in DEFAULT_SECRET_NAMES


def _preview_value(*, name: str, value: str, secret_predicate: SecretPredicate) -> str:
    if not value:
        return ""
    if secret_predicate(name):
        return "[REDACTED]"
    return f"{value[:5]}..."
