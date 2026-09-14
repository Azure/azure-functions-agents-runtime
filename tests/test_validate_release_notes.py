from pathlib import Path

import pytest
from eng.scripts import validate_release_notes as release_notes

VALID_METADATA = (
    '<!-- release-note: {"id":"customer-outcome","category":"feature","prs":[123],"frd":"0009"} -->'
)
VALID_BULLET = "- **Customer outcome.** Customers can now use the completed capability."


def release_document(unreleased: str = "", published_body: str = "") -> str:
    unreleased_content = f"\n{unreleased.rstrip()}\n" if unreleased else "\nNo changes are staged.\n"
    return f"""# Releases

## Unreleased
{unreleased_content}
## At a glance

| Version | Released | Highlights |
| --- | --- | --- |
| [0.1.0b2](#010b2) | January 2, 2026 | Current release |
| [0.1.0b1](#010b1) | January 1, 2026 | Earlier release |

## 0.1.0b2

**Released January 2, 2026** · [PyPI](https://pypi.org/project/azurefunctions-agents-runtime/0.1.0b2/)
{published_body}
[Compare 0.1.0b1...0.1.0b2](https://github.com/Azure/azure-functions-agents-runtime/compare/release0.1.0b1...v0.1.0b2)

## 0.1.0b1

**Released January 1, 2026** · [PyPI](https://pypi.org/project/azurefunctions-agents-runtime/0.1.0b1/)
"""


def messages(text: str) -> list[str]:
    return [finding.message for finding in release_notes.validate_text(text)]


def entry(metadata: str = VALID_METADATA, bullet: str = VALID_BULLET, heading: str = "Features") -> str:
    return f"### {heading}\n\n{metadata}\n{bullet}"


def test_empty_unreleased_and_historical_entries_are_valid() -> None:
    assert messages(release_document()) == []


def test_valid_metadata_unicode_and_crlf_are_valid() -> None:
    text = release_document(entry(bullet="- **Résumé support.** Customers can use café names."))
    assert messages(text.replace("\n", "\r\n")) == []


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (
            '<!-- release-note: {"id":"first","id":"second","category":"feature","prs":[1]} -->',
            "duplicate JSON key",
        ),
        (
            '<!-- release-note: {"id":"outcome","category":"feature","prs":[1],"extra":true} -->',
            "unknown release-note metadata field",
        ),
        ('<!-- release-note: {"category":"feature","prs":[1]} -->', "missing release-note metadata field"),
        ('<!-- release-note: {"id":"Bad_ID","category":"feature","prs":[1]} -->', "semantic slug"),
        ('<!-- release-note: {"id":"outcome","category":"other","prs":[1]} -->', "category must be one of"),
        ('<!-- release-note: {"id":"outcome","category":1,"prs":[1]} -->', "category must be a string"),
        ('<!-- release-note: {"id":"outcome","category":"feature","prs":[]} -->', "non-empty JSON array"),
        ('<!-- release-note: {"id":"outcome","category":"feature","prs":[true]} -->', "positive integers"),
        ('<!-- release-note: {"id":"outcome","category":"feature","prs":[1,1]} -->', "duplicate PR"),
        (
            '<!-- release-note: {"id":"outcome","category":"feature","prs":[1],"frd":9} -->',
            "four-digit string",
        ),
        ('<!-- release-note: {not-json} -->', "invalid release-note JSON"),
    ],
)
def test_invalid_metadata_is_rejected(metadata: str, expected: str) -> None:
    assert any(expected in message for message in messages(release_document(entry(metadata=metadata))))


def test_duplicate_semantic_ids_are_rejected_but_shared_prs_are_allowed() -> None:
    duplicate_id = "\n\n".join(
        [
            entry(),
            entry(
                metadata='<!-- release-note: {"id":"customer-outcome","category":"feature","prs":[124]} -->'
            ),
        ]
    )
    assert any("duplicate release-note id" in message for message in messages(release_document(duplicate_id)))

    shared_pr = "\n\n".join(
        [
            entry(),
            entry(
                metadata='<!-- release-note: {"id":"another-outcome","category":"feature","prs":[123]} -->'
            ),
        ]
    )
    assert messages(release_document(shared_pr)) == []


def test_one_outcome_can_reference_multiple_prs() -> None:
    metadata = '<!-- release-note: {"id":"customer-outcome","category":"feature","prs":[123,124]} -->'
    assert messages(release_document(entry(metadata=metadata))) == []


