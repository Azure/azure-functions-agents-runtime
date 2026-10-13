"""Built-in provider registry for process-backed local tools."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .._local_tool_descriptor import LocalToolLanguage, LocalToolSpec, PreparedLocalTool

type ValidateLocalToolSpec = Callable[[LocalToolSpec], None]
type PrepareLocalTools = Callable[
    [tuple[LocalToolSpec, ...]], tuple[tuple[PreparedLocalTool, ...], tuple[tuple[str, str], ...]]
]


@dataclass(frozen=True)
class LocalToolProvider:
    language: LocalToolLanguage
    extensions: frozenset[str]
    validate_spec: ValidateLocalToolSpec
    prepare_tools: PrepareLocalTools
    requires_explicit_entrypoint: bool = False


def _javascript_provider() -> LocalToolProvider:
    from .javascript import prepare_javascript_tools, validate_javascript_spec

    def prepare(
        specs: tuple[LocalToolSpec, ...],
    ) -> tuple[tuple[PreparedLocalTool, ...], tuple[tuple[str, str], ...]]:
        result = prepare_javascript_tools(specs)
        return result.tools, result.failed_loads

    return LocalToolProvider(
        language=LocalToolLanguage.JAVASCRIPT,
        extensions=frozenset({".js"}),
        validate_spec=validate_javascript_spec,
        prepare_tools=prepare,
    )


def _csharp_provider() -> LocalToolProvider:
    from .csharp import prepare_csharp_tools, validate_csharp_spec

    def prepare(
        specs: tuple[LocalToolSpec, ...],
    ) -> tuple[tuple[PreparedLocalTool, ...], tuple[tuple[str, str], ...]]:
        result = prepare_csharp_tools(specs)
        return result.tools, result.failed_loads

    return LocalToolProvider(
        language=LocalToolLanguage.CSHARP,
        extensions=frozenset({".cs"}),
        validate_spec=validate_csharp_spec,
        prepare_tools=prepare,
    )


def _go_provider() -> LocalToolProvider:
    from .go import prepare_go_tools, validate_go_spec

    def prepare(
        specs: tuple[LocalToolSpec, ...],
    ) -> tuple[tuple[PreparedLocalTool, ...], tuple[tuple[str, str], ...]]:
        result = prepare_go_tools(specs)
        return result.tools, result.failed_loads

    return LocalToolProvider(
        language=LocalToolLanguage.GO,
        extensions=frozenset({".go"}),
        validate_spec=validate_go_spec,
        prepare_tools=prepare,
        requires_explicit_entrypoint=True,
    )


def providers() -> tuple[LocalToolProvider, ...]:
    return (_javascript_provider(), _csharp_provider(), _go_provider())


def provider_by_extension() -> dict[str, LocalToolProvider]:
    return {
        extension: provider
        for provider in providers()
        for extension in provider.extensions
    }


def provider_for_spec(spec: LocalToolSpec) -> LocalToolProvider:
    for provider in providers():
        if provider.language is spec.language:
            return provider
    raise ValueError(f"No local tool provider is registered for {spec.language.value}")


def supported_source_files(tool_dir: Path) -> tuple[tuple[Path, LocalToolProvider], ...]:
    registry = provider_by_extension()
    return tuple(
        (path, registry[path.suffix])
        for path in sorted(tool_dir.iterdir())
        if path.is_file()
        and not path.name.startswith(".")
        and path.suffix in registry
        and path.suffix == path.suffix.lower()
    )
