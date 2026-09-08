from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_WIDTH = 1440
_HEIGHT = 900
_FONT = Path(r"C:\Windows\Fonts\segoeui.ttf")

_SCENES = {
    "portal-dts-timeline": {
        "duration": 32.0,
        "regions": [(0, 648, 286, 250, "IDENTITY REDACTED")],
    },
    "portal-app-insights-agents": {
        "duration": 25.0,
        "regions": [],
    },
    "portal-apim": {
        "duration": 18.0,
        "regions": [
            (1080, 0, 360, 70, "IDENTITY REDACTED"),
            (405, 300, 520, 55, "SUBSCRIPTION ID REDACTED"),
        ],
    },
    "portal-aca-sandbox-group": {
        "duration": 15.0,
        "regions": [
            (1080, 0, 360, 70, "IDENTITY REDACTED"),
            (470, 430, 515, 55, "SUBSCRIPTION ID REDACTED"),
        ],
    },
}


def _run(command: list[str]) -> str:
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def _duration(path: Path) -> float:
    return float(
        _run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ]
        )
    )


def _overlay(path: Path, regions: list[tuple[int, int, int, int, str]]) -> None:
    image = Image.new("RGBA", (_WIDTH, _HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(_FONT), 17)
    for x, y, width, height, label in regions:
        draw.rounded_rectangle(
            (x, y, x + width, y + height),
            radius=10,
            fill=(16, 40, 62, 255),
            outline=(63, 118, 159, 255),
            width=2,
        )
        bounds = draw.textbbox((0, 0), label, font=font)
        text_width = bounds[2] - bounds[0]
        text_height = bounds[3] - bounds[1]
        draw.text(
            (
                x + (width - text_width) / 2,
                y + (height - text_height) / 2 - bounds[1],
            ),
            label,
            font=font,
            fill=(205, 233, 255, 255),
        )
    image.save(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Redact portal identity details in clips.")
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.output_root.resolve()
    raw = root / "raw"
    evidence = root / "evidence"
    overlays = root / "source" / "redaction-overlays"
    overlays.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "scene-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_name = {str(scene["name"]): scene for scene in manifest["scenes"]}

    for name, spec in _SCENES.items():
        source = raw / f"{name}.webm"
        source_duration = _duration(source)
        duration = float(spec["duration"])
        regions = list(spec["regions"])
        target = raw / f"{name}-redacted.mp4"
        if regions:
            overlay = overlays / f"{name}.png"
            _overlay(overlay, regions)
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(source),
                    "-loop",
                    "1",
                    "-framerate",
                    "30",
                    "-i",
                    str(overlay),
                    "-filter_complex",
                    "[0:v][1:v]overlay=0:0",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    "18",
                    "-pix_fmt",
                    "yuv420p",
                    "-t",
                    f"{source_duration:.3f}",
                    str(target),
                ],
                check=True,
            )
            screenshot = Image.open(evidence / f"{name}.png").convert("RGBA")
            screenshot.alpha_composite(Image.open(overlay).convert("RGBA"))
            screenshot.convert("RGB").save(evidence / f"{name}.png")
        else:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(source),
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    "18",
                    "-pix_fmt",
                    "yuv420p",
                    str(target),
                ],
                check=True,
            )
        scene = by_name[name]
        scene["path"] = str(target)
        scene["start"] = round(max(0.0, source_duration - duration), 3)
        scene["duration"] = duration

    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({name: by_name[name] for name in _SCENES}, indent=2))


if __name__ == "__main__":
    main()