@pytest.mark.parametrize(
    ("unreleased", "expected"),
    [
        (f"### Features\n\n{VALID_METADATA}\n\n{VALID_BULLET}", "immediately followed"),
        (f"### Features\n\n{VALID_BULLET}", "require release-note metadata"),
        (f"### Features\n\n{VALID_METADATA}", "immediately followed"),
        (f"### Improvements\n\n{VALID_METADATA}\n{VALID_BULLET}", "does not match"),
        (f"### Other\n\n{VALID_METADATA}\n{VALID_BULLET}", "unknown Unreleased category"),
        (f"### Features\n\n {VALID_METADATA}\n{VALID_BULLET}", "documented format"),
        (
            f"### Features\n\n{VALID_METADATA}\n{VALID_BULLET}\n  - Nested detail",
            "must not contain nested lists",
        ),
    ],
)
def test_entry_shape_is_enforced(unreleased: str, expected: str) -> None:
    assert any(expected in message for message in messages(release_document(unreleased)))


@pytest.mark.parametrize(
    "forbidden",
    [
        "Version 0.2.0b1",
        "**Released January 3, 2026**",
        "https://github.com/Azure/azure-functions-agents-runtime/releases/tag/v0.2.0b1",
        "https://pypi.org/project/azurefunctions-agents-runtime/0.2.0b1/",
        "https://github.com/Azure/azure-functions-agents-runtime/compare/v1...v2",
        "TODO",
        "<PR number>",
    ],
)
def test_unreleased_rejects_release_metadata_and_placeholders(forbidden: str) -> None:
    unreleased = entry(bullet=f"- **Customer outcome.** {forbidden}")
    assert messages(release_document(unreleased))


def test_compatibility_entries_require_action_guidance() -> None:
    metadata = (
        '<!-- release-note: {"id":"compatibility-change",'
        '"category":"compatibility-deprecation","prs":[123]} -->'
    )
    without_guidance = entry(metadata, "- **Dependency change.** The dependency changed.", "Compatibility and deprecations")
    assert any("must state required customer action" in message for message in messages(release_document(without_guidance)))

    with_guidance = entry(
        metadata,
        "- **Dependency change.** No customer action is required.",
        "Compatibility and deprecations",
    )
    assert messages(release_document(with_guidance)) == []


def test_at_a_glance_versions_must_match_sections_and_be_newest_first() -> None:
    mismatch = release_document().replace("| [0.1.0b1](#010b1)", "| [0.1.0a1](#010a1)")
    assert any("must exactly match" in message for message in messages(mismatch))

    wrong_order = release_document().replace("## 0.1.0b2", "## TEMP", 1).replace(
        "## 0.1.0b1", "## 0.1.0b2", 1
    ).replace("## TEMP", "## 0.1.0b1", 1)
    assert any("newest-first" in message for message in messages(wrong_order))


def test_pypi_version_and_comparison_shape_are_validated() -> None:
    wrong_pypi = release_document().replace(
        "azurefunctions-agents-runtime/0.1.0b2/", "azurefunctions-agents-runtime/0.1.0b3/", 1
    )
    assert any("matching PyPI" in message for message in messages(wrong_pypi))

    malformed_compare = release_document().replace("release0.1.0b1...v0.1.0b2", "v0.1.0b2")
    assert any("two explicit Git tags" in message for message in messages(malformed_compare))


def test_partial_promotion_preserves_metadata_and_remaining_unreleased_entry() -> None:
    remaining = entry()
    promoted_metadata = (
        '<!-- release-note: {"id":"promoted-outcome","category":"feature","prs":[120]} -->'
    )
    promoted = f"\n### Features\n\n{promoted_metadata}\n- **Promoted outcome.** Customers can use it.\n"
    assert messages(release_document(remaining, published_body=promoted)) == []


def test_promoted_metadata_requires_a_canonical_category_heading() -> None:
    promoted = f"\n### Features and improvements\n\n{VALID_METADATA}\n{VALID_BULLET}\n"
    assert any(
        "not under a canonical category heading" in message
        for message in messages(release_document(published_body=promoted))
    )


def test_exactly_one_unreleased_section_is_required() -> None:
    assert any("exactly one" in message for message in messages(release_document().replace("## Unreleased", "## Draft")))
    assert any("exactly one" in message for message in messages(release_document() + "\n## Unreleased\n"))


def test_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    valid_path = tmp_path / "valid.md"
    valid_path.write_text(release_document(), encoding="utf-8")
    assert release_notes.main(["--path", str(valid_path)]) == 0

    invalid_path = tmp_path / "invalid.md"
    invalid_path.write_text("# Releases\n", encoding="utf-8")
    assert release_notes.main(["--path", str(invalid_path)]) == 1

    with pytest.raises(SystemExit) as usage_error:
        release_notes.main(["--path", str(tmp_path / "missing.md")])
    assert usage_error.value.code == 2

    def fail_unexpectedly(path: Path) -> list[release_notes.ValidationFinding]:
        raise OSError(f"cannot read {path}")

    monkeypatch.setattr(release_notes, "validate_file", fail_unexpectedly)
    assert release_notes.main(["--path", str(valid_path)]) == 3
    assert "unexpected validation failure" in capsys.readouterr().err
