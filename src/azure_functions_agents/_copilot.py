"""Pinned, lazy Copilot SDK adapter for the explicitly limited local preview."""

from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import os
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

import httpx

from ._copilot_state import NativeState
from ._credential import build_async_credential
from ._harness import (
    FLAG,
    PROTOCOL_VERSION,
    RUNTIME_VERSION,
    SDK_VERSION,
    AppHarness,
    CopilotPreviewError,
    HarnessRequest,
    check_sdk_dependency,
    get_harness,
)
from ._logger import logger
from .client_manager import InferenceTarget

if TYPE_CHECKING:
    from azure.core.credentials_async import AsyncTokenCredential
    from copilot import CopilotClient
    from copilot.copilot_request_handler import CopilotRequestContext, CopilotRequestHandler
    from copilot.generated.rpc import PermissionDecision
    from copilot.session import (
        CopilotSession,
        InfiniteSessionConfig,
        PermissionInvocation,
        ProviderConfig,
        ProviderTokenArgs,
        SystemMessageConfig,
        ToolSearchConfig,
    )
    from copilot.session_events import PermissionRequest, SessionEvent
    from copilot.tools import Tool, ToolInvocation, ToolResult

    from ._function_tool import FunctionTool
    from .runner import AgentResult


class _SessionOptions(TypedDict):
    model: str
    tools: list[Tool]
    available_tools: list[str]
    system_message: SystemMessageConfig
    provider: ProviderConfig
    streaming: bool
    enable_config_discovery: bool
    enable_session_telemetry: bool
    request_extensions: bool
    infinite_sessions: InfiniteSessionConfig
    tool_search: ToolSearchConfig
    on_event: Callable[[SessionEvent], None]


def _check_pair() -> None:
    check_sdk_dependency()
    from copilot._cli_version import CLI_VERSION
    from copilot._sdk_protocol_version import SDK_PROTOCOL_VERSION

    if CLI_VERSION != RUNTIME_VERSION or SDK_PROTOCOL_VERSION != PROTOCOL_VERSION:
        raise CopilotPreviewError("The installed Copilot SDK/native runtime pair is incompatible.")


def _runtime_path() -> Path:
    _check_pair()
    from copilot._cli_download import get_cache_dir
    from copilot._cli_version import get_runtime_platform

    directory = get_cache_dir(RUNTIME_VERSION) / "prebuilds" / get_runtime_platform()
    wrapper = directory / ("copilot-runtime.exe" if os.name == "nt" else "copilot-runtime")
    if not all(
        path.is_file() and path.stat().st_size
        for path in (wrapper, directory / "runtime.node", directory / ".hostless-runtime-assets-v2")
    ):
        raise CopilotPreviewError(
            "Pinned Copilot native runtime is not installed. With the preview flag enabled, "
            "run: python -m azure_functions_agents._copilot --setup. "
            "Requests never download a runtime."
        )
    return wrapper


def _native_environment(root: Path) -> dict[str, str]:
    allowed = {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "LANG", "LC_ALL"}
    env = {name: value for name, value in os.environ.items() if name.upper() in allowed}
    env.update(
        {
            "HOME": str(root),
            "USERPROFILE": str(root),
            "APPDATA": str(root / "roaming"),
            "LOCALAPPDATA": str(root / "local"),
            "COPILOT_OTEL_ENABLED": "false",
            "OTEL_SDK_DISABLED": "true",
        }
    )
    return env


@dataclass(frozen=True)
class _InferencePolicy:
    url: str
    max_output_tokens: int | None


def _request_handler(owner: _NativeRuntime) -> CopilotRequestHandler:
    from copilot.copilot_request_handler import CopilotRequestHandler

    class PreviewRequests(CopilotRequestHandler):
        async def send_request(
            self, request: httpx.Request, context: CopilotRequestContext
        ) -> httpx.Response:
            policy = owner._inference.get(context.session_id or "")
            if policy is None or str(request.url) != policy.url or request.method != "POST":
                raise CopilotPreviewError("Copilot requested inference outside the active provider target.")
            payload = json.loads(await request.aread())
            if not isinstance(payload, dict):
                raise CopilotPreviewError("Copilot produced an invalid inference request.")
            payload["store"] = False
            if policy.max_output_tokens is not None:
                if request.url.path.endswith("/responses"):
                    payload["max_output_tokens"] = policy.max_output_tokens
                else:
                    payload.pop("max_tokens", None)
                    payload["max_completion_tokens"] = policy.max_output_tokens
            headers = dict(request.headers)
            headers.pop("content-length", None)
            bounded = httpx.Request(
                request.method, request.url, headers=headers, json=payload, extensions=request.extensions
            )
            return await owner._forward_http(bounded, context)

    return PreviewRequests()


