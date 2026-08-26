# Engineering Scripts

This directory contains automation scripts for the azure-functions-agents-runtime repository.

## Available Scripts

### `generate_config_reference.py`

Auto-generates `docs/front-matter-reference.md` from Pydantic schema models.

**Purpose:** Keeps the API reference documentation in sync with the source code (`src/azure_functions_agents/config/schema.py`).

**Usage:**
```bash
# Generate/update the documentation
python eng/scripts/generate_config_reference.py

# Check if docs are up to date (CI mode)
python eng/scripts/generate_config_reference.py --check
```

**When to run:**
- After modifying `src/azure_functions_agents/config/schema.py`
- Automatically via pre-commit hook (if configured)
- During CI builds (verification mode)

**Integration:**
- **Pre-commit hook:** Configured in `.pre-commit-config.yaml`
- **CI pipeline:** Runs in `eng/templates/jobs/ci-tests.yml`

### `reap_aca_smoke_sandboxes.py`

Deletes leftover ACA smoke sandboxes created by the current Function App smoke
identity and its low-level CI companion tests.

**Purpose:** Cleanup safety net for the live ACA smoke job. It is label-scoped to
the BuildId-derived Function App owner/app hashes and the low-level CI
owner/app hashes, so it only ever deletes sandboxes this pipeline created. The
label selectors are imported from the test-support module
(`tests/live/aca_smoke_support.py`) rather than duplicated here.

**Usage:**
```bash
# Reap the current Function App and CI smoke sandbox families in the configured Sandbox Group
python eng/scripts/reap_aca_smoke_sandboxes.py
```

The Sandbox Group is read from the
`AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID` environment variable.

**When to run:**
- As an `always()` cleanup step after the ACA smoke job

**Integration:** The current-checkout E2E ACA job runs it with `always()`. It
deletes only the BuildId-derived Function App family and current-run low-level
family, including their snapshots, then fails if either remains.

### `aca_pr_smoke.py`

Runs the protected current-checkout ACA smoke preflight. It uses the controller
service connection to require exactly one guest UAMI with no system-assigned
identity and validates the protected group, disk, endpoint, and deployment
inputs. Guest model-only, no-state/no-group RBAC is an IaC/operations
prerequisite; the real model turn is positive access proof, not a negative
role-assignment attestation.

The retained `aca_deployed_qualification.py` and deployed suite helpers are
manual/local assets only pending the separate post-main qualification work.

### `aca_deployed_qualification.py` identity preflight

`preflight-identity` calls the deployed agent's authenticated
`/api/agents/<slug>/sandbox-preflight` route. The route performs the ARM Sandbox
Group GET and a label-scoped data-plane list with the Function's managed
identity, then reports its worker instance. The command sends a burst sized for
`AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_PREFLIGHT_WORKERS` and requires a quiet
window (`..._QUIET_SECONDS`, default 30) with no bind failures before the
qualification suite starts. A missing worker, non-200 response, or incomplete
probe fails closed; no external call is simulated.

Set `AZURE_FUNCTIONS_AGENTS_ACA_PREFLIGHT_ENABLED=1` in the deployed app and
run:

```bash
python eng/scripts/aca_deployed_qualification.py preflight-identity \
  --runtime-target python313
```
