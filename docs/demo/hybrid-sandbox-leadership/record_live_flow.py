"""Record one bounded Hosted Skills chat run and redacted sandbox inventory."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from azure_functions_agents.transport.aca_sdk import AcaSandboxAdapter

DEFAULT_PROMPT = (
    "First use Microsoft Learn MCP to confirm in one sentence what Azure Container "
    "Apps dynamic sessions provide. Then, in the same model turn, issue these two "
    "independent local tool calls in parallel: run_shell with "
    '`python -c "import time; time.sleep(25); print(\'ALPHA_COMPLETE\')"` and '
    'run_shell with `python -c "import time; time.sleep(25); '
    "print('BETA_COMPLETE')\"`. Report the MCP takeaway and both exact markers. "
    "Do not include internal identifiers."
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


async def _inventory(
    sandbox_group: str,
    region: str,
    observations: list[dict[str, Any]] | None = None,
    stop: threading.Event | None = None,
    interval: float = 1.0,
) -> dict[str, Any]:
    adapter = await AcaSandboxAdapter.open(sandbox_group, region=region)
    try:
        latest: dict[str, Any] = {}
        while True:
            sandboxes = await adapter.list_sandboxes(labels={})
            states = Counter(
                item.state.value if hasattr(item.state, "value") else str(item.state)
                for item in sandboxes
            )
            latest = {
                "observed_at": _utc_now(),
                "inventory_count": len(sandboxes),
                "states": dict(sorted(states.items())),
            }
            if observations is not None:
                observations.append(latest)
            if stop is None or stop.is_set():
                return latest
            await asyncio.sleep(interval)
    finally:
        await adapter.close()


def _poll_inventory(
    sandbox_group: str,
    region: str,
    observations: list[dict[str, Any]],
    stop: threading.Event,
    interval: float,
) -> None:
    asyncio.run(_inventory(sandbox_group, region, observations, stop, interval))


def _wait_for_active_inventory(
    observations: list[dict[str, Any]],
    timeout: float = 45.0,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if observations and observations[-1]["inventory_count"] > 0:
            return observations[-1]
        time.sleep(0.25)
    return None


def _show_live_inventory(page: Page, observation: dict[str, Any]) -> None:
    page.evaluate(
        """observation => {
            document.querySelector("#liveSandboxEvidence")?.remove();
            const panel = document.createElement("aside");
            panel.id = "liveSandboxEvidence";
            panel.setAttribute("aria-label", "Live ACA Sandbox inventory");
            const stateSummary = Object.entries(observation.states)
                .map(([state, count]) => `${state}: ${count}`)
                .join(" · ");
            panel.innerHTML = `
                <div class="live-label"><span></span>LIVE READ-ONLY ACA INVENTORY</div>
                <div class="live-count">${observation.inventory_count} active sandbox</div>
                <div class="live-state"></div>
                <div class="live-time"></div>
            `;
            panel.querySelector(".live-state").textContent = stateSummary;
            panel.querySelector(".live-time").textContent =
                `Observed ${observation.observed_at}`;
            document.body.appendChild(panel);
        }""",
        observation,
    )


def _prepare_chat(page: Page, base_url: str, function_key: str) -> None:
    page.goto(f"{base_url}/agents/main/", wait_until="networkidle", timeout=120_000)
    page.evaluate(
        """([url, key]) => {
            localStorage.setItem("chat.baseUrl", url);
            localStorage.setItem("chat.key", key);
            localStorage.removeItem("chat.sessionId");
            localStorage.removeItem("chat.sessionIdSource");
            localStorage.removeItem("chat.recentSessions");
        }""",
        [base_url, function_key],
    )
    page.reload(wait_until="networkidle", timeout=120_000)
    page.add_style_tag(
        content="""
            #sessionBar, #status, .details-pre,
            .details-section:first-child { display: none !important; }
            .app { max-width: 1180px !important; }
            .chat { padding: 28px 34px !important; }
            .bubble { font-size: 17px !important; line-height: 1.5 !important; }
            .details-modal { width: min(1050px, 100%) !important; }
            .details-item-title { color: #111827 !important; font-size: 17px !important; }
            #liveSandboxEvidence {
                position: fixed;
                right: 24px;
                bottom: 24px;
                z-index: 10001;
                width: 370px;
                padding: 20px 22px;
                border: 1px solid #86efac;
                border-radius: 14px;
                background: rgba(240, 253, 244, 0.98);
                box-shadow: 0 18px 44px rgba(15, 23, 42, 0.18);
                color: #14532d;
                font-family: "Segoe UI", sans-serif;
            }
            #liveSandboxEvidence .live-label {
                font-size: 13px;
                font-weight: 700;
                letter-spacing: 0.08em;
            }
            #liveSandboxEvidence .live-label span {
                display: inline-block;
                width: 10px;
                height: 10px;
                margin-right: 8px;
                border-radius: 50%;
                background: #16a34a;
                box-shadow: 0 0 0 5px rgba(22, 163, 74, 0.14);
            }
            #liveSandboxEvidence .live-count {
                margin-top: 14px;
                font-size: 28px;
                font-weight: 750;
            }
            #liveSandboxEvidence .live-state {
                margin-top: 8px;
                font-size: 16px;
                font-weight: 600;
            }
            #liveSandboxEvidence .live-time {
                margin-top: 10px;
                color: #3f6212;
                font-size: 12px;
            }
        """
    )
    page.locator("#settingsBackdrop").wait_for(state="hidden", timeout=30_000)
    page.locator("#promptInput").wait_for()


def main() -> None:  # noqa: PLR0915 - linear orchestration keeps the safety gates explicit
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--chrome", required=True, type=Path)
    parser.add_argument("--sandbox-group", required=True)
    parser.add_argument("--region", default="westus2")
    parser.add_argument(
        "--base-url",
        default="https://func-hybrid-sbx-0902.azurewebsites.net",
    )
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--inventory-interval", type=float, default=1.0)
    args = parser.parse_args()

    function_key = os.environ.get("HYBRID_SPIKE_FUNCTION_KEY", "").strip()
    if not function_key:
        raise RuntimeError("HYBRID_SPIKE_FUNCTION_KEY must be set in process memory.")

    args.output_root.mkdir(parents=True, exist_ok=True)
    pre_run = asyncio.run(_inventory(args.sandbox_group, args.region))
    if pre_run["inventory_count"] != 0:
        raise RuntimeError("Sandbox inventory must be zero before recording.")

    observations: list[dict[str, Any]] = []
    stop = threading.Event()
    poller = threading.Thread(
        target=_poll_inventory,
        args=(
            args.sandbox_group,
            args.region,
            observations,
            stop,
            args.inventory_interval,
        ),
        daemon=True,
    )

    request_status: int | None = None
    request_start: str | None = None
    request_end: str | None = None
    terminal_state = "unknown"
    active_observation: dict[str, Any] | None = None
    raw_video: Path | None = None

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=str(args.chrome),
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            record_video_dir=str(args.output_root),
            record_video_size={"width": 1920, "height": 1080},
            color_scheme="light",
        )
        page = context.new_page()
        video = page.video
        _prepare_chat(page, args.base_url, function_key)

        page.locator("#promptInput").press_sequentially(args.prompt, delay=2)
        time.sleep(2)
        poller.start()
        request_start = _utc_now()
        with page.expect_response(
            lambda response: response.url.split("?", 1)[0].endswith(
                "/agents/main/chatstream"
            ),
            timeout=args.timeout_seconds * 1000,
        ) as response_info:
            page.locator("#sendBtn").click()
        response = response_info.value
        request_status = response.status

        try:
            tool_bubble = page.locator(
                ".bubble.assistant.has-details",
                has_text="3 tool calls",
            )
            tool_bubble.wait_for(timeout=120_000)
            tool_bubble.click()
            page.locator("#detailsBackdrop.open").wait_for()
            page.locator(".details-item-title").nth(2).wait_for(timeout=30_000)
            active_observation = _wait_for_active_inventory(observations)
            if active_observation is not None:
                _show_live_inventory(page, active_observation)
            page.screenshot(path=args.output_root / "live-chat-tool-calls.png")
            if active_observation is not None:
                page.screenshot(path=args.output_root / "live-sandbox-active.png")
            time.sleep(12)
            page.locator("#closeDetailsBtn").click()
        except PlaywrightTimeoutError:
            pass

        response.finished()
        page.locator("#sendBtn:not([disabled])").wait_for(
            timeout=args.timeout_seconds * 1000
        )
        request_end = _utc_now()
        terminal_state = "done" if request_status == 200 else "error"
        time.sleep(5)

        page.screenshot(path=args.output_root / "live-chat-final.png")
        body_text = page.locator("body").inner_text()
        page.close()
        context.close()
        raw_video = Path(video.path())
        browser.close()

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if observations and observations[-1]["inventory_count"] == 0:
            break
        time.sleep(1)
    stop.set()
    poller.join(timeout=30)

    final_inventory = asyncio.run(_inventory(args.sandbox_group, args.region))
    if raw_video is None:
        raise RuntimeError("Playwright did not finalize the live recording.")
    video_target = args.output_root / "live-hosted-skill-flow.webm"
    raw_video.replace(video_target)

    safe_text = body_text.replace(function_key, "[REDACTED]")
    (args.output_root / "live-chat-transcript.txt").write_text(
        safe_text,
        encoding="utf-8",
    )
    result = {
        "prompt_id": "parallel-shell-mcp-leadership-demo",
        "prompt": args.prompt,
        "request": {
            "window_start": request_start,
            "window_end": request_end,
            "http_status": request_status,
            "terminal_state": terminal_state,
        },
        "sandbox_inventory": {
            "pre_run": pre_run,
            "observations": observations,
            "active_capture": active_observation,
            "final": final_inventory,
            "operator_cleanup_used": False,
        },
        "assets": {
            "video": str(video_target.resolve()),
            "final_screenshot": str(
                (args.output_root / "live-chat-final.png").resolve()
            ),
            "tool_calls_screenshot": str(
                (args.output_root / "live-chat-tool-calls.png").resolve()
            ),
            "active_sandbox_screenshot": (
                str((args.output_root / "live-sandbox-active.png").resolve())
                if active_observation is not None
                else None
            ),
            "transcript": str(
                (args.output_root / "live-chat-transcript.txt").resolve()
            ),
        },
    }
    (args.output_root / "live-flow-result.json").write_text(
        json.dumps(result, indent=2) + "\n",
        encoding="utf-8",
    )
    if request_status != 200 or terminal_state != "done":
        raise RuntimeError("The bounded live request did not complete successfully.")
    if final_inventory["inventory_count"] != 0:
        raise RuntimeError("Sandbox inventory did not return to zero.")
    if active_observation is None:
        raise RuntimeError("No active sandbox was observed during the bounded request.")


if __name__ == "__main__":
    main()
