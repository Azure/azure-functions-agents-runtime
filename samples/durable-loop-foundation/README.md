# Durable loop foundation sample

This private sample runs the experimental durable-loop foundation entirely
in-process with deterministic fake model and tool providers. It makes no Azure
or model-service calls.

```powershell
uv run --python 3.13 --frozen --extra dev python samples\durable-loop-foundation\run_sample.py
```

The script demonstrates a model-requested clarification, an adaptive tool step,
final commit, and a second turn that reuses the same committed session context.
