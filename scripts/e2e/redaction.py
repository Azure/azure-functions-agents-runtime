from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

REDACTED = "[REDACTED]"


def build_redactor(secret_values: Iterable[str]) -> Redactor:
    """Construct an immutable redactor for the provided secret string values."""

    filtered_values = tuple(
        sorted(
            {
                value
                for value in secret_values
                if value.strip() and len(value) >= 8
            },
            key=lambda value: (-len(value), value),
        )
    )
    pattern = (
        re.compile("|".join(re.escape(value) for value in filtered_values))
        if filtered_values
        else None
    )
    return Redactor(secret_values=filtered_values, pattern=pattern)


@dataclass(frozen=True, slots=True)
class Redactor:
    """Replace registered secret substrings with ``[REDACTED]`` longest-first."""

    secret_values: tuple[str, ...]
    pattern: re.Pattern[str] | None

    def redact_text(self, text: str) -> str:
        """Return *text* with every registered secret occurrence redacted."""

        redacted_text, _ = self._redact_with_count(text)
        return redacted_text

    def redact_file_in_place(self, path: Path) -> int:
        """Redact a UTF-8 text file in place and return the replacement count."""

        try:
            original_text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return 0

        redacted_text, replacements = self._redact_with_count(original_text)
        if replacements == 0:
            return 0

        temp_path = path.with_name(f"{path.name}.tmp")
        temp_path.write_text(redacted_text, encoding="utf-8")
        os.replace(temp_path, path)
        return replacements

    def redact_directory(
        self,
        root: Path,
        *,
        include_globs: tuple[str, ...] = ("*.log", "*.json", "*.txt"),
        exclude_globs: tuple[str, ...] = (),
    ) -> dict[Path, int]:
        """Redact every matching file under *root* and return per-file counts."""

        if not root.is_dir():
            return {}

        results: dict[Path, int] = {}
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue

            relative_path = path.relative_to(root)
            if not _matches_any_glob(relative_path, include_globs):
                continue
            if _matches_any_glob(relative_path, exclude_globs):
                continue

            results[path] = self.redact_file_in_place(path)
        return results

    def _redact_with_count(self, text: str) -> tuple[str, int]:
        """Apply the compiled redaction pattern and return text plus count."""

        if self.pattern is None:
            return text, 0
        return self.pattern.subn(REDACTED, text)


def _matches_any_glob(path: Path, globs: tuple[str, ...]) -> bool:
    """Return whether *path* matches any glob in *globs*."""

    return any(path.match(glob) for glob in globs)
