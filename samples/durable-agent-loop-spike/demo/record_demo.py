from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Callable
from urllib.request import urlopen

from playwright.sync_api import BrowserContext, Page, sync_playwright

_CONTROL_URL = "http://127.0.0.1:8765"
_TENANT_ID = "72f988bf-86f1-41af-91ab-2d7cd011db47"
_SUBSCRIPTION_ID = os.environ["DURABLE_LOOP_DEMO_SUBSCRIPTION_ID"]
_DTS_URL = (
    "https://dashboard.durabletask.io/subscriptions/"
    f"{_SUBSCRIPTION_ID}/schedulers/dts-durable-loop-0904/taskhubs/"
    "durable-loop-demo?endpoint=https%3a%2f%2fdts-durable-loo-gfekafew."
    f"eastus2.durabletask.io&tenantId={_TENANT_ID}"
)
_APP_INSIGHTS_URL = (
    f"https://portal.azure.com/?tenant={_TENANT_ID}#@microsoft.onmicrosoft.com/"
    f"resource/subscriptions/{_SUBSCRIPTION_ID}/resourceGroups/"
    "larohra-durable-agent-loop/providers/Microsoft.Insights/components/"
    "appi-durable-loop-0904/overview"
)
_APIM_URL = (
    f"https://portal.azure.com/?tenant={_TENANT_ID}#@microsoft.onmicrosoft.com/"
    f"resource/subscriptions/{_SUBSCRIPTION_ID}/resourceGroups/"
    "larohra-operations-agent-3p-rg/providers/Microsoft.ApiManagement/service/"
    "larohra-ai-gateway/overview"
)
_SANDBOX_URL = (
    f"https://portal.azure.com/?tenant={_TENANT_ID}#@microsoft.onmicrosoft.com/"
    f"resource/subscriptions/{_SUBSCRIPTION_ID}/resourceGroups/"
    "larohra-durable-agent-loop/providers/Microsoft.App/sandboxGroups/"
    "sbg-durable-loop-0904/overview"
)
_TERMINAL = frozenset({"Completed", "Failed", "Cancelled"})


def _json_get(url: str) -> dict[str, object]:
    with urlopen(url, timeout=120) as response:
        document = json.load(response)
    if not isinstance(document, dict):
        raise RuntimeError("control room response was not an object")
    return document


def _wait_run(run_id: str, desired: set[str], timeout_seconds: int) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        try:
            last = _json_get(f"{_CONTROL_URL}/api/run/{run_id}")
        except (OSError, TimeoutError, json.JSONDecodeError):
            time.sleep(3)
            continue
        if last.get("status") in desired:
            return last
        time.sleep(4)
    raise TimeoutError(f"run {run_id} did not reach {sorted(desired)}: {last}")


def _wait_sandbox_state(desired: set[str], timeout_seconds: int) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        last = _json_get(f"{_CONTROL_URL}/api/sandboxes")
        items = last.get("items")
        if not isinstance(items, list):
            time.sleep(5)
            continue
        states = {str(item.get("state")) for item in items if isinstance(item, dict)}
        if ("Deleted" in desired and not items) or states & desired:
            return last
        time.sleep(10)
    raise TimeoutError(f"sandbox did not reach {sorted(desired)}: {last}")


def _record(
    context: BrowserContext,
    *,
    name: str,
    url: str,
    raw_dir: Path,
    evidence_dir: Path,
    duration: float,
    prepare: Callable[[Page], None] | None = None,
    action: Callable[[Page], None] | None = None,
    wait_for: Callable[[Page], None] | None = None,
) -> dict[str, object]:
    page = context.new_page()
    video = page.video
    created = time.monotonic()
    page.goto(url, wait_until="domcontentloaded", timeout=120_000)
    if wait_for is not None:
        wait_for(page)
    else:
        page.wait_for_timeout(1_500)
    if prepare is not None:
        prepare(page)
    content_start = time.monotonic()
    if action is not None:
        action(page)
    page.screenshot(path=str(evidence_dir / f"{name}.png"))
    page.wait_for_timeout(round(duration * 1000))
    content_end = time.monotonic()
    page.close()
    raw_path = Path(video.path())
    target = raw_dir / f"{name}.webm"
    if raw_path != target:
        raw_path.replace(target)
    return {
        "name": name,
        "path": str(target),
        "start": round(content_start - created, 3),
        "duration": round(content_end - content_start, 3),
    }


