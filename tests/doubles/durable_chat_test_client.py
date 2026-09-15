"""A held-final MAF transport for native durable-chat end-to-end tests."""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Mapping, Sequence
from typing import Any

from agent_framework import (
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    FunctionInvocationLayer,
    Message,
    ResponseStream,
    UsageDetails,
)

from azure_functions_agents.client_manager import ClientManager

_NONCE = re.compile(r"\bE2E:([0-9a-f]{32})\b")
_channels: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Event]] = {}
_channel_lock = threading.Lock()


def release_test_response(nonce: str) -> bool:
    with _channel_lock:
        channel = _channels.get(nonce)
    if channel is None:
        return False
    loop, release = channel
    loop.call_soon_threadsafe(release.set)
    return True


class DurableChatTestClientManager(ClientManager):
    name = "openai"

    def resolve_model(self, requested: str | None) -> str:
        return requested or "durable-chat-e2e"

    def build_chat_client(self, model: str | None) -> _HeldFinalChatClient:
        return _HeldFinalChatClient()


class _HeldFinalChatClient(FunctionInvocationLayer[Any], BaseChatClient[Any]):
    def __init__(self) -> None:
        super().__init__(function_invocation_configuration={"enabled": True})

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> ResponseStream[ChatResponseUpdate, ChatResponse[Any]]:
        user_index = max(index for index, message in enumerate(messages) if message.role == "user")
        prompt = messages[user_index].text
        matched = _NONCE.search(prompt)
        if matched is None:
            raise AssertionError("The local E2E transport requires an explicit test nonce")
        nonce = matched.group(1)
        tool_results = sum(
            content.type == "function_result"
            for message in messages[user_index + 1 :]
            for content in message.contents
        )
        tool_call = _next_test_tool_call(prompt, nonce, tool_results)

        if tool_call is not None:
            async def tool_updates():
                yield ChatResponseUpdate(
                    contents=[tool_call],
                    role="assistant",
                    finish_reason="tool_calls",
                )
            return ResponseStream(tool_updates(), finalizer=ChatResponse.from_updates)

        release = asyncio.Event()
        with _channel_lock:
            _channels[nonce] = (asyncio.get_running_loop(), release)

        async def updates():
            try:
                yield ChatResponseUpdate(
                    contents=[Content.from_text("Durable ")],
                    role="assistant",
                )
                yield ChatResponseUpdate(contents=[Content.from_text("answer \u03bb")])
                async with asyncio.timeout(120):
                    await release.wait()
                yield ChatResponseUpdate(
                    contents=[
                        Content.from_text(f" for {nonce}."),
                        Content.from_usage(
                            UsageDetails(
                                input_token_count=7,
                                output_token_count=8,
                                total_token_count=15,
                            )
                        ),
                    ],
                    finish_reason="stop",
                )
            finally:
                with _channel_lock:
                    _channels.pop(nonce, None)

        return ResponseStream(updates(), finalizer=ChatResponse.from_updates)


def _next_test_tool_call(prompt: str, nonce: str, tool_results: int) -> Content | None:
    if "E2E-TOOLS" in prompt and tool_results < 2:
        return Content.from_function_call(
            f"call-{nonce}-{tool_results}",
            "append_note",
            arguments={"nonce": nonce, "step": tool_results},
        )
    if "E2E-HUMAN" in prompt and tool_results == 0:
        return Content.from_function_call(
            f"human-{nonce}",
            "request_human_input",
            arguments={
                "question": "Approve the local E2E continuation?",
                "choices": ["Proceed", "Stop"],
                "allow_free_text": False,
            },
        )
    return None
