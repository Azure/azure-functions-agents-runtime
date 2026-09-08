# Auto-Generated Documentation Setup

The `docs/front-matter-reference.md` file is now **auto-generated** from Pydantic schema models to ensure it stays in sync with the codebase.

## How It Works

The reference documentation is generated from `src/azure_functions_agents/config/schema.py` using the `eng/scripts/generate_config_reference.py` script.

### Source of Truth
- **Code:** `src/azure_functions_agents/config/schema.py` (Pydantic models)
- **Script:** `eng/scripts/generate_config_reference.py` (generator)
- **Output:** `docs/front-matter-reference.md` (auto-generated, do not edit manually)

## Usage

### Generate/Update Documentation
```bash
python eng/scripts/generate_config_reference.py
```

### Verify Documentation is Up to Date (CI Mode)
```bash
python eng/scripts/generate_config_reference.py --check
```

This exits with code 1 if the generated docs don't match the committed version.

## Integration Points

### 1. Pre-Commit Hook

Configured in `.pre-commit-config.yaml` to auto-generate docs when schema changes:

```yaml
repos:
  - repo: local
    hooks:
      - id: generate-config-reference
        name: Generate config reference docs
        entry: python eng/scripts/generate_config_reference.py
        language: system
        files: ^src/azure_functions_agents/config/schema\.py$
```

**Setup:**
```bash
# Install pre-commit (one-time)
pip install pre-commit

# Install the hooks (one-time per clone)
pre-commit install
```

**Usage:**
- Hooks run automatically on `git commit`
- Regenerates docs if `schema.py` changed
- Manually run with: `pre-commit run --all-files`

### 2. CI Pipeline

Added to `eng/templates/jobs/ci-tests.yml` to verify docs are current:

```yaml
- bash: |
    python eng/scripts/generate_config_reference.py --check
  displayName: 'Verify config reference docs are up to date'
```

This runs after lint/mypy and before tests in the CI build.

## Workflow

### When You Modify Schema

1. **Edit** `src/azure_functions_agents/config/schema.py`
2. **Regenerate docs** (automatic via pre-commit hook, or manually)
3. **Commit both files** together

### If CI Fails with "Docs Out of Date"

```bash
# Regenerate the docs
python eng/scripts/generate_config_reference.py

# Commit the updated docs
git add docs/front-matter-reference.md
git commit --amend --no-edit
git push --force-with-lease
```

## Customization

### Trigger Types

Trigger type documentation is defined in the `TRIGGER_TYPES` dictionary in `src/azure_functions_agents/config/schema.py` alongside the Pydantic models. To add/modify trigger types, edit the `TRIGGER_TYPES` dict in `schema.py`.

### Field Descriptions

Enhanced descriptions are in `*_DESCRIPTIONS` dictionaries in `src/azure_functions_agents/config/schema.py` alongside the Pydantic models. These complement the Pydantic model docstrings and add markdown formatting and links to other docs sections. To update field descriptions, edit the appropriate description dictionary in `schema.py`.

### Static Sections

The following sections are static templates in the script:
- Configuration Precedence
- Environment Variable Substitution  
- Validation Rules
- File Naming Conventions
- Additional Resources

## Benefits

✅ **Always accurate** - Docs match code by construction
✅ **Less maintenance** - No manual updates needed
✅ **CI-verified** - Pipeline catches stale docs
✅ **Developer-friendly** - Pre-commit hook automates updates
