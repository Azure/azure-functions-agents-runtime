---
name: Compacted Harness
description: Uses harness execution with conversation compaction
sdk: maf
mode:
  harness:
    max_context_window_tokens: 8192
    max_output_tokens: 4096
builtin_endpoints: true
---

Use harness execution with bounded model context.