from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def _duration(path: Path) -> float:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(completed.stdout.strip())


def main() -> None:
    parser = argparse.ArgumentParser(description="Build timed narration and music manifest.")
    parser.add_argument("--scene-manifest", type=Path, required=True)
    parser.add_argument("--narration-source", type=Path, required=True)
    parser.add_argument("--narration-dir", type=Path, required=True)
    parser.add_argument("--music", type=Path, required=True)
    parser.add_argument("--source-video", type=Path, required=True)
    parser.add_argument("--output-video", type=Path, required=True)
    parser.add_argument("--output-manifest", type=Path, required=True)
    args = parser.parse_args()

    scenes = json.loads(args.scene_manifest.read_text(encoding="utf-8"))["scenes"]
    narration = json.loads(args.narration_source.read_text(encoding="utf-8"))["segments"]
    scene_offsets: dict[str, tuple[float, float]] = {}
    cursor = 0.0
    for scene in scenes:
        name = str(scene["name"])
        duration = float(scene["duration"])
        scene_offsets[name] = (cursor, duration)
        cursor += duration

    scheduled = []
    for segment in narration:
        scene_name = str(segment["scene"])
        if scene_name not in scene_offsets:
            raise ValueError(f"narration references unknown scene: {scene_name}")
        audio_path = args.narration_dir / f"narration-{segment['id']}.wav"
        audio_duration = _duration(audio_path)
        scene_start, scene_duration = scene_offsets[scene_name]
        start = scene_start + 1.0
        if audio_duration + 1.5 > scene_duration:
            raise ValueError(
                f"narration {segment['id']} is {audio_duration:.2f}s for "
                f"{scene_name} ({scene_duration:.2f}s)"
            )
        scheduled.append(
            {
                "path": str(audio_path),
                "start_ms": round(start * 1000),
                "volume": 1.0,
            }
        )

    document = {
        "source": str(args.source_video),
        "output": str(args.output_video),
        "crf": 19,
        "overlays": [],
        "music": {
            "path": str(args.music),
            "volume": 0.13,
        },
        "narration": scheduled,
    }
    args.output_manifest.write_text(json.dumps(document, indent=2), encoding="utf-8")
    print(json.dumps({"duration": cursor, "narration_segments": len(scheduled)}))


if __name__ == "__main__":
    main()
