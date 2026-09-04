"""Record the leadership demo scenes and evidence stills with Playwright."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

SCENES = [
    {"id": "opening", "scene": 1, "duration": 4},
    {"id": "topology", "scene": 2, "duration": 8},
    {"id": "sandbox", "scene": 5, "duration": 16},
    {"id": "agent-trace", "scene": 6, "duration": 20},
    {"id": "apim", "scene": 4, "duration": 18},
    {"id": "demo-results", "scene": 8, "duration": 18},
    {"id": "takeaway", "scene": 9, "duration": 10},
]

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--chrome", required=True, type=Path)
    parser.add_argument("--only-scene", type=int)
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    page_url = (root / "demo.html").resolve().as_uri()
    clips = args.output_root / "raw"
    clips.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, object]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=str(args.chrome),
        )
        selected = [
            item
            for item in SCENES
            if args.only_scene is None or item["scene"] == args.only_scene
        ]
        if not selected:
            raise ValueError("only-scene must select one known scene")
        for item in selected:
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                record_video_dir=str(clips),
                record_video_size={"width": 1920, "height": 1080},
                color_scheme="dark",
            )
            page = context.new_page()
            video = page.video
            page.goto(f"{page_url}?scene={item['scene']}", wait_until="load")
            page.locator(".scene.active").wait_for()
            time.sleep(1)
            time.sleep(float(item["duration"]) - 1)
            page.close()
            context.close()
            path = Path(video.path())
            target = clips / f"{item['id']}.webm"
            path.replace(target)
            manifest.append(
                {
                    "path": str(target.resolve()),
                    "start": 0,
                    "duration": item["duration"],
                }
            )
        browser.close()

    if args.only_scene is None:
        (args.output_root / "scene-manifest.json").write_text(
            json.dumps(
                {
                    "width": 1920,
                    "height": 1080,
                    "fps": 30,
                    "crf": 20,
                    "scenes": manifest,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
