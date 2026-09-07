"""Hybrid-only Azure OpenAI-compatible APIM client."""

from __future__ import annotations

import os
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from agent_framework import ChatContext, ChatMiddleware

from .._credential import build_async_credential
from ..client_manager import ClientManager, InferenceTarget
from .hybrid_config import (
    HYBRID_APIM_KEY_HEADER,
    HYBRID_APIM_MODEL_ENV,
    resolve_hybrid_apim_settings,
)
from .hybrid_observability import (
    HybridMetric,
    record_hybrid_count,
    record_hybrid_duration,
)

_DEFAULT_MODEL = "gpt-4.1-mini"
_DUMMY_OPENAI_KEY = "apim-managed"


class HybridModelTimingMiddleware(ChatMiddleware):
    """Record APIM model latency without observing request or response bodies."""

    async def process(
        self,
        context: ChatContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        started = time.perf_counter()
        record_hybrid_count(HybridMetric.MODEL_CALLS)
        if not context.stream:
            try:
                await call_next()
            except BaseException:
                record_hybrid_count(HybridMetric.MODEL_FAILURES)
                raise
            finally:
                record_hybrid_duration(HybridMetric.MODEL_DURATION, started)
            return

        observed_first_token = False

        async def observe_first_token(update: Any) -> Any:
            nonlocal observed_first_token
            if not observed_first_token:
                observed_first_token = True
                record_hybrid_duration(HybridMetric.STREAM_TTFT, started)
            return update

        async def observe_stream_end() -> None:
            record_hybrid_duration(HybridMetric.MODEL_DURATION, started)

        context.stream_transform_hooks.append(observe_first_token)
        context.stream_cleanup_hooks.append(observe_stream_end)
        try:
            await call_next()
        except BaseException:
            record_hybrid_count(HybridMetric.MODEL_FAILURES)
            record_hybrid_duration(HybridMetric.MODEL_DURATION, started)
            raise


class HybridApimClientManager(ClientManager):
    """Build the experimental Responses client routed only through APIM."""

    name = "hybrid_apim"

    def __init__(
        self,
        *,
        base_url: str,
        audience: str | None,
        subscription_key: str | None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._base_url = base_url
        self._audience = audience
        self._subscription_key = subscription_key
        self._environment = environment
        self._credential: Any | None = None

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> HybridApimClientManager:
        base_url, audience, subscription_key = resolve_hybrid_apim_settings(environment)
        return cls(
            base_url=base_url,
            audience=audience,
            subscription_key=subscription_key,
            environment=environment,
        )

    def resolve_model(self, requested: str | None) -> str:
        if requested:
            return requested
        source = os.environ if self._environment is None else self._environment
        configured = source.get(HYBRID_APIM_MODEL_ENV, "").strip()
        if not configured:
            configured = source.get("AZURE_FUNCTIONS_AGENTS_MODEL", "").strip()
        if configured:
            return configured
        return _DEFAULT_MODEL

    def build_chat_client(self, model: str | None) -> Any:
        client, _ = self.build_chat_client_with_target(model)
        return client

    def build_chat_client_with_target(
        self, model: str | None
    ) -> tuple[Any, InferenceTarget]:
        from agent_framework.openai import OpenAIChatClient
        from openai import AsyncOpenAI

        resolved_model = self.resolve_model(model)
        headers: dict[str, str] = {}
        api_key: str | Callable[[], Awaitable[str]] = _DUMMY_OPENAI_KEY
        if self._subscription_key is not None:
            headers[HYBRID_APIM_KEY_HEADER] = self._subscription_key
        else:
            api_key = self._token_provider()
        sdk = AsyncOpenAI(
            api_key=api_key,
            base_url=self._base_url,
            default_headers=headers,
            max_retries=0,
        )
        client = OpenAIChatClient(
            model=resolved_model,
            async_client=sdk,
            middleware=[HybridModelTimingMiddleware()],
        )
        return client, self.resolve_inference_target(resolved_model)

    def resolve_inference_target(self, model: str | None) -> InferenceTarget:
        resolved_model = self.resolve_model(model)
        return InferenceTarget(
            provider="azure_openai_apim",
            model=resolved_model,
            endpoint=self._base_url.rstrip("/"),
            api_version="responses-v1",
        )

    def _token_provider(self) -> Callable[[], Awaitable[str]]:
        audience = self._audience
        if audience is None:
            raise RuntimeError("Hybrid APIM managed-identity audience is unavailable.")
        if self._credential is None:
            self._credential = build_async_credential()
        credential = self._credential
        scope = audience if audience.endswith("/.default") else f"{audience}/.default"

        async def get_token() -> str:
            token = await credential.get_token(scope)
            if not token.token:
                raise RuntimeError("Hybrid APIM credential returned no access token.")
            return token.token

        return get_token

    @property
    def model_base_url(self) -> str:
        """Return the validated APIM OpenAI-compatible base URL."""
        return self._base_url

    async def request_headers(
        self,
        extra: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Build one APIM request's caller headers without logging credentials."""
        headers = dict(extra or {})
        if self._subscription_key is not None:
            headers[HYBRID_APIM_KEY_HEADER] = self._subscription_key
            return headers
        token = await self._token_provider()()
        headers["Authorization"] = f"Bearer {token}"
        return headers

    async def close(self) -> None:
        if self._credential is None:
            return
        await self._credential.close()
        self._credential = None
