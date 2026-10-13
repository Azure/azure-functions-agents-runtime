"""Prepare local tools through their built-in language providers."""

from __future__ import annotations

from dataclasses import dataclass

from .._local_tool_descriptor import LocalToolSpec, PreparedLocalTool
from .providers import provider_for_spec


@dataclass(frozen=True)
class LocalToolPreparationResult:
    tools: tuple[PreparedLocalTool, ...]
    failed_loads: tuple[tuple[str, str], ...]


def prepare_local_tools(specs: tuple[LocalToolSpec, ...]) -> LocalToolPreparationResult:
    grouped: dict[object, list[LocalToolSpec]] = {}
    for spec in specs:
        provider = provider_for_spec(spec)
        grouped.setdefault(provider.language, []).append(spec)

    prepared: list[PreparedLocalTool] = []
    failures: list[tuple[str, str]] = []
    for language in sorted(grouped, key=lambda value: str(value)):
        provider = provider_for_spec(grouped[language][0])
        tools, failed_loads = provider.prepare_tools(tuple(grouped[language]))
        prepared.extend(tools)
        failures.extend(failed_loads)
    return LocalToolPreparationResult(tuple(prepared), tuple(failures))
