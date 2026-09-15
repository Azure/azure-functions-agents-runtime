"""Isolated Chromium automation for the durable chat browser tests."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp
import pytest


class ChromiumPage:
    def __init__(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        self.websocket = websocket
        self.session_id: str | None = None
        self.events: deque[dict[str, Any]] = deque(maxlen=200)
        self._sequence = 0
        self._lock = asyncio.Lock()

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        browser: bool = False,
        timeout: float = 15,
    ) -> dict[str, Any]:
        async with self._lock:
            self._sequence += 1
            command: dict[str, Any] = {
                "id": self._sequence,
                "method": method,
                "params": params or {},
            }
            if self.session_id is not None and not browser:
                command["sessionId"] = self.session_id
            await self.websocket.send_json(command)
            async with asyncio.timeout(timeout):
                async for message in self.websocket:
                    if message.type != aiohttp.WSMsgType.TEXT:
                        raise AssertionError(f"Chromium connection closed during {method}")
                    result = json.loads(message.data)
                    if result.get("id") != self._sequence:
                        self.events.append(result)
                        continue
                    if "error" in result:
                        raise AssertionError(f"Chromium {method}: {result['error']}")
                    return result.get("result", {})
        raise AssertionError(f"Chromium connection ended during {method}")

    async def evaluate(self, expression: str) -> Any:
        result = await self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
            },
        )
        if "exceptionDetails" in result:
            raise AssertionError(f"Browser JavaScript failed: {result['exceptionDetails']}")
        return result["result"].get("value")

    async def wait_for(self, expression: str, *, timeout: float = 15) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = await self.evaluate(expression)
            if value:
                return value
            await asyncio.sleep(0.05)
        page_text = await self.evaluate("document.body?.innerText ?? ''")
        raise AssertionError(
            f"Browser condition did not become true: {expression}\nPage text:\n{page_text}"
        )

    async def navigate(self, url: str) -> None:
        result = await self.call("Page.navigate", {"url": url})
        assert "errorText" not in result, result
        await self.wait_for(
            f"location.href === {json.dumps(url)} && document.readyState === 'complete'"
        )

    async def screenshot(self, path: Path) -> None:
        image = await self.call("Page.captureScreenshot", {"format": "png"})
        path.write_bytes(base64.b64decode(image["data"]))


def _browser_executable() -> str:
    configured = os.environ.get("DURABLE_CHAT_CHROMIUM")
    if configured:
        if not Path(configured).is_file():
            pytest.fail("DURABLE_CHAT_CHROMIUM does not name an existing executable")
        return configured
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome", "msedge"):
        executable = shutil.which(name)
        if executable:
            return executable
    for candidate in (
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    pytest.skip("A Chromium browser is required; set DURABLE_CHAT_CHROMIUM to its executable")


async def _stop_browser(process: subprocess.Popen[bytes]) -> None:
    try:
        await asyncio.to_thread(process.wait, timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            await asyncio.to_thread(
                subprocess.run,
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"Stop-Process -Id {process.pid} -Force -ErrorAction Stop",
                ],
                check=True,
                capture_output=True,
                timeout=15,
            )
        else:
            process.terminate()
        await asyncio.to_thread(process.wait, timeout=10)


@asynccontextmanager
async def chromium_page(directory: Path) -> AsyncIterator[ChromiumPage]:
    directory.mkdir(parents=True, exist_ok=True)
    profile = directory / "profile"
    port_file = profile / "DevToolsActivePort"
    with (directory / "chromium.log").open("wb") as log:
        process = subprocess.Popen(
            [
                _browser_executable(),
                "--headless=new",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-networking",
                "--disable-extensions",
                "--disable-sync",
                "--remote-debugging-port=0",
                f"--user-data-dir={profile}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 20
            while True:
                if process.poll() is not None:
                    raise AssertionError(f"Chromium exited; see {directory / 'chromium.log'}")
                if time.monotonic() >= deadline:
                    raise AssertionError("Chromium did not expose a readable debugging endpoint")
                try:
                    endpoint = port_file.read_text(encoding="utf-8").splitlines()
                except (FileNotFoundError, PermissionError):
                    endpoint = []
                if (
                    len(endpoint) >= 2
                    and endpoint[0].isascii()
                    and endpoint[0].isdigit()
                    and endpoint[1].startswith("/devtools/browser/")
                ):
                    port, websocket_path = endpoint[:2]
                    break
                await asyncio.sleep(0.05)
            async with (
                aiohttp.ClientSession() as client,
                client.ws_connect(f"http://127.0.0.1:{port}{websocket_path}") as websocket,
            ):
                page = ChromiumPage(websocket)
                target = await page.call(
                    "Target.createTarget", {"url": "about:blank"}, browser=True
                )
                attached = await page.call(
                    "Target.attachToTarget",
                    {"targetId": target["targetId"], "flatten": True},
                    browser=True,
                )
                page.session_id = attached["sessionId"]
                await page.call("Page.enable")
                await page.call("Runtime.enable")
                try:
                    yield page
                finally:
                    if not websocket.closed:
                        await page.call("Browser.close", browser=True)
        finally:
            await _stop_browser(process)
