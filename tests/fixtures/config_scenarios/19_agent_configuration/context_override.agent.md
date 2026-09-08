---
name: Context Override
description: Overrides one nested MAF setting and inherits the output limit
agent_configuration:
  agent_framework:
    compaction:
      max_context_window_tokens: 16384
builtin_endpoints: true
---

Use a larger context window while inheriting the output limit.