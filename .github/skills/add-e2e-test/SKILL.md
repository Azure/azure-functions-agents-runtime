---
name: add-e2e-test
description: "Use when a new feature is being added and an end-to-end test should be created or assessed for eligibility. Covers the full E2E test lifecycle: eligibility check (supported triggers and infra), creating a dedicated app under tests/endtoend/apps/, writing targeted pytest assertions in the appropriate test_apps_*.py file, adding new dependencies to pyproject.toml, and wiring pipeline variables. Trigger when entering Phase 4 (Testing) of the add-feature lifecycle, or when asked to 'add an E2E test', 'check E2E eligibility', or 'write an end-to-end test'. Not applicable for nits, pure doc changes, or features that depend on unsupported external resources."
---

# add-e2e-test — end-to-end test lifecycle for azure-functions-agents-runtime

This skill operationalizes the E2E testing standard defined in
[`AGENTS.md`](../../../AGENTS.md) §6. Follow every step in order. Record the
eligibility outcome — a passing test or an explicit waiver — before the PR is
merged.

## When to use

Use during **Phase 4 (Testing)** of the `add-feature` lifecycle, or any time a
new feature touches behavior that can be exercised through a real Function App
host. Skip (record waiver) only when the eligibility check concludes that the
feature genuinely cannot be tested in the available infra.

---

## Step 1 — Eligibility check

Assess whether the feature can be covered by an E2E test. The E2E environment
provides:

| Resource | Available |
| --- | --- |
| Azure Functions host (`func`) | Yes |
| Azurite (blob, queue, table) | Yes |
| Foundry project (`FOUNDRY_PROJECT_ENDPOINT` / `FOUNDRY_MODEL`) | Yes |
| Any other external resource (databases, connectors, etc.) | **No** |

**Supported trigger types:** `http_trigger`, `blob_trigger`, `queue_trigger`,
`timer_trigger` (scheduled timers do not fire in CI; tests should invoke timers deterministically via the Functions admin API), and MCP tool triggers
via the `builtin_endpoints.mcp` flag.

**Decision rule:**
- If the feature's behavior can be exercised with HTTP, storage, or MCP triggers
  and requires only Azurite and/or a Foundry resource → **add an E2E test**.
