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

### `generate_dynamic_workflow_gifs.py`

Generates the animated standard-agent-loop and Dynamic Workflow architecture
diagrams embedded in `docs/workflows.md`.

```bash
python -m pip install Pillow
python eng/scripts/generate_dynamic_workflow_gifs.py
```