def _scene_url(source: Path, scene: str) -> str:
    return f"{source.resolve().as_uri()}?scene={scene}"


def _load_active_run(page: Page) -> dict[str, object]:
    raw = page.evaluate("localStorage.getItem('durableLoopDemoRun')")
    if not isinstance(raw, str):
        raise RuntimeError("controller did not persist the active run")
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise RuntimeError("active run was not an object")
    return document


def _wait_control_ready(page: Page) -> None:
    page.get_by_role("heading", name="Durable Agent Loop").wait_for(timeout=30_000)


def _redact_portal(page: Page, aliases: dict[str, str]) -> None:
    page.evaluate(
        """
        ({ aliases, subscription }) => {
          const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
          const nodes = [];
          while (walker.nextNode()) nodes.push(walker.currentNode);
          for (const node of nodes) {
            let value = node.nodeValue || "";
            value = value.replaceAll(subscription, "SUBSCRIPTION-REDACTED");
            value = value.replace(/larohra@microsoft\\.com/gi, "IDENTITY REDACTED");
            for (const [full, alias] of Object.entries(aliases)) value = value.replaceAll(full, alias);
            value = value.replace(/@[0-9a-f]{64}/g, "@SESSION-FENCE");
            node.nodeValue = value;
          }
          const badge = document.createElement("div");
          badge.textContent = "PRIVATE SPIKE · IDENTITY + TENANT DETAILS REDACTED";
          Object.assign(badge.style, {
            position: "fixed", top: "12px", right: "18px", zIndex: 2147483647,
            background: "#10283e", color: "#cde9ff", border: "1px solid #3f769f",
            padding: "8px 12px", borderRadius: "9px", font: "600 12px Segoe UI"
          });
          document.body.appendChild(badge);
        }
        """,
        {"aliases": aliases, "subscription": _SUBSCRIPTION_ID},
    )


def _click_scenario(name: str) -> Callable[[Page], None]:
    def action(page: Page) -> None:
        page.locator(f'button[data-scenario="{name}"]').click()
        page.locator("#runId").filter(has_not_text="RUN —").wait_for(timeout=120_000)
        page.wait_for_timeout(3_000)

    return action


