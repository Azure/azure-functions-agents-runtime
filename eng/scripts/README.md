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

Deletes leftover ACA smoke sandboxes created by the CI-dedicated owner/app.

**Purpose:** Cleanup safety net for the live ACA smoke job. It is label-scoped to
the CI smoke owner/app hashes, so it only ever deletes sandboxes this pipeline
created. The label selector is imported from the test-support module
(`tests/live/aca_smoke_support.py`) rather than duplicated here.

**Usage:**
```bash
# Reap every CI smoke sandbox in the configured Sandbox Group
python eng/scripts/reap_aca_smoke_sandboxes.py
```

The Sandbox Group is read from the
`AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID` environment variable.

**When to run:**
- As an `always()` cleanup step after the ACA smoke job

**Integration:**
- **CI pipeline:** Runs in `eng/templates/official/jobs/e2e-tests.yml`

