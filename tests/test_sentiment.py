"""Tests for draft sentiment classification."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from azure_functions_agents import sentiment


@pytest.fixture(autouse=True)
def _clear_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(sentiment.API_KEY_ENV, raising=False)


def test_sentiment_disabled_without_api_key() -> None:
    assert sentiment.sentiment_enabled() is False


def test_sentiment_disabled_for_blank_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sentiment.API_KEY_ENV, "   ")
    assert sentiment.sentiment_enabled() is False


def test_sentiment_enabled_with_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sentiment.API_KEY_ENV, "key")
    assert sentiment.sentiment_enabled() is True


@pytest.mark.asyncio
async def test_classify_draft_is_neutral_without_api_key() -> None:
    assert await sentiment.classify_draft("This is broken again") == sentiment.NEUTRAL_RESULT


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", ["", "   "])
async def test_classify_draft_is_neutral_for_empty_draft(
    monkeypatch: pytest.MonkeyPatch, draft: str
) -> None:
    monkeypatch.setenv(sentiment.API_KEY_ENV, "key")
    assert await sentiment.classify_draft(draft) == sentiment.NEUTRAL_RESULT


@pytest.mark.asyncio
async def test_classify_draft_is_neutral_without_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sentiment.API_KEY_ENV, "key")
    monkeypatch.setitem(sys.modules, "typesafe_sdk", None)
    assert await sentiment.classify_draft("This is broken again") == sentiment.NEUTRAL_RESULT


class _Answer:
    def __init__(self, choice: Any, confidence: Any) -> None:
        self.choice = choice
        self.confidence = confidence


class _Response:
    def __init__(self, answer: _Answer) -> None:
        self.choices = {"sentiment": answer}


def _install_fake_sdk(
    monkeypatch: pytest.MonkeyPatch,
    *,
    answer: _Answer | None = None,
    error: Exception | None = None,
    recorder: dict[str, Any] | None = None,
) -> None:
    """Install a stand-in for ``typesafe_sdk`` so no network call is made."""

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            if recorder is not None:
                recorder["client_kwargs"] = kwargs

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_exc: Any) -> None:
            return None

        async def system_one(self, state: Any, questions: Any) -> _Response:
            if recorder is not None:
                recorder["state"] = state
                recorder["questions"] = questions
            if error is not None:
                raise error
            assert answer is not None
            return _Response(answer)

    class FakeChoice:
        def __init__(self, *, instructions: str, criteria: Any) -> None:
            self.instructions = instructions
            self.criteria = criteria

    class FakeRetryPolicy:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    module = types.SimpleNamespace(
        AsyncTypeSafeClient=FakeClient,
        Choice=FakeChoice,
        RetryPolicy=FakeRetryPolicy,
    )
    monkeypatch.setitem(sys.modules, "typesafe_sdk", module)


@pytest.mark.asyncio
async def test_classify_draft_returns_the_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sentiment.API_KEY_ENV, "key")
    recorder: dict[str, Any] = {}
    _install_fake_sdk(monkeypatch, answer=_Answer("annoyed", 0.91), recorder=recorder)

    result = await sentiment.classify_draft("  Why does this keep failing?  ")

    assert result == {"sentiment": "annoyed", "confidence": 0.91}
    # The draft is trimmed before it leaves the process.
    assert recorder["state"] == "Why does this keep failing?"
    assert set(recorder["questions"]["sentiment"].criteria) == set(sentiment.SENTIMENTS)


@pytest.mark.asyncio
async def test_classify_draft_truncates_a_long_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sentiment.API_KEY_ENV, "key")
    recorder: dict[str, Any] = {}
    _install_fake_sdk(monkeypatch, answer=_Answer("neutral", 0.5), recorder=recorder)

    await sentiment.classify_draft("a" * 5000)

    assert len(recorder["state"]) == 2000


@pytest.mark.asyncio
async def test_classify_draft_is_neutral_when_the_call_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(sentiment.API_KEY_ENV, "key")
    _install_fake_sdk(monkeypatch, error=RuntimeError("boom"))

    assert await sentiment.classify_draft("This is broken again") == sentiment.NEUTRAL_RESULT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "choice,confidence",
    [
        ("delighted", 0.9),  # not one of the agreed values
        (None, 0.9),
        ("positive", None),
        ("positive", "high"),
        ("positive", 1.5),
        ("positive", -0.1),
    ],
)
async def test_classify_draft_rejects_an_answer_outside_the_contract(
    monkeypatch: pytest.MonkeyPatch, choice: Any, confidence: Any
) -> None:
    monkeypatch.setenv(sentiment.API_KEY_ENV, "key")
    _install_fake_sdk(monkeypatch, answer=_Answer(choice, confidence))

    assert await sentiment.classify_draft("This is broken again") == sentiment.NEUTRAL_RESULT


def test_sentiments_match_the_chat_ui_contract() -> None:
    """The chat UI drops any value it does not know."""

    from pathlib import Path

    source = (
        Path(sentiment.__file__).parent / "public" / "assets" / "assistant-avatar.js"
    ).read_text(encoding="utf-8")

    for value in sentiment.SENTIMENTS:
        assert f'"{value}"' in source
