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
`AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID` environment variable,
and its required data-plane region is read from
`AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_REGION`.

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

### `aca_qualification_pipeline.py`

Packages, deploys, and verifies the deployed ACA qualification fixture
(`tests/live/apps/aca-qualification/`). Every command is run by hand; this
repository contains no pipeline wiring for it.

| Command | Purpose |
| --- | --- |
| `install-tooling` | Install the shared Python dependencies used by qualification runs |
| `stamp` | Write `BUILD_INFO.json` into the fixture app before packaging |
| `assemble` | Build the deployable upload: fixture source, the runtime wheel, the marker, and pinned requirements |
| `deploy` | Preflight deployment rights, configure the authored region, package and deploy the staged fixture, and add best-effort portal metadata |
| `check-build` | Verify lightweight in-package build ID, commit SHA, and Python-minor provenance |

`assemble` requires exactly one runtime wheel in the build output; ambiguity is
a hard error rather than a silent "newest wins", because deploying the wrong
wheel is precisely the failure `check-build` exists to catch. Fixture
dependencies come from the single Oryx-compatible
`eng/constraints/aca-fixture-requirements.txt` export of `uv.lock`, valid for
both supported interpreter minors.

`check-build` is meaningful only because the marker is a *file inside the
deployed package*: a file can be served only if that package is genuinely on
disk, so a stale app cannot claim a build it is not running. An app setting or
resource tag could be changed without deploying anything.

The provenance is deliberately narrow. It does not cover the wheel digest, the
installed package version, a deploy-input manifest, the deployment-storage
chain, or rollback; those remain open under issue #166.

The deployed fixture is limited to N=5 diagnostics with a 120-second reclaim
policy. `aca_deployed_qualification.py` rejects N=100 before authentication or
provider work with `formal_n100_unsupported_by_qualification_fixture`. Formal
N=100 remains future human-only acceptance requiring a purpose-built workflow.
