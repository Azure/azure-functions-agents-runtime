---
name: preview-check
description: Use this skill for a preview-check skill test. Read its reference file and return the test markers.
---

# Preview check

1. Read `references/check.txt` in this skill directory with the native `view` tool.
2. Return `SKILL_LOADED_PREVIEW_CHECK` and the exact marker from that file.
3. Do not call `make_receipt`, use a network tool, or run a shell command.

Do not guess the reference marker. If the file cannot be read, report the error.