class _NativeRuntime:
    """One client/stdio process and one local writer lease per app worker."""

    def __init__(self, root: Path) -> None:
        self.state = NativeState(root)
        self._client: CopilotClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._start_lock = asyncio.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._credential: AsyncTokenCredential | None = None
        self._http: httpx.AsyncClient | None = None
        self._inference: dict[str, _InferencePolicy] = {}
        atexit.register(self._exit)

    async def client(self) -> CopilotClient:
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise CopilotPreviewError(
                "Copilot preview requires one event loop per worker. "
                "Await shutdown_client_manager() before closing a standalone event loop."
            )
        self._loop = loop
        async with self._start_lock:
            if self._client is not None:
                return self._client
            path = _runtime_path()
            self.state.claim()
            self.state.native_root.mkdir(parents=True, exist_ok=True)
            from copilot import CopilotClient, RuntimeConnection

            client = CopilotClient(
                connection=RuntimeConnection.for_stdio(path=str(path)),
                mode="empty",
                base_directory=str(self.state.native_root),
                working_directory=str(self.state.native_root),
                env=_native_environment(self.state.native_root),
                use_logged_in_user=False,
                log_level="none",
                telemetry=None,
                request_handler=_request_handler(self),
            )
            try:
                await client.start()
                process = getattr(client, "_cli_process", None)
                if isinstance(process, subprocess.Popen):
                    self._process = process
                status = await client.get_status()
                if status.version != RUNTIME_VERSION or status.protocol_version != PROTOCOL_VERSION:
                    raise CopilotPreviewError("The running Copilot native version is incompatible.")
            except BaseException:
                process = getattr(client, "_cli_process", None)
                if isinstance(process, subprocess.Popen):
                    self._process = process
                try:
                    await asyncio.wait_for(client.stop(), timeout=5)
                finally:
                    self._exit()
                raise
            self._client = client
            logger.info(
                "Agent harness ready: harness=copilot sdk=%s runtime=%s protocol=%s "
                "transport=stdio",
                SDK_VERSION,
                RUNTIME_VERSION,
                PROTOCOL_VERSION,
            )
            return client

    async def close(self) -> None:
        try:
            if self._client is not None:
                await asyncio.wait_for(self._client.stop(), timeout=10)
        finally:
            self._client = None
            self._loop = None
            self._exit()
            atexit.unregister(self._exit)
            if self._credential is not None:
                await self._credential.close()
                self._credential = None
            if self._http is not None:
                await self._http.aclose()
                self._http = None

    @contextmanager
    def inference_turn(
        self, native_id: str, harness: AppHarness, max_output_tokens: int | None
    ) -> Iterator[None]:
        url = (
            f"{harness.endpoint}/openai/v1/responses"
            if harness.provider == "foundry"
            else "https://api.openai.com/v1/chat/completions"
        )
        self._inference[native_id] = _InferencePolicy(url, max_output_tokens)
        try:
            yield
        finally:
            self._inference.pop(native_id, None)

    async def _forward_http(
        self, request: httpx.Request, _context: CopilotRequestContext
    ) -> httpx.Response:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=None, follow_redirects=False)
        return await self._http.send(request, stream=True)

    async def foundry_token(self, _args: ProviderTokenArgs) -> str:
        if self._credential is None:
            self._credential = build_async_credential()
        try:
            token = await self._credential.get_token("https://ai.azure.com/.default")
        except Exception:
            raise CopilotPreviewError(
                "Copilot Foundry preview could not acquire an Entra token. "
                "Check the existing Azure sign-in and project access."
            ) from None
        return token.token

    def _exit(self) -> None:
        # Hold the Popen handle, never a bare PID that could have been reused.
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            except OSError:
                logger.error("Copilot native process cleanup failed.")
        self._process = None
        self.state.close()


_RUNTIMES: dict[Path, _NativeRuntime] = {}


