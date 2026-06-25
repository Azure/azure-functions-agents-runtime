# Daily Calendar Email

A timer-triggered agent that sends a daily email summary of your calendar meetings, powered by the **GitHub Copilot SDK** (`copilot-sdk` mode).

| Trigger | Custom Tools | Connectors | MCP Servers | Skills | Sandbox | Chat UI |
|---|---|---|---|---|---|---|
| Timer (weekdays 8:55am) | | ✅ Office 365 Outlook | ✅ Office 365 Outlook | | | ✅ |

## Features

- **Timer trigger** — runs at 8:55 AM UTC on weekdays (Monday-Friday)
- **Copilot SDK mode** — uses GitHub Copilot SDK instead of Microsoft Agent Framework
- **Office 365 Outlook connector** — reads calendar events and sends email via MCP server
- **Built-in chat UI** — manual trigger available for testing

## Prerequisites

- [Azure Developer CLI (`azd`)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
- An Azure subscription
- Office 365 account with calendar access

## Deploy

1. **Set environment variables:**

   ```bash
   cd samples/daily-calendar-email
   azd init
   azd env set AZURE_LOCATION eastus2
   azd env set TO_EMAIL <your-email@example.com>
   ```

2. **Deploy to Azure:**

   ```bash
   azd up
   ```

3. **Authorize the Office 365 connection:**

   After deployment, navigate to the Azure Portal and authorize the Office 365 Outlook connection in the Connector Gateway resource.

## Configuration

### Environment Variables

| Variable | Description |
|---|---|
| `TO_EMAIL` | Email address to send the daily calendar summary to |

### Customizing the Schedule

The agent runs at 8:55 AM UTC by default. To change the schedule, modify the `trigger.args.schedule` in [src/daily_calendar.agent.md](src/daily_calendar.agent.md).

Cron format: `minute hour day month day-of-week`
- `55 8 * * 1-5` — 8:55 AM weekdays (current)
- `0 9 * * 1-5` — 9:00 AM weekdays
- `30 7 * * *` — 7:30 AM every day

## Copilot SDK Mode

This sample demonstrates the new `copilot-sdk` mode, which uses the GitHub Copilot SDK as the underlying agent runtime instead of Microsoft Agent Framework.

To enable this mode, set `sdk_mode: copilot-sdk` in `agents.config.yaml`:

```yaml
sdk_mode: copilot-sdk
model: $FOUNDRY_MODEL
timeout: 900
```

## Local Development

1. **Install dependencies:**

   ```powershell
   cd samples/daily-calendar-email/src
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. **Configure local settings:**

   ```powershell
   Copy-Item local.settings.template.json local.settings.json
   ```

   Edit `local.settings.json` with your credentials.

3. **Run locally:**

   ```bash
   func start
   ```

4. **Trigger manually:**

   Navigate to `http://localhost:7071/daily-calendar/chat`

## License

See [LICENSE.md](../../LICENSE.md) in the repository root.
