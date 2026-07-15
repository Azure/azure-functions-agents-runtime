# Serverless Agent Portal — Mockups

Static, clickable HTML mockups for the [requirements](../requirements.md). No
backend, no real data — these exist purely to align on layout, information
hierarchy, and flow before any build.

## View them

Open [`index.html`](index.html) in any browser and use the left nav to move
between screens. From VS Code you can right-click `index.html` → **Open with
Live Preview** (or **Reveal in File Explorer** and double-click).

## Screens

| File | Screen | Requirements |
| --- | --- | --- |
| `index.html` | Dashboard (fleet overview) | FR-1–FR-3 |
| `function-apps.html` | Function Apps — a customer's many apps, each hosting many agents | Option B |
| `create-function-app.html` | Create Function App (Subscription + Resource Group + region → provisioned for you) | Option B |
| `agents.html` | Agents within a Function App + app files (host.json, requirements.txt) | FR-4–FR-6 |
| `agent-detail.html` | Agent detail (tabbed: Overview, Authoring, Tools & MCP, Triggers, Sessions, Monitoring) | FR-7–FR-12 |
| `create-agent.html` | Create agent — pick target Function App (existing or new), then author | FR-13–FR-14 |
| `playground.html` | Chat / test playground | FR-15–FR-17 |
| `source-control.html` | Source & history (blob working copy → Publish → Push to GitHub) | FR-33–FR-38 |
| `connectors.html` | Connectors hub (SRE Agent–inspired) | FR-18–FR-21 |
| `providers.html` | Model providers | FR-22–FR-24 |
| `environments.html` | Environments & app settings | FR-25–FR-27 |
| `settings.html` | Project, access, observability, preferences | FR-28–FR-30 |
| `monitoring.html` | Fleet monitoring | FR-1–FR-3, FR-12 |
| `styles.css` | Shared styling | — |

## Notes for reviewers

- The **agent-detail** tabs are interactive (click to switch panes).
- **create-agent** updates the file preview as you type the name / pick a template.
- Colors, spacing, and iconography are placeholders — feedback on layout and
  content priority is more useful than pixel styling at this stage.
- Data shown (agent names, metrics) is invented to make screens feel real.

Leave feedback inline in [`../requirements.md`](../requirements.md) or against
specific screens.
