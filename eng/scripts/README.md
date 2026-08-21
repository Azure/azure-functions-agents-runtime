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
driven by the post-main ACA qualification stages in `eng/ci/e2e-tests.yml`
and remain runnable by hand.

### `aca_qualification_pipeline.py`

Supports the post-main deployment and qualification stages.

| Command | Purpose |
| --- | --- |
| `stamp` | Write `BUILD_INFO.json` into the fixture app before packaging |
| `assemble` | Build the deployable upload from the build artifact: fixture source, runtime wheel, marker, and pinned requirements |
| `preflight-deploy` | Fail fast, with a named remediation, when the deployment identity lacks rights on the target app |
| `check-build` | Verify the deployed app is running this build, on the expected Python minor |
| `sweep` | Report and clear sandboxes left by earlier runs; never fatal |

`check-build` is meaningful only because the marker is a *file inside the
deployed package*: a file can be served only if that package is genuinely on
disk, so a stale app cannot claim a build it is not running. An app setting or
resource tag could be changed without deploying anything.

`sweep` runs **before** a qualification rather than after it. ACA idle-delete
and the controller's hourly reconciliation already reclaim sandboxes, so an
end-of-run reaper would mask their failure; sweeping first turns a leftover into
a signal that automatic cleanup has stopped working. It scopes by age rather
than by build ID because it is hunting other runs' leftovers, and never deletes
a sandbox whose age cannot be determined.

See [`docs/aca-qualification-runbook.md`](../../docs/aca-qualification-runbook.md).
