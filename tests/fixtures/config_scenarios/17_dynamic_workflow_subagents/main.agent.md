---
name: Workflow Coordinator
description: Reviews pull requests and writes one report
builtin_endpoints:
  chat_api: true
workflows:
  enabled: true
  exclude:
    - private_publisher
  subagents:
    - agent: pr_status_analyst
      when: Review one pull request
    - agent: actionable_report_writer
---

Review the requested pull requests in parallel, then write one report.