- If the feature requires an external resource not listed above → **skip E2E**.
  Record the waiver in the **FRD Decisions log** (medium+ features) or the **PR
  description** (small features) with a one-line reason (e.g., "E2E waived:
  feature requires a live Service Bus namespace").

---

## Step 2 — Design the app

E2E apps are deliberately narrow: one feature, one behavior. They are **not**
samples — avoid bundling multiple capabilities.

### Required files

Create a new directory under `tests/endtoend/apps/<slug>/` where `<slug>` is
short, lowercase, and hyphenated (e.g., `queue-error-handling`). Each directory
**must** contain the following supporting files, plus one or more `*.agent.md` files:

#### `<name>.agent.md` (one or more)

One focused agent that exercises the feature. Follow standard front-matter
conventions from `docs/front-matter-spec.md`. Keep the system prompt minimal.

```markdown
---
name: My Feature Agent
description: One-sentence description of what this agent tests.
trigger:
  type: http_trigger          # or blob_trigger / queue_trigger
  args:
    route: "my-feature"
    methods: ["POST"]
    auth_level: anonymous
builtin_endpoints:
  mcp: true                   # optional: also register this agent as an MCP tool

You are a concise assistant. Reply in one short sentence.
```

#### `function_app.py`

Always exactly:

```python
from azure_functions_agents import create_function_app

app = create_function_app()
```

#### `host.json`

Copy verbatim from `tests/endtoend/apps/minimal-http/host.json`. Do not
customise unless the feature requires a specific extension setting.

```json
{
  "version": "2.0",
  "extensions": {
    "http": {
      "routePrefix": ""
    }
  },
  "logging": {
    "logLevel": {
      "default": "Information"
    }
  },
  "extensionBundle": {
    "id": "Microsoft.Azure.Functions.ExtensionBundle",
    "version": "[4.*, 5.0.0)"
  }
}
```

#### `local.settings.json`

No secrets. Always include the two standard entries. Add non-secret app
settings (feature flags, non-sensitive config) here. Secrets and infra
credentials are pipeline-only (see Step 5).

```json
{
  "IsEncrypted": false,
  "Values": {
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "AzureWebJobsStorage": "UseDevelopmentStorage=true"
  }
}
```

#### `requirements.txt`

Always install the local editable version, not the published package:

```
-e ../../../..
```

---

## Step 3 — Write the tests

### Which file to add tests to

Map the primary trigger type to the appropriate test file:

| Trigger | Test file |
| --- | --- |
| `http_trigger` | `tests/endtoend/test_apps_http.py` |
| `blob_trigger` or `queue_trigger` | `tests/endtoend/test_apps_storage.py` |
| MCP (`builtin_endpoints.mcp`) | `tests/endtoend/test_apps_mcp.py` |
| New trigger category | Create `tests/endtoend/test_apps_<trigger>.py` using an existing file as a template |

> **Free smoke test:** `tests/endtoend/test_apps_start.py` auto-discovers every
> directory under `tests/endtoend/apps/` that contains a `host.json` and runs a
> `func start` startup check. Adding a new app dir automatically opts it into
> this test — no changes to that file are needed.

### Test design principles

1. **Target one behavior.** Each test function should assert a single, specific
   outcome of the feature under test.
2. **Prefer provider-independent assertions.** Test behavior that does not
   require a live LLM call wherever possible: endpoint existence, HTTP method
   handling, JSON schema validation, error response shape, storage trigger
   invocation (logged execution), etc.
3. **Gate LLM-dependent assertions.** When a test must make a real agent call,
   guard it with the `requires_llm` skip mark already defined in each
   `test_apps_*.py` file:
   ```python
   @requires_llm
   def test_my_feature_happy_path(my_feature_host: Served) -> None:
       ...
   ```
4. **Use module-scoped fixtures.** Follow the pattern in existing files: one
   `@pytest.fixture(scope="module")` that starts the host, and multiple test
   functions that share it.
5. **Keep assertions deterministic.** Avoid assertions on LLM response content;
   assert on status codes, keys present in JSON, function index entries, or
   logged execution markers.

### Fixture pattern (HTTP example)

```python
@pytest.fixture(scope="module")
def my_feature_host() -> Iterator[Served]:
    with _serve("my-feature-app") as served:
        yield served


def test_my_feature_endpoint_is_discovered(my_feature_host: Served) -> None:
    _, endpoints = my_feature_host
    ep = find_endpoint(endpoints, "my-feature")
    assert ep is not None, "expected 'my-feature' route to be registered"


@requires_llm
def test_my_feature_returns_response(my_feature_host: Served) -> None:
    client, endpoints = my_feature_host
    ep = find_endpoint(endpoints, "my-feature")
    assert ep is not None
    resp = client.post(ep.url, json={"prompt": "hello"})
    expect_status(resp, 200)
    expect_json_keys(resp, ["session_id", "response"])
```

---

## Step 4 — New dependencies

If the E2E app or test helpers require packages not already in the `[dev]`
section of `pyproject.toml`:

1. Add the package with a pinned range to `[project.optional-dependencies] dev`
   in `pyproject.toml`.
2. Add an explanatory comment above the entry, following the style of existing
   entries (e.g., `# E2E storage-trigger tests: write blobs/queue messages to Azurite`).
3. Run `python -m pip install -U -e .[dev]` to verify the install.

Do **not** add runtime-only packages to `[dev]`; add them to the
`[project.dependencies]` section only if the runtime itself needs them.

---

## Step 5 — Environment variables and pipeline wiring

### Non-secrets (app settings)

Add non-secret variables directly to the app's `local.settings.json` under
`Values`. These are picked up automatically by `func start` during both local
and CI runs.

### Secrets and infra variables (Foundry endpoint, model name, etc.)

**Never commit secrets.** For variables that are sensitive or that differ per
environment:

1. Leave `local.settings.json` without the value (omit the key entirely, or
   note it in a comment in the agent's README if helpful for local setup).
2. Add the variable to the `env:` block in
   `eng/templates/official/jobs/e2e-tests.yml`, following the pattern of
   `FOUNDRY_PROJECT_ENDPOINT` and `FOUNDRY_MODEL`:
   ```yaml
   env:
     FOUNDRY_PROJECT_ENDPOINT: $(FOUNDRY_PROJECT_ENDPOINT)
     FOUNDRY_MODEL: $(FOUNDRY_MODEL)
     MY_NEW_VAR: $(MY_NEW_VAR)   # ← add here
   ```
3. Ensure the corresponding pipeline variable is defined (ask the team if
   you do not have access to the pipeline variable group).

---

## Step 6 — Local run

Running E2E tests locally is **recommended during feature development** but is
not a blocker if the required infra is unavailable. CI always runs them.

**Prerequisites:**
- Azure Functions Core Tools (`func`) installed and on `PATH`.
- Azurite running locally (e.g., `azurite --skipApiVersionCheck`).
- A Foundry resource, with `FOUNDRY_PROJECT_ENDPOINT` and `FOUNDRY_MODEL` set
  as environment variables (required only for `@requires_llm` tests).

**Run command (all E2E tests):**
```bash
python -m pytest -m e2e tests/endtoend -v
```

**Run only the new app's tests:**
```bash
python -m pytest -m e2e tests/endtoend/test_apps_http.py -v -k "my_feature"
```

> E2E tests are excluded from the default unit-test run by `addopts = "-m 'not e2e'"` in
> `pyproject.toml`. The E2E CI pipeline runs them explicitly with `-m e2e`.

---

## Step 7 — Verification checklist

Before marking Phase 4 complete:

- [ ] Eligibility check done; outcome recorded (test added or waiver noted in
      FRD Decisions log / PR description).
- [ ] New app directory exists under `tests/endtoend/apps/<slug>/` with all
      five required files.
- [ ] `requirements.txt` installs the local editable version (`-e ../../../..`),
      not the published package.
- [ ] `local.settings.json` contains no secrets.
- [ ] New dependencies (if any) added to `[dev]` in `pyproject.toml` with a
      comment.
- [ ] New infra/secret variables (if any) added to `e2e-tests.yml` `env:` block.
- [ ] App starts cleanly via `test_apps_start.py` (automatic: just verify the
      startup smoke test passes for the new app).
- [ ] Targeted feature tests pass locally (or waiver noted if local infra is
      unavailable).
- [ ] No unrelated apps or tests modified.

---

## Guardrails

- E2E apps are **test fixtures, not samples.** Keep them minimal and focused.
- Never add secrets to any committed file.
- Each feature's E2E app is **self-contained** — do not reuse or modify existing
  app directories for a new feature's tests.
- This skill is repo dev-tooling under `.github/skills/`; it is unrelated to the
  runtime's user-authored agent skills discovered from an app's `skills/` folder.
