from __future__ import annotations

from typing import Any, ClassVar

from agent_framework import ChatResponse, Message

from azure_functions_agents import ClientManager, create_function_app, set_client_manager


class _DeterministicChatClient:
    additional_properties: ClassVar[dict[str, Any]] = {}

    def get_response(self, messages: Any, *, stream: bool = False, **kwargs: Any) -> Any:
        del kwargs
        if stream:
            raise RuntimeError("The A2A simple E2E must use the non-streaming runner.")
        prompt = messages[-1].text

        async def _response() -> ChatResponse:
            return ChatResponse(
                messages=[
                    Message(
                        "assistant",
                        [
                            (
                                f"Triage received: {prompt} "
                                "Immediate action: pause the latest rollout."
                            )
                        ],
                    )
                ]
            )

        return _response()


class _DeterministicClientManager(ClientManager):
    def resolve_model(self, requested: str | None) -> str:
        return requested or "deterministic-e2e"

    def build_chat_client(self, model: str | None) -> Any:
        del model
        return _DeterministicChatClient()


set_client_manager(_DeterministicClientManager())
app = create_function_app()
