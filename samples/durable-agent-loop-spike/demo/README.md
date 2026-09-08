# Durable Agent Loop live control room

This local UI calls the deployed Function endpoints through a loopback-only
Python proxy. The Function key remains in the proxy process environment and is
never returned to browser JavaScript, browser storage, source files, or logs.
The UI renders only bounded control metadata and hashed sandbox aliases.

```powershell
$env:DURABLE_LOOP_FUNCTION_KEY = '<function-key>'
uv run --extra aca_sandbox python `
  samples\durable-agent-loop-spike\demo\control_room.py
```

Open `http://127.0.0.1:8765`. The Azure CLI credential is used only to read the
live ACA Sandbox Group inventory.
