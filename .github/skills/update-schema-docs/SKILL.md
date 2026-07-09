---
name: update-schema-docs
description: "Detect schema.py changes and update front-matter-spec.md and architecture.md with examples and documentation. Use when schema.py has been modified as part of a feature to ensure documentation stays synchronized with the schema source of truth. Automatically generates usage examples, updates field descriptions, and flags architecture changes that need manual review. Trigger on schema changes during feature development, or when asked to 'update docs for schema changes', 'sync schema documentation', or 'add examples for new config fields'."
---

# update-schema-docs — Schema-driven documentation synchronization

This skill ensures `docs/front-matter-spec.md` and `docs/architecture.md` stay
synchronized when `src/azure_functions_agents/config/schema.py` changes.
`docs/front-matter-reference.md` is already auto-generated (via
`eng/scripts/generate_config_reference.py`), but spec and architecture docs
require intelligent example generation and contextual updates.

## When to use

Use this skill during **Phase 5 (Docs)** of the medium+ feature pipeline when:
- `schema.py` has been modified (new fields, models, or validation logic)
- New public configuration surface was added
- After implementing config-related features
- When asked to synchronize documentation with schema changes

## What this skill does

1. **Detect changes:** Compare current `schema.py` against the last commit or
   baseline to identify added/modified fields and models.
2. **Update front-matter-spec.md:**
   - Add usage examples for new fields
   - Update existing examples if field semantics changed
   - Add validation examples for new constraints
   - Ensure Examples section coverage matches the reference
3. **Review architecture.md:**
   - Flag if new models suggest pipeline stage changes
   - Check if module map needs updates
   - Verify configuration precedence rules still hold
4. **Generate PR checklist:** List all doc changes made + items needing human review

## Prerequisites

- `schema.py` changes committed or staged
- Understanding of what the schema change enables (from the FRD)
- `eng/scripts/generate_config_reference.py` already run (reference up to date)

## Workflow

### Step 1: Analyze schema changes

1. Read the current `src/azure_functions_agents/config/schema.py`
2. Identify what changed:
   - New models (e.g., new trigger types, new capabilities)
   - New fields on existing models (e.g., GlobalConfig, AgentSpec)
   - Changed field types or validators
   - New Pydantic constraints or defaults

3. Categorize changes by impact:
   - **Agent frontmatter surface:** affects `AgentSpec` → needs examples in spec
   - **Global config surface:** affects `GlobalConfig` → needs global examples
   - **Trigger surface:** affects trigger models → needs trigger examples
   - **Structural:** new pipeline stage or module → needs architecture review

### Step 2: Update front-matter-spec.md

For each new or changed field:

1. **Locate the relevant section:**
   - Agent fields → `### Agent Front Matter` examples
   - Global fields → `### Global Configuration` examples
   - Trigger fields → `### Triggers` or `### Advanced Examples`

2. **Generate usage examples:**
   - Start from the field's type, description, and default in schema.py
   - Create realistic YAML examples showing:
     - Basic usage (most common case)
     - Edge cases (if validation is complex)
     - Integration with related fields
   - Follow existing example style (concise, commented, realistic values)

3. **Add validation examples if needed:**
   - If the field has custom validators (e.g., CRON expressions, URL patterns)
   - Show valid and invalid examples with explanations

4. **Cross-reference to the reference:**
   - Link to `front-matter-reference.md#<section>` for full API details
   - Ensure the spec focuses on *how to use*, reference on *what exists*

### Step 3: Review architecture.md

1. **Check if module map needs updates:**
   - New models in `schema.py` may indicate new modules in `config/`, `discovery/`, etc.
   - Verify the module map in §3 reflects reality
   - If discovery logic changed, update the discovery stage description

2. **Check if pipeline stages changed:**
   - New configuration may affect discover → translate → register → execute flow
   - Document new capabilities or constraints in the relevant stage

3. **Check configuration precedence:**
   - If new global/agent/env interactions exist, update §4 precedence rules
   - Ensure the precedence diagram (if present) is still accurate

### Step 4: Generate PR checklist

Create a checklist of:
- [ ] front-matter-reference.md updated (auto-generated via script)
- [ ] front-matter-spec.md examples added for: `<list new fields>`
- [ ] architecture.md module map verified
- [ ] Human review needed for: `<list architectural concerns>`
- [ ] Validation examples added for: `<list complex validators>`

## Example: Adding a new agent field

**Scenario:** `AgentSpec` gained a new field `retry_policy: RetryPolicyConfig | None = None`

**Actions:**

1. **Analyze:** New optional field on `AgentSpec`, type is a nested model
2. **Update front-matter-spec.md:**
   ```yaml
   ---
   name: Resilient Agent
   description: Agent with automatic retry logic
   retry_policy:
     max_attempts: 3
     backoff_multiplier: 2
     initial_delay_ms: 1000
   trigger:
     type: http_trigger
     args:
       route: resilient
   ---
   ```
   Add to `### Agent Front Matter` examples with commentary explaining retry behavior

