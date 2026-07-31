from __future__ import annotations

import base64
import hashlib

import pytest

from azure_functions_agents.session_state import (
    LABEL_SAFE_PAYLOAD_LENGTH,
    LABEL_SAFE_PAYLOAD_PATTERN,
    encode_label_safe_digest,
)


def test_sha256_digest_encodes_to_exactly_52_lowercase_base32_characters() -> None:
    digest = hashlib.sha256(b"azure-functions-agents-runtime").digest()

    encoded = encode_label_safe_digest(digest)

    assert len(encoded) == LABEL_SAFE_PAYLOAD_LENGTH == 52
    assert encoded == encoded.lower()
    assert "=" not in encoded
    assert LABEL_SAFE_PAYLOAD_PATTERN.fullmatch(encoded)


def test_encoding_is_deterministic_and_matches_stdlib_base32() -> None:
    digest = hashlib.sha256(b"determinism-check").digest()

    first = encode_label_safe_digest(digest)
    second = encode_label_safe_digest(digest)

    assert first == second
    assert first == base64.b32encode(digest).decode("ascii").rstrip("=").lower()


def test_different_digests_encode_to_different_payloads() -> None:
    left = encode_label_safe_digest(hashlib.sha256(b"left").digest())
    right = encode_label_safe_digest(hashlib.sha256(b"right").digest())

    assert left != right
    assert LABEL_SAFE_PAYLOAD_PATTERN.fullmatch(left)
    assert LABEL_SAFE_PAYLOAD_PATTERN.fullmatch(right)


@pytest.mark.parametrize(
    "invalid",
    [
        "A" * 52,  # uppercase must be rejected
        "a" * 51,  # too short
        "a" * 53,  # too long
        "a" * 51 + "=",  # padding must be rejected
        "a" * 51 + "1",  # '1' is not in the base32 alphabet
        "a" * 51 + "0",  # '0' is not in the base32 alphabet
        "a" * 51 + "8",  # '8' is not in the base32 alphabet
        "a" * 51 + "9",  # '9' is not in the base32 alphabet
        "",
    ],
)
def test_pattern_rejects_uppercase_padding_and_malformed_lengths(invalid: str) -> None:
    assert LABEL_SAFE_PAYLOAD_PATTERN.fullmatch(invalid) is None


def test_pattern_accepts_full_base32_alphabet() -> None:
    # a-z minus the RFC 4648 exclusions (0, 1, 8, 9) plus 2-7.
    alphabet = "abcdefghijklmnopqrstuvwxyz234567"
    payload = (alphabet * 2)[:52]

    assert LABEL_SAFE_PAYLOAD_PATTERN.fullmatch(payload)
