---
name: pr-status-analysis
description: Defines how to assess pull-request activity, checks, reviews, and merge readiness.
---

# PR status analysis

Treat a pull request as ready to merge only when it is not a draft, required
checks pass, required approvals are present, no blocking review threads remain,
and the repository reports it as mergeable.

When `last_checked_at` is provided, call out comments, reviews, and commits after
that timestamp. Always identify the evidence behind a blocker or next action.
