# Dynamic Workflow Sub Agents preview

> [!IMPORTANT]
> This is a design preview, not a runnable sample. The proposed
> `workflows.allowed_sub_agents` field and `sub_agent` task type are not
> implemented. This directory intentionally omits `host.json`,
> `function_app.py`, and deployment files.

The preview shows the proposed external contract:

- `main.agent.md` grants durable access to the `billing` specialist without
  granting chat-time `delegate_billing`;
- a Workflow plan sends the specialist one self-contained `task`;
- `billing.agent.md` runs with its own instructions and capability filters;
- successful output is available to downstream nodes as
  `${analyze_billing.result.text}`.

The `tools.allow` and `skills.allow` examples in `billing.agent.md` are comments,
not supported syntax. They make the current positive-allowlist gap visible to
reviewers without implying that this proposal has resolved it.
