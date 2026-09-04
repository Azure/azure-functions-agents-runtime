"""Capture a redacted Application Insights Agent Trace from an authenticated profile."""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import Frame, Page, sync_playwright

TRACE_HOST = "appinsights.hosting.portal.azure.net"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sanitize_frame(frame: Frame) -> None:
    frame.evaluate(
        """() => {
            const walker = document.createTreeWalker(
                document.body,
                NodeFilter.SHOW_TEXT,
            );
            const nodes = [];
            while (walker.nextNode()) nodes.push(walker.currentNode);
            for (const node of nodes) {
                let value = node.nodeValue ?? "";
                if (/\\/subscriptions\\//i.test(value)) {
                    value = "ACA Sandbox management operation · identifiers redacted";
                }
                if (/Azure blob:/i.test(value)) {
                    value = "Azure Blob operation · identifiers redacted";
                }
                if (/\\[\\{"type":\\s*"function"/i.test(value)) {
                    value = "[tool definitions redacted]";
                }
                value = value
                    .replace(
                        /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}/g,
                        "[authenticated account]",
                    )
                    .replace(
                        /\\b[0-9a-f]{8}-[0-9a-f-]{27,36}\\b/gi,
                        "[redacted]",
                    )
                    .replace(/\\b[0-9a-f]{32}\\b/gi, "[redacted]");
                node.nodeValue = value;
            }
            document.querySelectorAll(
                '#fxs-avatarmenu-button, [aria-label*="Account menu"]',
            ).forEach((element) => {
                element.style.visibility = "hidden";
            });
        }"""
    )


def _add_badge(page: Page) -> None:
    page.evaluate(
        """() => {
            document.querySelector("#agentTraceCaptureBadge")?.remove();
            const badge = document.createElement("aside");
            badge.id = "agentTraceCaptureBadge";
            badge.textContent =
                "LIVE PORTAL · APPLICATION INSIGHTS AGENT TRACE · REDACTED";
            Object.assign(badge.style, {
                position: "fixed",
                right: "28px",
                bottom: "24px",
                zIndex: "100000",
                padding: "12px 18px",
                borderRadius: "10px",
                background: "rgba(7, 21, 35, 0.94)",
                border: "1px solid #53b7ff",
                color: "#f5f9ff",
                font: "700 14px Segoe UI, sans-serif",
                letterSpacing: "0.04em",
                boxShadow: "0 10px 30px rgba(0, 0, 0, 0.22)",
            });
            document.body.appendChild(badge);
        }"""
    )


def _trace_frame(page: Page) -> Frame:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        for frame in page.frames:
            if TRACE_HOST in frame.url:
                return frame
        page.wait_for_timeout(500)
    raise RuntimeError("Application Insights trace frame did not load.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile-dir", required=True, type=Path)
    parser.add_argument("--trace-url", required=True)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--duration-seconds", type=float, default=18)
    args = parser.parse_args()

    args.output_root.mkdir(parents=True, exist_ok=True)
    screenshot = args.output_root / "appinsights-agent-trace-portal.png"
    video_target = args.output_root / "appinsights-agent-trace-portal.webm"
    raw_video: Path | None = None
    captured_at: str | None = None

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(args.profile_dir),
            channel="msedge",
            headless=False,
            viewport={"width": 1920, "height": 1080},
            record_video_dir=str(args.output_root),
            record_video_size={"width": 1920, "height": 1080},
            color_scheme="light",
        )
        page = context.new_page()
        for existing_page in context.pages:
            if existing_page != page:
                existing_page.close()
        video = page.video
        page.goto(args.trace_url, wait_until="domcontentloaded", timeout=120_000)
        trace_frame = _trace_frame(page)
        trace_frame.get_by_text("End-to-end transaction", exact=True).wait_for(
            timeout=120_000
        )
        page.wait_for_timeout(5_000)

        for frame in page.frames:
            _sanitize_frame(frame)
        _add_badge(page)
        captured_at = _utc_now()
        page.screenshot(path=screenshot)

        page.wait_for_timeout(5_000)
        page.mouse.move(1050, 760)
        page.mouse.wheel(0, 420)
        page.wait_for_timeout(max(args.duration_seconds - 10, 1) * 1_000)

        page.close()
        context.close()
        raw_video = Path(video.path())

    if raw_video is None or captured_at is None:
        raise RuntimeError("Playwright did not finalize the Agent Trace capture.")
    raw_video.replace(video_target)
    result = {
        "captured_at_utc": captured_at,
        "source": "Application Insights Agents (Preview) Agent Trace blade",
        "scope": "Earlier retained live agent run; not the 68.8-second capture run",
        "redactions": [
            "account identity",
            "operation identifiers",
            "subscription identifiers",
            "sandbox identifiers",
            "resource paths",
        ],
        "assets": {
            "screenshot": str(screenshot.resolve()),
            "video": str(video_target.resolve()),
        },
    }
    (args.output_root / "agent-trace-capture.json").write_text(
        json.dumps(result, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