3. **Review architecture.md:**
   - Check if runner stage (§3.4) mentions retry logic
   - If RetryPolicyConfig is in a new module, add to module map
   - No pipeline stage changes needed (execution detail)

4. **Checklist:**
   - [x] front-matter-reference.md updated
   - [x] front-matter-spec.md: added retry_policy example under Agent Front Matter
   - [x] architecture.md: verified runner stage mentions retry (update if not)
   - [ ] Human review: decide if retry belongs in runner or client_manager

## Example: Adding a new trigger type

**Scenario:** New `CosmosDbTrigger` model added to schema.py

**Actions:**

1. **Analyze:** New trigger model → new public surface in `trigger.type`
2. **Update front-matter-spec.md:**
   ```yaml
   ---
   name: Cosmos Change Feed Agent
   description: Responds to Cosmos DB document changes
   trigger:
     type: cosmosdb_trigger
     args:
       database_name: my-database
       container_name: my-container
       connection: CosmosDbConnection
       lease_container_name: leases
   ---
   ```
   Add to `### Triggers` section with explanation of change feed behavior

3. **Update docs/triggers.md** (if it exists):
   - Add detailed CosmosDB trigger documentation
   - Explain lease container, change feed continuation, etc.

4. **Review architecture.md:**
   - Discovery stage: verify it mentions trigger discovery from schema
   - Registration stage: confirm it handles new binding type
   - Module map: ensure `registration/triggers.py` is listed

5. **Checklist:**
   - [x] front-matter-reference.md updated (auto via TRIGGER_TYPES dict in script)
   - [x] front-matter-spec.md: added cosmosdb_trigger example
   - [x] docs/triggers.md: added detailed CosmosDB section
   - [x] architecture.md: verified trigger registration flow
   - [ ] Human review: binding extension dependency (needs host.json update?)

## Output format

When this skill completes, provide:

1. **Summary of changes made:**
   ```
   Schema changes detected:
   - Added field: AgentSpec.retry_policy (optional, nested config)
   - Modified field: GlobalConfig.timeout (type widened to int | str)
   
   Documentation updates:
   - front-matter-spec.md: Added retry_policy example (line 245)
   - front-matter-spec.md: Updated timeout example to show string format (line 89)
   - architecture.md: Verified runner stage docs (no changes needed)
   ```

2. **PR checklist** (markdown, ready to paste):
   ```markdown
   ## Documentation Updates (Schema Changes)
   
   - [x] `front-matter-reference.md` regenerated via `generate_config_reference.py`
   - [x] `front-matter-spec.md` examples added for: `retry_policy`
   - [x] `front-matter-spec.md` timeout example updated to show string support
   - [x] `architecture.md` module map verified (no changes needed)
   - [ ] **Human review:** Retry logic placement (runner vs client_manager?)
   ```

3. **Architectural concerns** (if any):
   ```
   ⚠️ Human review needed:
   - New RetryPolicyConfig model suggests retry logic in the runner.
     Does this conflict with MAF's built-in retry? Check AgentClient docs.
   - If retry is per-request, should it live in client_manager instead?
   ```

## Guardrails

- **Do not auto-generate prose sections of architecture.md** — only verify module
  map and flag inconsistencies. Design decisions require human judgment.
- **Match the existing example style** in front-matter-spec.md — concise,
  realistic, focused on common use cases.
- **Link liberally** — examples in spec should reference the reference for
  complete API details.
- **Favor explicit over exhaustive** — one good example > ten variations.
- **This skill complements, does not replace, human review** — complex
  architectural implications still need human sign-off in the FRD Decisions log.

## Integration with AGENTS.md Phase 5

This skill is invoked during **Phase 5 (Docs)** of the medium+ pipeline, after
`eng/scripts/generate_config_reference.py` has been run. The workflow becomes:

1. Run `generate_config_reference.py` → updates `front-matter-reference.md`
2. Run **this skill** → updates `front-matter-spec.md` examples + reviews `architecture.md`
3. Human reviews the PR checklist and architectural concerns
4. Commit documentation updates alongside implementation

## Files touched

- **Input (read):**
  - `src/azure_functions_agents/config/schema.py` (detect changes)
  - `docs/front-matter-reference.md` (understand new API surface)
  - `docs/frds/<NNNN>-<slug>.md` (understand intent of schema changes)

- **Output (write):**
  - `docs/front-matter-spec.md` (add/update examples)
  - `docs/triggers.md` (if trigger surface changed)

- **Output (review/flag):**
  - `docs/architecture.md` (flag inconsistencies, suggest updates)
  - `README.md` (flag if user-facing quickstart examples need updates)

## Success criteria

- Every new field in schema.py has at least one usage example in front-matter-spec.md
- Existing examples updated if field semantics changed
- Architecture.md module map matches reality
- Clear PR checklist with human review items flagged
- No "orphaned" documentation (examples for removed fields)
