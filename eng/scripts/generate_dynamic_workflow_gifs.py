"""Generate the animated architecture diagrams used by docs/workflows.md.

Requires Pillow:

    python -m pip install Pillow
    python eng/scripts/generate_dynamic_workflow_gifs.py
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFilter, ImageFont

WIDTH: Final = 1200
HEIGHT: Final = 675
FRAME_COUNT: Final = 60
FRAME_DURATION_MS: Final = 75

ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = ROOT / "docs" / "images" / "dynamic-workflows"

BACKGROUND = "#07111f"
PANEL = "#0d1c2f"
PANEL_LIGHT = "#132945"
TEXT = "#edf7ff"
MUTED = "#8ea8c2"
CYAN = "#27d7ff"
BLUE = "#4385ff"
PURPLE = "#a576ff"
GREEN = "#3ee6a8"
AMBER = "#ffbc57"
RED = "#ff6384"


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = ("segoeuib.ttf", "arialbd.ttf") if bold else ("segoeui.ttf", "arial.ttf")
    font_dir = Path("C:/Windows/Fonts")
    for name in names:
        path = font_dir / name
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


FONT_TITLE = _font(34, bold=True)
FONT_SUBTITLE = _font(17)
FONT_HEADING = _font(20, bold=True)
FONT_BODY = _font(16)
FONT_SMALL = _font(13)
FONT_MONO = _font(14)
FONT_METRIC = _font(25, bold=True)


def _ease(value: float) -> float:
    value = min(1.0, max(0.0, value))
    return 1 - (1 - value) ** 3


def _pulse(frame: int, period: int = 20) -> float:
    return (math.sin(frame * math.tau / period) + 1) / 2


def _base_frame() -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    for x in range(0, WIDTH, 40):
        draw.line((x, 0, x, HEIGHT), fill="#0a192a", width=1)
    for y in range(0, HEIGHT, 40):
        draw.line((0, y, WIDTH, y), fill="#0a192a", width=1)
    return image


def _text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    value: str,
    *,
    font: ImageFont.ImageFont = FONT_BODY,
    fill: str = TEXT,
    anchor: str = "la",
) -> None:
    draw.text(xy, value, font=font, fill=fill, anchor=anchor)


def _centered_lines(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    lines: list[tuple[str, ImageFont.ImageFont, str]],
) -> None:
    x1, y1, x2, y2 = box
    heights = [draw.textbbox((0, 0), value, font=font)[3] for value, font, _ in lines]
    gap = 7
    y = y1 + ((y2 - y1) - sum(heights) - gap * (len(lines) - 1)) / 2
    for (value, font, fill), height in zip(lines, heights, strict=True):
        draw.text(((x1 + x2) / 2, y), value, font=font, fill=fill, anchor="ma")
        y += height + gap


def _panel(
    image: Image.Image,
    box: tuple[int, int, int, int],
    *,
    fill: str = PANEL,
    outline: str = "#29435e",
    radius: int = 18,
    glow: str | None = None,
    glow_strength: float = 0,
) -> None:
    if glow and glow_strength > 0:
        layer = Image.new("RGBA", image.size)
        layer_draw = ImageDraw.Draw(layer)
        alpha = int(110 * glow_strength)
        layer_draw.rounded_rectangle(box, radius=radius, outline=glow + f"{alpha:02x}", width=8)
        image.alpha_composite(layer.filter(ImageFilter.GaussianBlur(12)))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=2)


def _arrow(
    draw: ImageDraw.ImageDraw,
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    fill: str = "#31506c",
    width: int = 3,
) -> None:
    draw.line((start, end), fill=fill, width=width)
    angle = math.atan2(end[1] - start[1], end[0] - start[0])
    length = 12
    spread = 0.55
    points = [
        end,
        (
            end[0] - length * math.cos(angle - spread),
            end[1] - length * math.sin(angle - spread),
        ),
        (
            end[0] - length * math.cos(angle + spread),
            end[1] - length * math.sin(angle + spread),
        ),
    ]
    draw.polygon(points, fill=fill)


def _particle(
    image: Image.Image,
    start: tuple[float, float],
    end: tuple[float, float],
    progress: float,
    color: str,
) -> None:
    x = start[0] + (end[0] - start[0]) * progress
    y = start[1] + (end[1] - start[1]) * progress
    glow = Image.new("RGBA", image.size)
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse((x - 14, y - 14, x + 14, y + 14), fill=color + "80")
    image.alpha_composite(glow.filter(ImageFilter.GaussianBlur(8)))
    draw = ImageDraw.Draw(image)
    draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=color)


def _metric(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    label: str,
    value: str,
    color: str,
) -> None:
    draw.rounded_rectangle(box, radius=13, fill="#0b192a", outline="#24415e", width=2)
    x1, y1, _, _ = box
    _text(draw, (x1 + 15, y1 + 12), label.upper(), font=FONT_SMALL, fill=MUTED)
    _text(draw, (x1 + 15, y1 + 34), value, font=FONT_METRIC, fill=color)


def _token_meter(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    fraction: float,
    *,
    color: str,
    label: str,
) -> None:
    x1, y1, x2, y2 = box
    _text(draw, (x1, y1 - 8), label, font=FONT_SMALL, fill=MUTED, anchor="ls")
    draw.rounded_rectangle(box, radius=8, fill="#091625", outline="#24415e", width=2)
    inner_width = int((x2 - x1 - 8) * min(1.0, max(0.03, fraction)))
    draw.rounded_rectangle(
        (x1 + 4, y1 + 4, x1 + 4 + inner_width, y2 - 4),
        radius=5,
        fill=color,
    )


def _save(frames: list[Image.Image], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=FRAME_DURATION_MS,
        loop=0,
        optimize=True,
        disposal=2,
    )
    print(f"Wrote {path.relative_to(ROOT)}")


def _agent_loop_frame(frame: int) -> Image.Image:
    image = _base_frame().convert("RGBA")
    draw = ImageDraw.Draw(image)
    phase_length = FRAME_COUNT / 3
    round_index = min(2, int(frame / phase_length))
    round_progress = (frame % phase_length) / phase_length
    stage = min(3, int(round_progress * 4))
    stage_progress = (round_progress * 4) % 1

    _text(draw, (54, 44), "THE STANDARD AGENT LOOP", font=FONT_TITLE)
    _text(
        draw,
        (55, 91),
        "Every tool step returns through the model — and resends a growing context.",
        font=FONT_SUBTITLE,
        fill=MUTED,
    )

    context_box = (55, 165, 350, 453)
    llm_box = (465, 205, 715, 365)
    tool_box = (850, 205, 1145, 365)
    history_box = (465, 455, 715, 580)

    active = [stage == index for index in range(4)]
    _panel(
        image,
        context_box,
        glow=CYAN,
        glow_strength=0.8 if active[0] else 0,
    )
    _panel(
        image,
        llm_box,
        fill="#10233d",
        outline=BLUE,
        glow=BLUE,
        glow_strength=0.8 if active[0] or active[3] else 0,
    )
    _panel(
        image,
        tool_box,
        glow=PURPLE,
        glow_strength=0.8 if active[1] else 0,
    )
    _panel(
        image,
        history_box,
        glow=AMBER,
        glow_strength=0.8 if active[2] else 0,
    )
    draw = ImageDraw.Draw(image)

    _text(draw, (78, 191), f"CONTEXT · LOOP {round_index + 1}", font=FONT_HEADING, fill=CYAN)
    context_rows = [
        ("Original prompt", 1),
        ("Conversation history", round_index + 1),
        ("Skill definitions", 1),
        ("Tool definitions", 1),
        ("Tool results", round_index),
    ]
    y = 236
    for label, count in context_rows:
        row_color = TEXT if count else "#526a80"
        draw.rounded_rectangle((77, y - 3, 327, y + 31), radius=8, fill="#10233a")
        draw.ellipse((89, y + 8, 99, y + 18), fill=CYAN if count else "#42566c")
        _text(draw, (110, y + 1), label, font=FONT_BODY, fill=row_color)
        if count > 1:
            _text(draw, (311, y + 1), f"x{count}", font=FONT_SMALL, fill=AMBER, anchor="ra")
        y += 40

    _centered_lines(
        draw,
        llm_box,
        [
            ("LLM", FONT_METRIC, TEXT),
            ("reason → decide", FONT_BODY, BLUE),
            ("one model round-trip", FONT_SMALL, MUTED),
        ],
    )
    _centered_lines(
        draw,
        tool_box,
        [
            ("TOOL CALL", FONT_HEADING, TEXT),
            (["search()", "inspect()", "act()"][round_index], FONT_MONO, PURPLE),
            ("raw result", FONT_SMALL, MUTED),
        ],
    )
    _centered_lines(
        draw,
        history_box,
        [
            ("APPEND TO HISTORY", FONT_HEADING, AMBER),
            ("tool call + full result", FONT_SMALL, TEXT),
        ],
    )

    arrows = [
        ((350, 285), (465, 285), CYAN),
        ((715, 285), (850, 285), PURPLE),
        ((998, 365), (715, 500), AMBER),
        ((590, 455), (590, 365), BLUE),
    ]
    for index, (start, end, color) in enumerate(arrows):
        _arrow(draw, start, end, fill="#35536f")
        if stage == index:
            _particle(image, start, end, _ease(stage_progress), color)

    _metric(draw, (55, 505, 350, 580), "Model round-trips", f"{round_index + 1} and growing", RED)
    token_fraction = (0.34, 0.65, 0.94)[round_index]
    _token_meter(
        draw,
        (795, 492, 1145, 524),
        token_fraction,
        color=RED,
        label="ILLUSTRATIVE TOKEN LOAD",
    )
    _text(
        draw,
        (795, 553),
        "Prompt + history + definitions + results",
        font=FONT_SMALL,
        fill=TEXT,
    )
    _text(
        draw,
        (795, 575),
        "are sent again on the next loop.",
        font=FONT_SMALL,
        fill=MUTED,
    )

    _text(
        draw,
        (55, 638),
        "EXPLORE",
        font=FONT_SMALL,
        fill=CYAN,
    )
    for index in range(3):
        x1 = 128 + index * 73
        fill = CYAN if index <= round_index else "#20364b"
        draw.rounded_rectangle((x1, 631, x1 + 55, 643), radius=6, fill=fill)
    _text(draw, (365, 638), "Each decision crosses the LLM boundary", font=FONT_SMALL, fill=MUTED)
    return image.convert("P", palette=Image.Palette.ADAPTIVE, colors=128)


def _dag_node(
    image: Image.Image,
    box: tuple[int, int, int, int],
    title: str,
    subtitle: str,
    *,
    color: str,
    active: float,
) -> None:
    _panel(
        image,
        box,
        fill="#10243a",
        outline=color if active > 0 else "#31506a",
        radius=13,
        glow=color,
        glow_strength=active,
    )
    draw = ImageDraw.Draw(image)
    x1, y1, x2, _ = box
    _text(draw, ((x1 + x2) // 2, y1 + 18), title, font=FONT_HEADING, anchor="ma")
    _text(draw, ((x1 + x2) // 2, y1 + 47), subtitle, font=FONT_SMALL, fill=color, anchor="ma")


def _dynamic_workflow_frame(frame: int) -> Image.Image:
    image = _base_frame().convert("RGBA")
    draw = ImageDraw.Draw(image)
    normalized = frame / FRAME_COUNT

    _text(draw, (54, 44), "DYNAMIC WORKFLOW", font=FONT_TITLE)
    _text(
        draw,
        (55, 91),
        "The model authors the DAG. Durable Functions executes it outside the agent loop.",
        font=FONT_SUBTITLE,
        fill=MUTED,
    )

    prompt_box = (45, 235, 215, 350)
    agent_box = (265, 210, 465, 375)
    durable_box = (505, 135, 965, 535)
    result_box = (1005, 235, 1165, 350)

    prompt_active = 1.0 if normalized < 0.2 else 0
    agent_active = 1.0 if normalized < 0.36 or normalized > 0.84 else 0
    _panel(image, prompt_box, glow=CYAN, glow_strength=prompt_active)
    _panel(
        image,
        agent_box,
        fill="#10233d",
        outline=BLUE,
        glow=BLUE,
        glow_strength=agent_active,
    )
    _panel(image, durable_box, fill="#0a1a2a", outline=GREEN, radius=24)
    _panel(image, result_box, glow=GREEN, glow_strength=1.0 if normalized > 0.75 else 0)
    draw = ImageDraw.Draw(image)

    _centered_lines(
        draw,
        prompt_box,
        [("PROMPT", FONT_HEADING, CYAN), ("goal + constraints", FONT_SMALL, TEXT)],
    )
    _centered_lines(
        draw,
        agent_box,
        [
            ("AGENT + LLM", FONT_HEADING, TEXT),
            ("author one DAG", FONT_BODY, BLUE),
            ("start_workflow(plan)", FONT_SMALL, MUTED),
        ],
    )
    _centered_lines(
        draw,
        result_box,
        [
            ("FINAL", FONT_HEADING, GREEN),
            ("result envelope", FONT_SMALL, TEXT),
            ("one summary turn", FONT_SMALL, MUTED),
        ],
    )

    _text(draw, (530, 162), "AZURE DURABLE FUNCTIONS", font=FONT_HEADING, fill=GREEN)
    _text(
        draw,
        (530, 191),
        "checkpointed · parallel · restart-safe",
        font=FONT_SMALL,
        fill=MUTED,
    )

    discover = (540, 235, 675, 305)
    analyze_a = (735, 220, 900, 282)
    analyze_b = (735, 305, 900, 367)
    analyze_c = (735, 390, 900, 452)
    summarize = (540, 410, 675, 480)

    plan_visible = _ease((normalized - 0.16) / 0.16)
    execution = (normalized - 0.34) / 0.4
    discover_active = 1.0 if 0 <= execution < 0.22 else 0
    parallel_active = 1.0 if 0.22 <= execution < 0.68 else 0
    summarize_active = 1.0 if 0.68 <= execution < 1.0 else 0

    if plan_visible > 0:
        _dag_node(
            image,
            discover,
            "DISCOVER",
            "activity",
            color=CYAN,
            active=discover_active,
        )
        _dag_node(
            image,
            analyze_a,
            "ANALYZE [0]",
            "activity",
            color=PURPLE,
            active=parallel_active,
        )
        _dag_node(
            image,
            analyze_b,
            "ANALYZE [1]",
            "activity",
            color=PURPLE,
            active=parallel_active,
        )
        _dag_node(
            image,
            analyze_c,
            "ANALYZE [2]",
            "activity",
            color=PURPLE,
            active=parallel_active,
        )
        _dag_node(
            image,
            summarize,
            "SUMMARIZE",
            "activity",
            color=GREEN,
            active=summarize_active,
        )
        draw = ImageDraw.Draw(image)
        edges = [
            ((675, 270), (735, 251)),
            ((675, 270), (735, 336)),
            ((675, 270), (735, 421)),
            ((735, 251), (675, 432)),
            ((735, 336), (675, 445)),
            ((735, 421), (675, 458)),
        ]
        for start, end in edges:
            _arrow(draw, start, end, fill="#31536a", width=2)

    _arrow(draw, (215, 292), (265, 292), fill="#31536a")
    _arrow(draw, (465, 292), (505, 292), fill="#31536a")
    _arrow(draw, (965, 292), (1005, 292), fill="#31536a")

    if normalized < 0.16:
        _particle(image, (215, 292), (265, 292), _ease(normalized / 0.16), CYAN)
    elif normalized < 0.34:
        _particle(
            image,
            (465, 292),
            (505, 292),
            _ease((normalized - 0.16) / 0.18),
            BLUE,
        )
    elif 0 <= execution < 0.22:
        _particle(image, (675, 270), (735, 251), _ease(execution / 0.22), CYAN)
    elif 0.22 <= execution < 0.68:
        progress = _ease((execution - 0.22) / 0.46)
        for end, offset in [((735, 251), 0.0), ((735, 336), 0.12), ((735, 421), 0.24)]:
            _particle(image, (675, 270), end, min(1, max(0, progress - offset)), PURPLE)
    elif 0.68 <= execution < 1:
        _particle(
            image,
            (735, 336),
            (675, 445),
            _ease((execution - 0.68) / 0.32),
            GREEN,
        )
    elif normalized < 0.84:
        _particle(
            image,
            (965, 292),
            (1005, 292),
            _ease((normalized - 0.74) / 0.1),
            GREEN,
        )

    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((528, 493, 940, 519), radius=10, fill="#0e2830")
    _text(
        draw,
        (734, 506),
        "INTERMEDIATE RESULTS STAY IN THE ORCHESTRATION",
        font=FONT_SMALL,
        fill=GREEN,
        anchor="mm",
    )

    _metric(draw, (45, 445, 215, 520), "LLM planning", "1 turn", BLUE)
    _metric(draw, (265, 445, 465, 520), "Agent polling", "none", GREEN)
    _token_meter(
        draw,
        (1005, 445, 1165, 477),
        0.25 + _pulse(frame, 24) * 0.04,
        color=GREEN,
        label="TOKEN LOAD",
    )
    _text(draw, (1005, 505), "No per-task model", font=FONT_SMALL, fill=TEXT)
    _text(draw, (1005, 525), "round-trips.", font=FONT_SMALL, fill=MUTED)

    _text(draw, (55, 606), "PLAN ONCE", font=FONT_SMALL, fill=BLUE)
    _arrow(draw, (150, 605), (390, 605), fill="#29465f", width=2)
    _text(draw, (420, 606), "EXECUTE DURABLY", font=FONT_SMALL, fill=GREEN)
    _arrow(draw, (575, 605), (815, 605), fill="#29465f", width=2)
    _text(draw, (845, 606), "SUMMARIZE ONCE", font=FONT_SMALL, fill=CYAN)
    return image.convert("P", palette=Image.Palette.ADAPTIVE, colors=128)


def main() -> None:
    standard_frames = [_agent_loop_frame(frame) for frame in range(FRAME_COUNT)]
    workflow_frames = [_dynamic_workflow_frame(frame) for frame in range(FRAME_COUNT)]
    _save(standard_frames, OUTPUT_DIR / "standard-agent-loop.gif")
    _save(workflow_frames, OUTPUT_DIR / "dynamic-workflow.gif")


if __name__ == "__main__":
    main()
