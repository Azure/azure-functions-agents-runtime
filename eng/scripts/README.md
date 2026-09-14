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

### `validate_release_notes.py`

Validates the structure and machine-readable metadata in `docs/releases.md` without network
access.

```bash
# Validate docs/releases.md
python eng/scripts/validate_release_notes.py

# Validate another release-note document
python eng/scripts/validate_release_notes.py --path path/to/releases.md
```

The command returns `0` on success, `1` for validation findings, `2` for invalid command usage,
and `3` for an unexpected internal error. Findings include the source path and line number.

An Unreleased entry uses a single-line strict JSON comment immediately followed by one top-level
Markdown bullet:

```markdown
<!-- release-note: {"id":"frd-0009-release-summaries","category":"maintenance-documentation","prs":[234],"frd":"0009"} -->
- **Customer-facing release summaries.** Feature work records customer outcomes while implementation context is current.
```

The metadata contract is:

- `id`: required lowercase semantic slug, unique across the document;
- `category`: required; one of `feature`, `improvement`, `bug-fix`, `security`,
  `compatibility-deprecation`, or `maintenance-documentation`;
- `prs`: required non-empty array of unique positive PR numbers; and
- `frd`: optional four-digit string.

Unknown or duplicate JSON keys are invalid. Entries must be under the matching canonical category
heading. Unreleased content cannot contain package versions, release dates, release/PyPI/comparison
URLs, or placeholders. Compatibility entries must state required customer action or explicitly say
that no action is required. Published sections retain metadata when entries are promoted, while
historical entries without metadata remain valid.

The validator also checks that at-a-glance rows and published sections are unique, paired, and
newest-first; that section versions match their PyPI URLs; and that comparison links contain two
explicit Git tags. It does not judge customer value, prose accuracy, security wording, or authorize
published-history corrections. Those remain human-review responsibilities.

### `generate_dynamic_workflow_gifs.py`

Generates the animated standard-agent-loop and Dynamic Workflow architecture
diagrams embedded in `docs/workflows.md`.

```bash
python -m pip install Pillow
python eng/scripts/generate_dynamic_workflow_gifs.py
```