def _runtime(harness: AppHarness) -> _NativeRuntime:
    if harness.name != "copilot" or harness.storage_root is None:
        raise CopilotPreviewError("Copilot was not selected for this app.")
    owner = _RUNTIMES.get(harness.storage_root)
    if owner is None:
        owner = _NativeRuntime(harness.storage_root)
        _RUNTIMES[harness.storage_root] = owner
    return owner


async def shutdown() -> None:
    for owner in list(_RUNTIMES.values()):
        await owner.close()
    _RUNTIMES.clear()


def _token(_args: ProviderTokenArgs) -> str:
    token = os.environ.get("OPENAI_API_KEY", "").strip()
    if not token:
        raise CopilotPreviewError("Copilot OpenAI preview requires OPENAI_API_KEY in the host environment.")
    return token


def _deny_permission(
    _request: PermissionRequest, _invocation: PermissionInvocation
) -> PermissionDecision:
    from copilot.generated.rpc import PermissionDecisionDeniedByRules

    return PermissionDecisionDeniedByRules(rules=[])


def _tool(function: FunctionTool, calls: list[dict[str, Any]]) -> Tool:
    from copilot.tools import Tool, ToolResult

    async def invoke(invocation: ToolInvocation) -> ToolResult:
        record: dict[str, Any] = {
            "type": "tool_start",
            "tool_call_id": invocation.tool_call_id,
            "tool_name": function.name,
            "arguments": invocation.arguments,
        }
        calls.append(record)
        try:
            contents = await function.invoke(
                arguments=invocation.arguments,
                tool_call_id=invocation.tool_call_id,
            )
            if not isinstance(contents, list) or any(
                getattr(item, "type", None) != "text" for item in contents
            ):
                raise CopilotPreviewError("Copilot preview tools must return text or JSON.")
            text = "\n".join(item.text or "" for item in contents)
        except Exception:
            logger.warning("Copilot custom tool failed: tool=%s", function.name)
            text = '{"error":"Custom tool failed or returned unsupported content."}'
            record["result"] = text
            return ToolResult(text_result_for_llm=text, result_type="failure")
        record["result"] = text
        return ToolResult(text_result_for_llm=text, result_type="success")

    return Tool(
        name=function.name,
        description=function.description,
        parameters=function.parameters(),
        handler=invoke,
        skip_permission=True,
        defer="never",
    )


async def _release(session: CopilotSession, *, abort: bool) -> None:
    try:
        if abort:
            await asyncio.wait_for(session.abort(), timeout=5)
    finally:
        await asyncio.wait_for(session.disconnect(), timeout=5)


