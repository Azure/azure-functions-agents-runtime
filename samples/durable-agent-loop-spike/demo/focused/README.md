# Focused Durable Chat demo

This machine-local UI drives the deployed private durable endpoints through a
loopback-only proxy. The browser receives friendly aliases and opaque local
handles; the Function key and raw run/session identifiers remain in proxy
memory.

Required environment variables:

- `DURABLE_LOOP_FUNCTION_URL` — deployed Function App origin, with no query or credentials.
- `DURABLE_LOOP_FUNCTION_KEY` — Function key used only by the proxy.
- `DTS_TASK_HUB_DASHBOARD_URL` — HTTPS DTS task-hub dashboard URL. The UI opens
  it in a named separate window; it does not construct a run deep link.

Start from the repository root:

```powershell
python samples\durable-agent-loop-spike\demo\focused\proxy.py --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765`. Runs use `retained_session` by default. **New
session** omits the prior session mapping; selecting a recent friendly session
resumes it. The scenario selector supports normal execution and the private
`model_apim_429_once` qualification path.

Recording automation should own the server object in-process, resolve the
displayed alias with `server.store.raw_run_id_for_automation(alias)`, and then
search the authenticated dashboard for that exact instance ID. There is no
browser route for this mapping.
