"""
ACA Dynamic Sessions sandbox — execute_python tool.

Provides an ``execute_python`` tool backed by Azure Container Apps dynamic
sessions (code-interpreter pools). Configured via the ``execution_sandbox``
block in agent frontmatter.

Each agent can have its own session pool endpoint. The ACA session id is
usually derived from the runtime's ``session_id`` (passed in by the runner via
``fallback_session_id``) so REPL state — variables, imports, files, browser
pages — persists across calls within a conversation. When no session id is
available, a fresh GUID is generated for that tool instance.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import aiohttp
from azure.identity.aio import get_bearer_token_provider
from pydantic import BaseModel, Field

from .._credential import build_async_credential
from .._function_tool import FunctionTool, tool
from .._logger import logger
from ..config.env import has_unresolved_placeholders, substitute_env_vars_in_value

if TYPE_CHECKING:
    from azure.identity.aio import DefaultAzureCredential

_API_VERSION = "2025-10-02-preview"

# ---------------------------------------------------------------------------
# Playwright helper that is pre-loaded into every sandbox session
# ---------------------------------------------------------------------------

_ACA_SESSION_SETUP = """
async def launch_browser(width=1280, height=800):
    from playwright.async_api import async_playwright
    p = await async_playwright().start()
    browser = await p.chromium.launch(
        headless=True,
        args=[
            f'--window-size={width},{height}',
            '--disable-blink-features=AutomationControlled',
            '--disable-extensions',
        ],
    )
    context = await browser.new_context(
        user_agent=(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/131.0.0.0 Safari/537.36'
        ),
        viewport={'width': width, 'height': height},
    )
    page = await context.new_page()
    return page