async def run(harness: AppHarness, request: HarnessRequest) -> AgentResult:
    from copilot.session_events import AssistantMessageData, AssistantUsageData

    from .runner import AgentResult, _AgentUsageRecorder, _session_lock_bounded_by

    owner = _runtime(harness)
    native_id = owner.state.native_id(request.agent_slug, request.session_id)
    session: CopilotSession | None = None
    calls: list[dict[str, Any]] = []
    messages: list[str] = []
    usage: dict[str, int] = {}
    recorder = _AgentUsageRecorder(
        agent_name=request.agent_slug,
        execution_role="primary",
        inference_target=InferenceTarget(harness.provider, request.model),
    )
    invocation_started = False
    state_started = False

    def on_event(event: SessionEvent) -> None:
        data = event.data
        if isinstance(data, AssistantMessageData) and data.content:
            messages.append(data.content)
        elif isinstance(data, AssistantUsageData):
            for key, count in (
                ("input_token_count", data.input_tokens),
                ("output_token_count", data.output_tokens),
            ):
                if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                    usage[key] = usage.get(key, 0) + count

    try:
        async with _session_lock_bounded_by(
            native_id, request.deadline, agent_slug=request.agent_slug
        ), asyncio.timeout_at(request.deadline):
            with owner.inference_turn(native_id, harness, request.max_output_tokens):
                token_args: ProviderTokenArgs = {"provider_name": "", "session_id": native_id}
                if harness.provider == "foundry":
                    await owner.foundry_token(token_args)
                else:
                    _token(token_args)
                # Validate persisted state before native startup; absence is never a new session.
                owner.state.validate(native_id, new_session=request.new_session)
                client = await owner.client()
                owner.state.begin(native_id, new_session=request.new_session)
                state_started = True
                provider: ProviderConfig = {
                    "type": "openai",
                    "wire_api": "completions",
                    "base_url": "https://api.openai.com/v1",
                    "bearer_token_provider": _token,
                }
                if harness.provider == "foundry":
                    provider = {
                        "type": "openai",
                        "wire_api": "responses",
                        "base_url": f"{harness.endpoint}/openai/v1",
                        "bearer_token_provider": owner.foundry_token,
                    }
                if request.max_output_tokens is not None:
                    provider["max_output_tokens"] = request.max_output_tokens
                tools = [_tool(function, calls) for function in request.tools]
                options: _SessionOptions = {
                    "model": request.model,
                    "tools": tools,
                    "available_tools": [f"custom:{function.name}" for function in request.tools],
                    "system_message": {"mode": "replace", "content": request.instructions or ""},
                    "provider": provider,
                    "streaming": False,
                    "enable_config_discovery": False,
                    "enable_session_telemetry": False,
                    "request_extensions": False,
                    "infinite_sessions": {"enabled": False},
                    "tool_search": {"enabled": False},
                    "on_event": on_event,
                }
                if request.new_session:
                    session = await client.create_session(
                        session_id=native_id, on_permission_request=_deny_permission, **options
                    )
                else:
                    session = await client.resume_session(
                        native_id,
                        on_permission_request=_deny_permission,
                        continue_pending_work=False,
                        **options,
                    )
                metadata = await session.rpc.tools.get_current_metadata()
                if metadata.tools is None:
                    raise CopilotPreviewError("Copilot did not report its model-visible tool catalog.")
                actual_names = sorted(item.name for item in metadata.tools)
                expected_names = sorted(function.name for function in request.tools)
                if actual_names != expected_names:
                    raise CopilotPreviewError(
                        "Copilot model-visible tool catalog differs from the configured custom tools. "
                        "No prompt was sent."
                    )
                logger.info("Copilot tool catalog verified: custom_tool_count=%d", len(expected_names))
                logger.info("Copilot request target: provider=%s model=%s", harness.provider, request.model)
                invocation_started = True
                response = await session.send_and_wait(
                    request.prompt,
                    timeout=max(0.0, request.deadline - asyncio.get_running_loop().time()),
                )
                if (
                    response is None
                    or not isinstance(response.data, AssistantMessageData)
                    or not response.data.content.strip()
                ):
                    raise CopilotPreviewError("Copilot returned no final model reply.")
                content = response.data.content
                await _release(session, abort=False)
                session = None
                owner.state.complete(native_id)
            return AgentResult(
                session_id=request.session_id,
                content=content,
                content_intermediate=messages[:-1],
                tool_calls=calls,
            )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        raise CopilotPreviewError(
            "Copilot preview request timed out. This session has an unfinished turn."
            if state_started
            else "Copilot preview timed out before starting this turn. "
            "This request did not mark the session unfinished; retry after the active turn completes."
        ) from None
    except CopilotPreviewError:
        raise
    except Exception:
        logger.error("Copilot preview execution failed; native details were not logged.")
        raise CopilotPreviewError(
            "Copilot preview failed. Check the pinned runtime, provider authentication and "
            "local storage. This conversation cannot be silently restarted."
        ) from None
    finally:
        if invocation_started:
            recorder.emit(usage)
        if session is not None:
            try:
                await asyncio.shield(_release(session, abort=True))
                if not invocation_started:
                    owner.state.complete(native_id)
            except Exception:
                logger.error("Copilot session cleanup failed; unfinished state remains blocked.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up or inspect the pinned local Copilot preview.")
    parser.add_argument("--setup", action="store_true", help="Download the exact verified native bundle.")
    args = parser.parse_args()
    if get_harness().name != "copilot":
        parser.error(f"Set {FLAG}=true before invoking preview setup or diagnostics.")
    _check_pair()
    if args.setup:
        from copilot._cli_download import ensure_runtime_wrapper

        ensure_runtime_wrapper(RUNTIME_VERSION)
    _runtime_path()
    print(
        f"harness=copilot sdk={SDK_VERSION} runtime={RUNTIME_VERSION} "
        f"protocol={PROTOCOL_VERSION} transport=stdio provider={get_harness().provider} (not authenticated)"
    )


if __name__ == "__main__":
    main()
