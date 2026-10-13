"""Immutable language-neutral declarations for local process-backed tools."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, cast


class PortableToolType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"


class LocalToolLanguage(StrEnum):
    JAVASCRIPT = "javascript"
    CSHARP = "csharp"
    GO = "go"


@dataclass(frozen=True)
class LocalToolParameter:
    name: str
    type: PortableToolType
    description: str


@dataclass(frozen=True)
class LocalToolSpec:
    name: str
    description: str
    entrypoint: str
    source_path: Path
    metadata_path: Path
    language: LocalToolLanguage
    parameters: tuple[LocalToolParameter, ...] = ()
    _input_schema_json: str = field(default="{}", repr=False)
    _contract_json: str = field(default="{}", repr=False)

    @classmethod
    def create(
        cls,
        *,
        name: str,
        description: str,
        entrypoint: str,
        source_path: Path,
        metadata_path: Path,
        parameters: tuple[LocalToolParameter, ...],
        language: LocalToolLanguage | None = None,
    ) -> LocalToolSpec:
        properties = {
            parameter.name: {
                "type": parameter.type.value,
                "description": parameter.description,
            }
            for parameter in parameters
        }
        input_schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if parameters:
            input_schema["required"] = [parameter.name for parameter in parameters]
        contract = {
            "name": name,
            "description": description,
            "entrypoint": entrypoint,
            "parameters": [
                {
                    "name": parameter.name,
                    "type": parameter.type.value,
                    "description": parameter.description,
                }
                for parameter in parameters
            ],
            "inputSchema": input_schema,
        }
        resolved_language = language
        if resolved_language is None:
            resolved_language = {
                ".js": LocalToolLanguage.JAVASCRIPT,
                ".cs": LocalToolLanguage.CSHARP,
                ".go": LocalToolLanguage.GO,
            }.get(source_path.suffix)
        if resolved_language is None:
            raise ValueError(f"Unsupported local tool source extension: {source_path.suffix}")
        return cls(
            name=name,
            description=description,
            entrypoint=entrypoint,
            source_path=source_path,
            metadata_path=metadata_path,
            language=resolved_language,
            parameters=parameters,
            _input_schema_json=json.dumps(
                input_schema,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            _contract_json=json.dumps(
                contract,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )

    def input_schema(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self._input_schema_json))

    def contract_json(self) -> str:
        return self._contract_json


@dataclass(frozen=True)
class LocalToolLaunch:
    command: Path
    arguments: tuple[str, ...]
    environment: tuple[tuple[str, str], ...] = ()

    @classmethod
    def create(
        cls,
        *,
        command: Path,
        arguments: tuple[str, ...],
        environment: tuple[tuple[str, str], ...] = (),
    ) -> LocalToolLaunch:
        resolved = command.resolve()
        if not resolved.is_absolute() or not resolved.is_file():
            raise ValueError(f"Local tool command is not an absolute regular file: {resolved}")
        return cls(command=resolved, arguments=arguments, environment=environment)


@dataclass(frozen=True)
class PreparedLocalTool:
    spec: LocalToolSpec
    source_sha256: str
    launch: LocalToolLaunch

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def description(self) -> str:
        return self.spec.description