"""

# ---------------------------------------------------------------------------
# Tool description
# ---------------------------------------------------------------------------

_EXECUTE_PYTHON_DESCRIPTION = (
    "Execute Python code in a persistent sandboxed REPL backed by a"
    " Jupyter kernel. Returns JSON with result, stdout, and stderr.\n"
    "\n"
    "IMPORTANT: This runs in an ISOLATED SANDBOX with its own file system."
    "\n"
    "Only use this tool when you need to actually run code,"
    " when no other tool can accomplish the task (there's a small cost to using it) —"
    " computation, data processing, web browsing, etc."
    " Do NOT call this tool just to print text, format output, or display"
    " results you already have. Respond directly with text instead.\n"
    "\n"
    "Key behaviors:\n"
    "- State persists across calls: variables, imports, and files"
    " (/mnt/data/) are retained between invocations.\n"
    "- The last expression value is returned in 'result' (like a"
    " Jupyter cell). Use print() for explicit output to 'stdout'.\n"
    "- Top-level await is supported (Jupyter kernel).\n"
    "- Playwright is pre-installed for browser automation (see `launch_browser` helper below).\n"
    "- Shell commands: use subprocess.run(), not '!' syntax.\n"
    "- Common packages are pre-installed: requests, numpy, pandas, matplotlib,"
    " scikit-learn, playwright, etc.\n"
    "\n"
    "Returning binary data (images, screenshots):\n"
    "- Generate the data, base64-encode it, and print it to stdout.\n"
    "- Example for plots:\n"
    "  import matplotlib; matplotlib.use('Agg')\n"
    "  import matplotlib.pyplot as plt, base64, io\n"
    "  fig, ax = plt.subplots()\n"
    "  ax.plot([1,2,3],[4,5,6])\n"
    "  buf = io.BytesIO()\n"
    "  fig.savefig(buf, format='png'); buf.seek(0)\n"
    "  print(base64.b64encode(buf.read()).decode())\n"
    "  plt.close()\n"
    "\n"
    "Playwright (browser automation):\n"
    "- ALWAYS use the pre-loaded helper to get a page:\n"
    "    page = await launch_browser()\n"
    "  NEVER call async_playwright() or chromium.launch() directly.\n"
    "  The helper configures optimal settings that are required\n"
    "  for sites to load properly.\n"
    "- Call launch_browser() once, then reuse `page` across calls (state persists).\n"
    "- Use the async API with top-level await.\n"
    "- To see what's on a page, you can:\n"
    "  1. Take a screenshot (returns base64 you can analyze):\n"
    "     import base64\n"
    "     screenshot_bytes = await page.screenshot(full_page=False)\n"
    "     print(base64.b64encode(screenshot_bytes).decode())\n"
    "  2. Extract text from the DOM:\n"
    "     text = await page.inner_text('body')\n"
    "     elements = await page.query_selector_all('css selector')\n"
    "     for el in elements:\n"
    "         print(await el.text_content())\n"
    "  Prefer DOM extraction for structured data. Use screenshots\n"
    "  when you need to understand visual layout or image content.\n"
    "- Use CSS selectors and aria attributes to find and interact\n"
    "  with elements.\n"
)

# ---------------------------------------------------------------------------
# Pydantic param schema
# ---------------------------------------------------------------------------


class ExecutePythonParams(BaseModel):
    code: str = Field(description="Python code to execute")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sanitize_input(code: str) -> str:
    """Strip backticks, whitespace, and 'python' prefix from LLM output."""
    code = re.sub(r"^(\s|`)*(?i:python)?\s*", "", code)
    code = re.sub(r"(\s|`)*$", "", code)
    return code


def _build_url(endpoint: str, session_id: str) -> str:
    base = endpoint.rstrip("/")
    encoded_id = urllib.parse.quote(session_id)
    return f"{base}/executions?api-version={_API_VERSION}&identifier={encoded_id}"


async def _execute_code(
    endpoint: str,
    code: str,
    session_id: str,
    token_provider: Callable[[], Awaitable[str]],
    http_session: aiohttp.ClientSession,
) -> str:
    """Execute Python code in an ACA dynamic session."""
    code = _sanitize_input(code)
    token = await token_provider()
    url = _build_url(endpoint, session_id)

    async with http_session.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "codeInputType": "Inline",
            "executionType": "Synchronous",
            "code": code,
            "timeoutInSeconds": 60,
        },
        timeout=aiohttp.ClientTimeout(total=120),
    ) as response:
        if response.status >= 400:
            body = await response.text()
            raise RuntimeError(f"ACA sessions API error ({response.status}): {body[:500]}")
        data = await response.json()

    result = data.get("result", {})
    return json.dumps(
        {
            "result": result.get("executionResult"),
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
        },
        indent=2,
    )


# ---------------------------------------------------------------------------
# Factory: create per-agent execute_python tool
# ---------------------------------------------------------------------------

# Shared credential and HTTP session (created lazily, reused across agents).
# These are process-wide because building credentials and aiohttp sessions is
# expensive — one is enough for the entire app.
_credential: DefaultAzureCredential | None = None
_token_provider: Callable[[], Awaitable[str]] | None = None
_http_session: aiohttp.ClientSession | None = None
_init_lock = asyncio.Lock()

# Track which ACA sessions have been set up (Playwright helper loaded)
_setup_sessions: set[str] = set()
_setup_lock = asyncio.Lock()


async def _ensure_shared_resources() -> None:
    """Lazily create the shared credential, token provider, and HTTP session."""
    global _credential, _token_provider, _http_session
    if _token_provider is not None:
        return
    async with _init_lock:
        if _token_provider is not None:
            return
        _credential = build_async_credential()
        _token_provider = get_bearer_token_provider(
            _credential, "https://dynamicsessions.io/.default"
        )
        _http_session = aiohttp.ClientSession()
        logger.debug(
            "execution_sandbox: shared credential, token provider, and HTTP session initialized"
        )


def create_sandbox_tools(
    config: dict[str, Any],
    *,
    fallback_session_id: str | None = None,
) -> list[FunctionTool]:
    """Create an ``execute_python`` tool bound to a specific ACA session pool.

    Parameters
    ----------
    config:
        The ``execution_sandbox`` block from agent frontmatter. Must contain
        ``session_pool_management_endpoint``.
    fallback_session_id:
        Used as the ACA session identifier so the REPL state persists across
        ``execute_python`` calls within the same conversation. The runner
        passes the resolved agent-runtime session id here. MAF does not
        currently expose the active session id to tools, so the runner bakes
        it into the tool closure on every request. When omitted, a fresh GUID
        is generated so independent invocations do not share a sandbox.

    Returns a list with one tool, or an empty list if the config is invalid.
    """
    raw_endpoint = config.get("session_pool_management_endpoint", "")
    if not raw_endpoint:
        logger.warning("execution_sandbox: missing 'session_pool_management_endpoint', skipping")
        return []

    endpoint = substitute_env_vars_in_value(str(raw_endpoint))
    if not endpoint or has_unresolved_placeholders(endpoint):
        logger.warning("execution_sandbox: could not resolve endpoint '%s', skipping", raw_endpoint)
        return []

    aca_session_id = fallback_session_id or uuid.uuid4().hex
    logger.info(
        "execution_sandbox: creating tool with endpoint %s (aca_session=%s)",
        endpoint,
        aca_session_id,
    )

    @tool(
        name="execute_python",
        description=_EXECUTE_PYTHON_DESCRIPTION,
        schema=ExecutePythonParams,
    )
    async def execute_python(params: ExecutePythonParams) -> str:
        await _ensure_shared_resources()
        token_provider = _token_provider
        http_session = _http_session
        assert token_provider is not None
        assert http_session is not None

        code = params.code or ""
        if not code.strip():
            return json.dumps({"error": "No code provided"})

        logger.info("execution_sandbox: executing code in ACA session %s", aca_session_id)

        try:
            # Pre-load Playwright helper on first call per session
            async with _setup_lock:
                if aca_session_id not in _setup_sessions:
                    await _execute_code(
                        endpoint,
                        _ACA_SESSION_SETUP,
                        aca_session_id,
                        token_provider,
                        http_session,
                    )
                    _setup_sessions.add(aca_session_id)

            result = await _execute_code(
                endpoint,
                code,
                aca_session_id,
                token_provider,
                http_session,
            )
            logger.info(
                "execution_sandbox: ACA session %s completed successfully",
                aca_session_id,
            )
            return result
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.error(
                "execution_sandbox: ACA session %s failed: %s",
                aca_session_id,
                error_msg,
            )
            return json.dumps({"error": error_msg})

    logger.info("execution_sandbox: execute_python tool created")
    return [execute_python]