def main() -> None:
    parser = argparse.ArgumentParser(description="Record the Durable Agent Loop leadership demo.")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--portal-profile", type=Path, required=True)
    args = parser.parse_args()

    output_root = args.output_root.resolve()
    raw_dir = output_root / "raw"
    evidence_dir = output_root / "evidence"
    capture_profile = output_root.parent / "durable-loop-video-capture-profile"
    raw_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).with_name("video-scenes.html")
    scenes: list[dict[str, object]] = []
    runs: dict[str, dict[str, object]] = {}

    with sync_playwright() as playwright:
        control = playwright.chromium.launch_persistent_context(
            user_data_dir=str(capture_profile),
            channel="chrome",
            headless=True,
            viewport={"width": 1440, "height": 900},
            record_video_dir=str(raw_dir),
            record_video_size={"width": 1440, "height": 900},
            color_scheme="light",
        )
        reset = control.new_page()
        reset.goto(_CONTROL_URL, wait_until="domcontentloaded")
        reset.evaluate("localStorage.clear()")
        reset.close()

        for scene, duration in (
            ("intro", 18),
            ("architecture", 32),
            ("live", 18),
        ):
            scenes.append(
                _record(
                    control,
                    name=f"card-{scene}",
                    url=_scene_url(source, scene),
                    raw_dir=raw_dir,
                    evidence_dir=evidence_dir,
                    duration=duration,
                )
            )

        scenes.append(
            _record(
                control,
                name="control-retained-create-start",
                url=_CONTROL_URL,
                raw_dir=raw_dir,
                evidence_dir=evidence_dir,
                duration=22,
                wait_for=_wait_control_ready,
                action=_click_scenario("retained_create"),
            )
        )
        page = control.new_page()
        page.goto(_CONTROL_URL, wait_until="domcontentloaded")
        runs["retained_create"] = _load_active_run(page)
        page.close()
        _wait_run(str(runs["retained_create"]["run_id"]), set(_TERMINAL), 1_200)
        scenes.append(
            _record(
                control,
                name="control-retained-create-complete",
                url=_CONTROL_URL,
                raw_dir=raw_dir,
                evidence_dir=evidence_dir,
                duration=18,
                wait_for=lambda page: page.locator("#status").filter(has_text="Completed").wait_for(
                    timeout=120_000
                ),
            )
        )

        _wait_sandbox_state({"Stopped"}, 480)
        scenes.append(
            _record(
                control,
                name="control-retained-reuse-start",
                url=_CONTROL_URL,
                raw_dir=raw_dir,
                evidence_dir=evidence_dir,
                duration=26,
                wait_for=lambda page: page.locator(".sandbox-state.stopped").wait_for(
                    timeout=60_000
                ),
                action=_click_scenario("retained_reuse"),
            )
        )
        page = control.new_page()
        page.goto(_CONTROL_URL, wait_until="domcontentloaded")
        runs["retained_reuse"] = _load_active_run(page)
        page.close()
        _wait_run(str(runs["retained_reuse"]["run_id"]), set(_TERMINAL), 1_200)
        scenes.append(
            _record(
                control,
                name="control-retained-reuse-complete",
                url=_CONTROL_URL,
                raw_dir=raw_dir,
                evidence_dir=evidence_dir,
                duration=18,
                wait_for=lambda page: page.locator("#status").filter(has_text="Completed").wait_for(
                    timeout=120_000
                ),
            )
        )

        scenes.append(
            _record(
                control,
                name="control-fault-start",
                url=_CONTROL_URL,
                raw_dir=raw_dir,
                evidence_dir=evidence_dir,
                duration=22,
                wait_for=_wait_control_ready,
                action=_click_scenario("fault_recovery"),
            )
        )
        page = control.new_page()
        page.goto(_CONTROL_URL, wait_until="domcontentloaded")
        runs["fault_recovery"] = _load_active_run(page)
        page.close()
        _wait_run(str(runs["fault_recovery"]["run_id"]), set(_TERMINAL), 1_500)
        scenes.append(
            _record(
                control,
                name="control-fault-complete",
                url=_CONTROL_URL,
                raw_dir=raw_dir,
                evidence_dir=evidence_dir,
                duration=20,
                wait_for=lambda page: page.locator("#status").filter(has_text="Completed").wait_for(
                    timeout=120_000
                ),
            )
        )

        scenes.append(
            _record(
                control,
                name="control-hitl-start",
                url=_CONTROL_URL,
                raw_dir=raw_dir,
                evidence_dir=evidence_dir,
                duration=20,
                wait_for=_wait_control_ready,
                action=_click_scenario("hitl"),
            )
        )
        page = control.new_page()
        page.goto(_CONTROL_URL, wait_until="domcontentloaded")
        runs["hitl"] = _load_active_run(page)
        page.close()
        _wait_run(str(runs["hitl"]["run_id"]), {"Waiting"}, 900)

        def answer_hitl(page: Page) -> None:
            page.locator("#human.visible").wait_for(timeout=120_000)
            page.wait_for_timeout(6_000)
            page.get_by_role("button", name="Beta").click()
            page.locator("#status").filter(has_text="Completed").wait_for(timeout=180_000)

        scenes.append(
            _record(
                control,
                name="control-hitl-wait-resume",
                url=_CONTROL_URL,
                raw_dir=raw_dir,
                evidence_dir=evidence_dir,
                duration=18,
                wait_for=_wait_control_ready,
                action=answer_hitl,
            )
        )
        scenes.append(
            _record(
                control,
                name="card-observability",
                url=_scene_url(source, "observability"),
                raw_dir=raw_dir,
                evidence_dir=evidence_dir,
                duration=20,
            )
        )
        control.close()

        aliases = {
            str(runs["retained_reuse"]["run_id"]): (
                "RUN " + str(runs["retained_reuse"]["run_id"])[4:12].upper()
            ),
            str(runs["fault_recovery"]["run_id"]): (
                "RUN " + str(runs["fault_recovery"]["run_id"])[4:12].upper()
            ),
            str(runs["hitl"]["run_id"]): "RUN " + str(runs["hitl"]["run_id"])[4:12].upper(),
        }
        portal = playwright.chromium.launch_persistent_context(
            user_data_dir=str(args.portal_profile.resolve()),
            channel="chrome",
            headless=True,
            viewport={"width": 1440, "height": 900},
            record_video_dir=str(raw_dir),
            record_video_size={"width": 1440, "height": 900},
            color_scheme="light",
        )

        def open_dts_detail(page: Page) -> None:
            run_id = str(runs["fault_recovery"]["run_id"])
            page.get_by_role("link", name=run_id).click()
            page.get_by_role("button", name="Timeline").wait_for(timeout=90_000)
            _redact_portal(page, aliases)
            page.get_by_role("button", name="Timeline").click()
            page.wait_for_timeout(3_000)

        scenes.append(
            _record(
                portal,
                name="portal-dts-timeline",
                url=_DTS_URL,
                raw_dir=raw_dir,
                evidence_dir=evidence_dir,
                duration=35,
                wait_for=lambda page: page.get_by_role("heading", name="Orchestrations").wait_for(
                    timeout=120_000
                ),
                prepare=open_dts_detail,
            )
        )

        def open_agents(page: Page) -> None:
            page.get_by_role("link", name="Agents (Preview)").click()
            page.get_by_role("heading", name="Agent Operational Metrics").wait_for(timeout=120_000)
            _redact_portal(page, aliases)

        scenes.append(
            _record(
                portal,
                name="portal-app-insights-agents",
                url=_APP_INSIGHTS_URL,
                raw_dir=raw_dir,
                evidence_dir=evidence_dir,
                duration=30,
                wait_for=lambda page: page.get_by_role("heading", name="Essentials").wait_for(
                    timeout=120_000
                ),
                prepare=open_agents,
            )
        )

        scenes.append(
            _record(
                portal,
                name="portal-apim",
                url=_APIM_URL,
                raw_dir=raw_dir,
                evidence_dir=evidence_dir,
                duration=25,
                wait_for=lambda page: page.wait_for_timeout(30_000),
                prepare=lambda page: _redact_portal(page, aliases),
            )
        )
        scenes.append(
            _record(
                portal,
                name="portal-aca-sandbox-group",
                url=_SANDBOX_URL,
                raw_dir=raw_dir,
                evidence_dir=evidence_dir,
                duration=22,
                wait_for=lambda page: page.wait_for_timeout(30_000),
                prepare=lambda page: _redact_portal(page, aliases),
            )
        )
        portal.close()

        control = playwright.chromium.launch_persistent_context(
            user_data_dir=str(capture_profile),
            channel="chrome",
            headless=True,
            viewport={"width": 1440, "height": 900},
            record_video_dir=str(raw_dir),
            record_video_size={"width": 1440, "height": 900},
            color_scheme="light",
        )
        _wait_sandbox_state({"Deleted"}, 900)
        scenes.append(
            _record(
                control,
                name="control-sandbox-deleted",
                url=_CONTROL_URL,
                raw_dir=raw_dir,
                evidence_dir=evidence_dir,
                duration=18,
                wait_for=lambda page: page.locator("#sandboxCount").filter(has_text="0 LIVE").wait_for(
                    timeout=120_000
                ),
            )
        )
        for scene, duration in (("results", 38), ("close", 25)):
            scenes.append(
                _record(
                    control,
                    name=f"card-{scene}",
                    url=_scene_url(source, scene),
                    raw_dir=raw_dir,
                    evidence_dir=evidence_dir,
                    duration=duration,
                )
            )
        control.close()

    manifest = {
        "width": 1440,
        "height": 900,
        "fps": 30,
        "crf": 20,
        "scenes": scenes,
        "runs": runs,
    }
    (output_root / "scene-manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"scene_count": len(scenes), "runs": runs}, indent=2))


if __name__ == "__main__":
    main()
