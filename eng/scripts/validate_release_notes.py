#!/usr/bin/env python3
"""Validate the structure and machine-readable metadata in docs/releases.md.

The validator is deliberately offline. It verifies the release-note contract but
leaves customer value, prose accuracy, security wording, and published-history
corrections to human review.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PATH = _REPO_ROOT / "docs" / "releases.md"
_METADATA_PREFIX = "<!-- release-note: "
_METADATA_RE = re.compile(r"^<!-- release-note: (\{.*\}) -->$")
_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc|\.dev)(\d+))?$")
_VERSION_HEADING_RE = re.compile(r"^## (\d+\.\d+\.\d+(?:(?:a|b|rc|\.dev)\d+)?)$")
_TABLE_VERSION_RE = re.compile(
    r"^\| \[(\d+\.\d+\.\d+(?:(?:a|b|rc|\.dev)\d+)?)\]\(#[^)]+\) \|"
)
_RELEASED_RE = re.compile(r"^\*\*Released ([A-Z][a-z]+ \d{1,2}, \d{4})\*\*")
_PYPI_RE = re.compile(r"https://pypi\.org/project/azurefunctions-agents-runtime/([^/]+)/")
_COMPARE_RE = re.compile(
    r"https://github\.com/Azure/azure-functions-agents-runtime/compare/([^\s)]+?)\.\.\.([^\s)]+)"
)
_NESTED_BULLET_RE = re.compile(r"^\s+[-*+]\s+")
_PLACEHOLDER_RE = re.compile(r"(?:\bT(?:BD|ODO)\b|<[^>]+>)", re.IGNORECASE)


class ReleaseCategory(StrEnum):
    """Canonical machine identifiers for release-note categories."""

    FEATURE = "feature"
    IMPROVEMENT = "improvement"
    BUG_FIX = "bug-fix"
    SECURITY = "security"
    COMPATIBILITY_DEPRECATION = "compatibility-deprecation"
    MAINTENANCE_DOCUMENTATION = "maintenance-documentation"


_CATEGORY_HEADINGS: dict[str, ReleaseCategory] = {
    "Features": ReleaseCategory.FEATURE,
    "Improvements": ReleaseCategory.IMPROVEMENT,
    "Bug fixes": ReleaseCategory.BUG_FIX,
    "Security": ReleaseCategory.SECURITY,
    "Compatibility and deprecations": ReleaseCategory.COMPATIBILITY_DEPRECATION,
    "Maintenance and documentation": ReleaseCategory.MAINTENANCE_DOCUMENTATION,
}
_ALLOWED_KEYS = frozenset({"id", "category", "prs", "frd"})
_ACTION_PHRASES = (
    "no customer action is required",
    "no action is required",
    "no migration is required",
    "before upgrading",
    "after upgrading",
    "to upgrade",
    "customers must",
    "applications must",
    "requires customers",
)


@dataclass(frozen=True)
class ReleaseMetadata:
    """Validated metadata attached to one release-note bullet."""

    id: str
    category: ReleaseCategory
    prs: tuple[int, ...]
    frd: str | None = None


@dataclass(frozen=True)
class ValidationFinding:
    """One actionable validation finding."""

    line: int
    message: str


class DuplicateJsonKeyError(ValueError):
    """Raised when strict JSON metadata repeats an object key."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKeyError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _parse_metadata(line: str) -> ReleaseMetadata:
    match = _METADATA_RE.fullmatch(line)
    if match is None:
        raise ValueError(
            "release-note metadata must be a single-line strict JSON comment in the documented format"
        )

    try:
        raw = json.loads(match.group(1), object_pairs_hook=_strict_object)
    except (json.JSONDecodeError, DuplicateJsonKeyError) as exc:
        raise ValueError(f"invalid release-note JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError("release-note metadata must be a JSON object")

    unknown = set(raw) - _ALLOWED_KEYS
    missing = {"id", "category", "prs"} - set(raw)
    if unknown:
        raise ValueError(f"unknown release-note metadata field(s): {', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"missing release-note metadata field(s): {', '.join(sorted(missing))}")

    identifier = raw["id"]
    if not isinstance(identifier, str) or _ID_RE.fullmatch(identifier) is None:
        raise ValueError("release-note id must be a lowercase semantic slug")

    category_value = raw["category"]
    if not isinstance(category_value, str):
        raise ValueError("release-note category must be a string")
    try:
        category = ReleaseCategory(category_value)
    except ValueError as exc:
        allowed = ", ".join(category.value for category in ReleaseCategory)
        raise ValueError(f"release-note category must be one of: {allowed}") from exc

    prs_value = raw["prs"]
    if not isinstance(prs_value, list) or not prs_value:
        raise ValueError("release-note prs must be a non-empty JSON array")
    if any(type(pr) is not int or pr <= 0 for pr in prs_value):
        raise ValueError("release-note prs must contain only positive integers")
    if len(set(prs_value)) != len(prs_value):
        raise ValueError("release-note prs must not contain duplicate PR numbers")

    frd_value = raw.get("frd")
    if frd_value is not None and (
        not isinstance(frd_value, str) or re.fullmatch(r"\d{4}", frd_value) is None
    ):
        raise ValueError("release-note frd must be a four-digit string")

    return ReleaseMetadata(identifier, category, tuple(prs_value), frd_value)


def _version_key(value: str) -> tuple[int, int, int, int, int]:
    """Return an ordering key for the PEP 440 forms used by this project."""

    match = _VERSION_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"unsupported release version {value!r}")
    major, minor, patch = (int(match.group(index)) for index in range(1, 4))
    phase = match.group(4)
    number = int(match.group(5) or 0)
    phase_order = {".dev": 0, "a": 1, "b": 2, "rc": 3, None: 4}
    return major, minor, patch, phase_order[phase], number


def _find_sections(lines: list[str]) -> tuple[list[tuple[str, int, int]], list[ValidationFinding]]:
    headings = [(index, line) for index, line in enumerate(lines) if line.startswith("## ")]
    sections: list[tuple[str, int, int]] = []
    findings: list[ValidationFinding] = []
    for position, (start, heading) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        match = _VERSION_HEADING_RE.fullmatch(heading)
        if match is not None:
            sections.append((match.group(1), start, end))

    unreleased = [index for index, line in enumerate(lines) if line == "## Unreleased"]
    if len(unreleased) != 1:
        findings.append(
            ValidationFinding(1, f"expected exactly one '## Unreleased' section; found {len(unreleased)}")
        )
    return sections, findings


def _validate_entry(
    lines: list[str],
    index: int,
    expected_category: ReleaseCategory | None,
    seen_ids: dict[str, int],
) -> tuple[int, list[ValidationFinding]]:
    findings: list[ValidationFinding] = []
    line_number = index + 1
    try:
        metadata = _parse_metadata(lines[index])
    except ValueError as exc:
        return index + 1, [ValidationFinding(line_number, str(exc))]

    previous_line = seen_ids.get(metadata.id)
    if previous_line is not None:
        findings.append(
            ValidationFinding(line_number, f"duplicate release-note id {metadata.id!r}; first used on line {previous_line}")
        )
    else:
        seen_ids[metadata.id] = line_number

    if expected_category is None:
        findings.append(ValidationFinding(line_number, "release-note entry is not under a canonical category heading"))
    elif metadata.category != expected_category:
        findings.append(
            ValidationFinding(
                line_number,
                f"metadata category {metadata.category.value!r} does not match the current heading",
            )
        )

    bullet_index = index + 1
    if bullet_index >= len(lines) or not lines[bullet_index].startswith("- "):
        findings.append(
            ValidationFinding(line_number, "release-note metadata must be immediately followed by one top-level bullet")
        )
        return index + 1, findings

    entry_lines = [lines[bullet_index][2:]]
    cursor = bullet_index + 1
    while cursor < len(lines):
        line = lines[cursor]
        if not line or line.startswith(("#", "- ", _METADATA_PREFIX)):
            break
        if _NESTED_BULLET_RE.match(line):
            findings.append(ValidationFinding(cursor + 1, "release-note entries must not contain nested lists"))
            cursor += 1
            continue
        if line.startswith(" "):
            entry_lines.append(line.strip())
            cursor += 1
            continue
        break

    entry_text = " ".join(entry_lines)
    if _PLACEHOLDER_RE.search(entry_text):
        findings.append(ValidationFinding(bullet_index + 1, "release-note entry contains an unresolved placeholder"))
    if metadata.category is ReleaseCategory.COMPATIBILITY_DEPRECATION and not any(
        phrase in entry_text.casefold() for phrase in _ACTION_PHRASES
    ):
        findings.append(
            ValidationFinding(
                bullet_index + 1,
                "compatibility entry must state required customer action or explicitly state that no action is required",
            )
        )

    return max(cursor, bullet_index + 1), findings


def _validate_unreleased(
    lines: list[str], unreleased_index: int, seen_ids: dict[str, int]
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    current_category: ReleaseCategory | None = None
    index = unreleased_index + 1
    while index < len(lines) and not lines[index].startswith("## "):
        line = lines[index]
        if line.startswith("### "):
            heading = line[4:]
            current_category = _CATEGORY_HEADINGS.get(heading)
            if current_category is None:
                findings.append(ValidationFinding(index + 1, f"unknown Unreleased category heading {heading!r}"))
            index += 1
            continue
        if "release-note:" in line:
            index, entry_findings = _validate_entry(lines, index, current_category, seen_ids)
            findings.extend(entry_findings)
            continue
        if line.startswith("- "):
            findings.append(
                ValidationFinding(index + 1, "Unreleased top-level bullets require release-note metadata")
            )
        index += 1

    unreleased_text = "\n".join(lines[unreleased_index + 1 : index])
    forbidden_patterns = {
        "a package version": r"\b\d+\.\d+\.\d+(?:(?:a|b|rc|\.dev)\d+)?\b",
        "a GitHub release URL": r"github\.com/Azure/azure-functions-agents-runtime/releases/tag/",
        "a PyPI version URL": r"pypi\.org/project/azurefunctions-agents-runtime/[^/]+/",
        "a comparison URL": r"github\.com/Azure/azure-functions-agents-runtime/compare/",
        "a release date marker": r"\*\*Released [A-Z][a-z]+ \d{1,2}, \d{4}\*\*",
    }
    for description, pattern in forbidden_patterns.items():
        match = re.search(pattern, unreleased_text)
        if match is not None:
            relative_line = unreleased_text[: match.start()].count("\n")
            findings.append(
                ValidationFinding(unreleased_index + relative_line + 2, f"Unreleased content must not contain {description}")
            )
    return findings


def _validate_published_metadata(
    lines: list[str], sections: list[tuple[str, int, int]], seen_ids: dict[str, int]
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for _, start, end in sections:
        current_category: ReleaseCategory | None = None
        index = start + 1
        while index < end:
            line = lines[index]
            if line.startswith("### "):
                current_category = _CATEGORY_HEADINGS.get(line[4:])
            if "release-note:" in line:
                index, entry_findings = _validate_entry(lines, index, current_category, seen_ids)
                findings.extend(entry_findings)
                continue
            index += 1
    return findings


def _validate_release_index(
    lines: list[str], sections: list[tuple[str, int, int]]
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    section_versions = [version for version, _, _ in sections]
    table_versions = [match.group(1) for line in lines if (match := _TABLE_VERSION_RE.match(line))]

    if len(section_versions) != len(set(section_versions)):
        findings.append(ValidationFinding(1, "published release version headings must be unique"))
    if len(table_versions) != len(set(table_versions)):
        findings.append(ValidationFinding(1, "at-a-glance release versions must be unique"))
    if table_versions != section_versions:
        findings.append(
            ValidationFinding(1, "at-a-glance versions must exactly match published version sections in order")
        )

    try:
        keys = [_version_key(version) for version in section_versions]
    except ValueError as exc:
        findings.append(ValidationFinding(1, str(exc)))
    else:
        if keys != sorted(keys, reverse=True):
            findings.append(ValidationFinding(1, "published release versions must be newest-first"))

    dates: list[date] = []
    for version, start, end in sections:
        section_lines = lines[start + 1 : end]
        released_matches = [match for line in section_lines if (match := _RELEASED_RE.match(line))]
        if len(released_matches) != 1:
            findings.append(
                ValidationFinding(start + 1, f"release {version} must contain exactly one release date")
            )
        else:
            try:
                dates.append(date.fromisoformat(_month_date_to_iso(released_matches[0].group(1))))
            except ValueError:
                findings.append(
                    ValidationFinding(start + 1, f"release {version} has an invalid release date")
                )

        pypi_matches = [match.group(1) for line in section_lines for match in [_PYPI_RE.search(line)] if match]
        if len(pypi_matches) != 1 or pypi_matches[0] != version:
            findings.append(
                ValidationFinding(start + 1, f"release {version} must contain one matching PyPI version URL")
            )

        for offset, line in enumerate(section_lines, start=start + 2):
            if "github.com/Azure/azure-functions-agents-runtime/compare/" in line and _COMPARE_RE.search(line) is None:
                findings.append(ValidationFinding(offset, "comparison links must contain two explicit Git tags"))

    if dates and dates != sorted(dates, reverse=True):
        findings.append(ValidationFinding(1, "published release dates must be newest-first"))
    return findings


def _month_date_to_iso(value: str) -> str:
    parsed = datetime.strptime(value, "%B %d, %Y").date()
    return parsed.isoformat()


def validate_text(text: str) -> list[ValidationFinding]:
    """Validate release-note Markdown and return all findings."""

    lines = text.splitlines()
    sections, findings = _find_sections(lines)
    unreleased_indices = [index for index, line in enumerate(lines) if line == "## Unreleased"]
    seen_ids: dict[str, int] = {}
    if len(unreleased_indices) == 1:
        findings.extend(_validate_unreleased(lines, unreleased_indices[0], seen_ids))
    findings.extend(_validate_published_metadata(lines, sections, seen_ids))
    findings.extend(_validate_release_index(lines, sections))
    return findings


def validate_file(path: Path) -> list[ValidationFinding]:
    """Read and validate one release-note Markdown file."""

    return validate_text(path.read_text(encoding="utf-8"))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate docs/releases.md structure")
    parser.add_argument("--path", type=Path, default=_DEFAULT_PATH, help="release-note Markdown path")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command-line validator."""

    try:
        args = _build_parser().parse_args(argv)
        if not args.path.is_file():
            _build_parser().error(f"file not found: {args.path}")
        findings = validate_file(args.path)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"ERROR: unexpected validation failure: {exc}", file=sys.stderr)
        return 3

    if findings:
        for finding in findings:
            print(f"{args.path}:{finding.line}: {finding.message}", file=sys.stderr)
        return 1

    display_path = args.path
    if args.path.resolve().is_relative_to(_REPO_ROOT):
        display_path = args.path.resolve().relative_to(_REPO_ROOT)
    print(f"OK: {display_path} release-note structure is valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
