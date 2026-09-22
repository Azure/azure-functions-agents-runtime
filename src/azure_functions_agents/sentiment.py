"""Draft sentiment classification for the built-in chat avatar.

The built-in chat page shows an assistant avatar that reacts while the user
types. This module turns a draft message into one bounded sentiment value.

It uses the TypeSafe System One API (the Jev model), which answers a typed
question instead of generating text. That keeps the call small, fast, and
cheap enough to run while the user types.

The module is optional. It stays inert unless ``TYPESAFE_API_KEY`` is set and
``typesafe-sdk`` is installed. A failure always degrades to ``neutral``,
because the avatar must never block or break the chat.
"""

from __future__ import annotations

import os
from typing import Any, Final

from ._logger import logger

API_KEY_ENV: Final = "TYPESAFE_API_KEY"

#: The values the chat UI understands. They match ``ASSISTANT_EXPRESSIONS``
#: in ``public/assets/assistant-avatar.js``.
SENTIMENTS: Final = ("neutral", "positive", "concerned", "annoyed")

NEUTRAL_RESULT: Final[dict[str, Any]] = {"sentiment": "neutral", "confidence": 0.0}

#: The draft is a partial message, so the question asks about the writer, not
#: about a finished document.
_INSTRUCTIONS: Final = (
    "The text is a message the user is still typing to an AI assistant. "
    "What mood does the user show? "
    "Answer 'positive' when the user sounds pleased, friendly, or excited; "
    "'concerned' when the user sounds worried, confused, or reports a problem; "
    "'annoyed' when the user sounds frustrated, impatient, or critical; "
    "'neutral' for a plain request or question."
)

#: A draft changes on every keystroke, so a slow answer is a useless answer.
_TIMEOUT_SECONDS: Final = 2.0
_MAX_DRAFT_CHARS: Final = 2000


def sentiment_enabled() -> bool:
    """Report whether draft sentiment can run in this environment."""

    return bool(os.environ.get(API_KEY_ENV, "").strip())


async def classify_draft(draft: str) -> dict[str, Any]:
    """Classify one draft message.

    Returns a mapping with a ``sentiment`` from :data:`SENTIMENTS` and a
    ``confidence`` between 0 and 1. Any problem returns the neutral result,
    so the caller never has to handle an error.
    """

    text = (draft or "").strip()[:_MAX_DRAFT_CHARS]
    if not text or not sentiment_enabled():
        return NEUTRAL_RESULT

    try:
        from typesafe_sdk import AsyncTypeSafeClient, Choice, RetryPolicy
    except ImportError:
        logger.warning(
            "Draft sentiment is configured but typesafe-sdk is not installed. "
            "Install the 'jev' extra to enable it."
        )
        return NEUTRAL_RESULT

    try:
        async with AsyncTypeSafeClient(
            retry=RetryPolicy(max_retries=1, timeout=_TIMEOUT_SECONDS)
        ) as client:
            response = await client.system_one(
                text,
                {
                    "sentiment": Choice(
                        instructions=_INSTRUCTIONS,
                        criteria=dict.fromkeys(SENTIMENTS),
                    )
                },
            )
        answer = response.choices["sentiment"]
    # The avatar must never break chat, so every failure degrades to neutral.
    except Exception as exc:
        logger.debug("Draft sentiment classification failed: %s", exc)
        return NEUTRAL_RESULT

    return _normalize(getattr(answer, "choice", None), getattr(answer, "confidence", None))


def _normalize(choice: Any, confidence: Any) -> dict[str, Any]:
    """Keep the response inside the contract the chat UI expects."""

    if choice not in SENTIMENTS:
        return NEUTRAL_RESULT

    try:
        value = float(confidence)
    except (TypeError, ValueError):
        return NEUTRAL_RESULT

    if not 0.0 <= value <= 1.0:
        return NEUTRAL_RESULT

    return {"sentiment": choice, "confidence": value}
