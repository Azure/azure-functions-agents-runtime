"""Shared RFC 4648 base32 encoding for label-safe versioned identity hashes.

Azure Container Apps (ACA) Sandbox labels reject values longer than 63
characters. A hex-encoded SHA-256 digest is 64 characters by itself -- before
even adding a ``<version>-`` prefix -- so it cannot be used as an ACA label.
Every digest-derived token in :mod:`azure_functions_agents.session_state`
(the ``a1-`` app hash, ``o1-`` owner hash, and ``s1-`` state-store fingerprint)
therefore uses the SAME canonical encoding instead: the full SHA-256 digest
(32 bytes / 256 bits), base32-encoded per RFC 4648, lower-cased, and stripped
of ``=`` padding.

That always yields exactly :data:`LABEL_SAFE_PAYLOAD_LENGTH` (52) characters
for a 32-byte digest -- ``ceil(256 / 5) == 52`` -- so every
``<version>-<payload>`` token is 55 characters total, comfortably inside
ACA's 63-character label limit while preserving the full 256 bits of digest
entropy (unlike truncating a hex digest, which would throw entropy away).

Only the canonical lower-case ``[a-z2-7]{52}`` form is ever accepted.
Uppercase output, ``=`` padding, and any other alphabet/length are rejected by
:data:`LABEL_SAFE_PAYLOAD_PATTERN` -- there is exactly one accepted canonical
form, so verification never has to normalize case or padding first.
"""

from __future__ import annotations

import base64
import re

LABEL_SAFE_PAYLOAD_LENGTH = 52
LABEL_SAFE_PAYLOAD_CHARS = "a-z2-7"
LABEL_SAFE_PAYLOAD_GROUP = rf"[{LABEL_SAFE_PAYLOAD_CHARS}]{{{LABEL_SAFE_PAYLOAD_LENGTH}}}"
LABEL_SAFE_PAYLOAD_PATTERN = re.compile(rf"^{LABEL_SAFE_PAYLOAD_GROUP}$")


def encode_label_safe_digest(digest: bytes) -> str:
    """Encode a digest as lower-case, unpadded RFC 4648 base32.

    For a 32-byte SHA-256 digest this always returns
    :data:`LABEL_SAFE_PAYLOAD_LENGTH` characters matching
    :data:`LABEL_SAFE_PAYLOAD_PATTERN`. Callers hashing anything other than a
    full SHA-256 digest are responsible for validating the length they need;
    this function itself only performs the encoding.
    """
    return base64.b32encode(digest).decode("ascii").rstrip("=").lower()
