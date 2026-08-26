---
frd: 0008
title: GitHub Models provider
status: Finalized
author: swapnil
created: 2026-08-26
updated: 2026-08-26
issues: []
pull_requests: []
branch: swapnil/supportGHModel
---

# FRD 0008 — GitHub Models provider

## 1. Summary

Add GitHub Models as a supported Microsoft Agent Framework inference provider. Authors can
select `AZURE_FUNCTIONS_AGENTS_PROVIDER=github` or allow dedicated-token auto-detection,
authenticate with a GitHub token, and run any GitHub Models model exposed through its
OpenAI-compatible inference endpoint without changing agent files or runtime registration.

## 2. Motivation / problem

The runtime supports Microsoft Foundry, Azure OpenAI, and OpenAI, but developers using
GitHub Models must currently write and inject a custom `ClientManager`. This blocks the
normal markdown-first quickstart and makes local prototypes and GitHub Actions workflows
carry provider code unrelated to the agent. GitHub Models exposes an OpenAI-compatible API,
and the installed MAF `OpenAIChatClient` supports a custom `base_url`, so the runtime can
support it without another SDK dependency.

This capability was verified against the repository-pinned `agent-framework-openai==1.3.*`:
`OpenAIChatClient(model, *, api_key=..., base_url=...)` is part of its public constructor.
The existing dependency floor therefore supports this design without a version change.

## 3. Goals / Non-goals

**Goals**

- Add `github` as a valid `AZURE_FUNCTIONS_AGENTS_PROVIDER` value.
- Build GitHub Models clients through MAF's existing `OpenAIChatClient`.
- Authenticate with `GITHUB_MODELS_TOKEN`, falling back to `GITHUB_TOKEN` only when the
  provider is explicitly `github`.
- Auto-detect GitHub Models from non-blank `GITHUB_MODELS_TOKEN` after all existing provider
  signals, preserving existing selection precedence.
- Resolve models as requested model, then `GITHUB_MODELS_MODEL`, then
  `AZURE_FUNCTIONS_AGENTS_MODEL`, then `openai/gpt-4.1-mini`.
- Default to `https://models.github.ai/inference`, with optional
  `GITHUB_MODELS_ENDPOINT` override for testing and future-compatible gateways.
- Report `provider=github` and the resolved model through existing inference-target
  observability metadata.
- Include a minimal local-first chat sample using the built-in browser and HTTP endpoints.

**Non-goals**

- Add GitHub repository, GitHub App, OAuth, or GitHub API integration.
- Provision tokens, models, billing, rate limits, or GitHub organization policy.
- Auto-detect from generic `GITHUB_TOKEN`, which may exist for unrelated GitHub operations.
- Add model catalog discovery or model capability validation.
- Change `.agent.md`, `agents.config.yaml`, triggers, registration, or runtime schemas.
- Add fallback between providers after an inference error.
- Provision GitHub tokens or Azure deployment infrastructure for the chat sample.

## 4. Proposed design

| Pipeline stage | Module(s) | Change |
| --- | --- | --- |
| discover | `discovery/*` | No change. |
| translate | `config/*` | No change; model strings already pass through. |
| register | `registration/*` | No change. |
| execute | `client_manager.py` | Select `github`, resolve its model, and construct an `OpenAIChatClient` with token and base URL. |

### Provider selection

Explicit `AZURE_FUNCTIONS_AGENTS_PROVIDER` remains authoritative. Accepted values become
`openai`, `azure_openai`, `foundry`, and `github`. Without an explicit provider, selection
remains first-match and adds GitHub Models last:

1. `AZURE_OPENAI_ENDPOINT` → `azure_openai`
2. `FOUNDRY_PROJECT_ENDPOINT` → `foundry`
3. `OPENAI_API_KEY` → `openai`
4. `GITHUB_MODELS_TOKEN` → `github`

`GITHUB_TOKEN` never participates in auto-detection. This avoids changing behavior in
GitHub Actions or apps that use a GitHub token for source operations while configuring a
different inference provider.

### Model and endpoint resolution

For `github`, model resolution is:

1. requested model from the resolved agent;
2. non-blank `GITHUB_MODELS_MODEL`;
3. non-blank `AZURE_FUNCTIONS_AGENTS_MODEL`;
4. `openai/gpt-4.1-mini`.

The client receives `base_url` from non-blank `GITHUB_MODELS_ENDPOINT`, otherwise
`https://models.github.ai/inference`. The value is passed to MAF without string-building
request paths in this runtime.

### Authentication and errors

The builder reads a non-blank `GITHUB_MODELS_TOKEN`, then falls back to non-blank
`GITHUB_TOKEN`. If neither exists, explicit `provider=github` raises an actionable
`RuntimeError` before constructing the client:
`AZURE_FUNCTIONS_AGENTS_PROVIDER=github requires GITHUB_MODELS_TOKEN or GITHUB_TOKEN to be set.`
Tokens are passed only as `api_key` and are never logged or included in `InferenceTarget`.

The unknown-provider error lists `openai, azure_openai, foundry, github`. The no-provider
error retains the existing provider guidance, adds `GITHUB_MODELS_TOKEN (GitHub Models)`,
and lists all four values in its explicit-override guidance.

MAF/provider authentication, authorization, throttling, model-not-found, and content-policy
errors continue through the existing runner error path. The runtime does not translate
GitHub-specific response bodies in this increment.

### Authoring / API surface

No authoring file or HTTP endpoint changes. The public surface is environment-only:

| Variable | Required | Meaning |
| --- | --- | --- |
| `AZURE_FUNCTIONS_AGENTS_PROVIDER=github` | No when `GITHUB_MODELS_TOKEN` is set; otherwise yes | Explicit provider selection. |
| `GITHUB_MODELS_TOKEN` | Preferred | Dedicated GitHub Models token; also enables auto-detection. |
| `GITHUB_TOKEN` | Fallback for explicit provider only | Standard GitHub token used when the dedicated token is absent. |
| `GITHUB_MODELS_MODEL` | No | Provider-specific model, e.g. `openai/gpt-4.1-mini`. |
| `GITHUB_MODELS_ENDPOINT` | No | OpenAI-compatible base URL override. |

### Compatibility

Existing explicit provider values and auto-detection retain their behavior and precedence.
An environment containing only `GITHUB_TOKEN` still reports no provider unless
`AZURE_FUNCTIONS_AGENTS_PROVIDER=github` is set. Existing `OPENAI_API_KEY` continues to
select OpenAI even when `GITHUB_MODELS_TOKEN` is also present. No dependency or schema
migration is required.

## 5. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Authentication variables | `GITHUB_TOKEN` only / dedicated only / dedicated then generic | `GITHUB_MODELS_TOKEN`, then `GITHUB_TOKEN` | Human | 2026-08-26 |
| 2 | Provider selection | explicit only / dedicated-token auto-detect / either-token auto-detect | Explicit `github` or auto-detect dedicated token last | Human | 2026-08-26 |
| 3 | Model resolution | runtime-only / provider-specific fallback / required | `GITHUB_MODELS_MODEL`, runtime model, then `openai/gpt-4.1-mini` | Human | 2026-08-26 |
| 4 | Endpoint | fixed / overridable | Default public endpoint with `GITHUB_MODELS_ENDPOINT` override | Human | 2026-08-26 |
| 5 | Client implementation | new GitHub SDK / direct HTTP / MAF OpenAI-compatible client | Reuse the verified `OpenAIChatClient(base_url=...)` constructor in pinned `agent-framework-openai==1.3.*`; no dependency added | Agent | 2026-08-26 |
| 6 | Generic token auto-detection | include `GITHUB_TOKEN` / exclude it | Exclude to prevent accidental provider selection | Agent | 2026-08-26 |
| 7 | Introductory sample scope | Azure-provisioned / local-first | Local-first built-in chat app with no Azure AI resources | Agent | 2026-08-26 |

## 6. Test plan

- [x] `tests/test_client_manager.py`: explicit provider selects the GitHub builder and emits
  `InferenceTarget(provider="github", model=...)`.
- [x] Requested model, `GITHUB_MODELS_MODEL`, runtime model, and default precedence.
- [x] Dedicated token wins over `GITHUB_TOKEN`; generic token supports explicit provider;
  missing both fails before client construction.
- [x] Default and overridden base URL are passed unchanged to `OpenAIChatClient`.
- [x] Auto-detection selects GitHub Models only from `GITHUB_MODELS_TOKEN`, after all existing
  provider signals; blank values behave as unset.
- [x] Unknown-provider and no-provider messages list GitHub Models configuration.
- [x] `tests/test_github_models_sample.py`: sample composes its built-in chat endpoints and
  declares the dedicated GitHub Models settings.
- [x] `tests/endtoend/test_github_models_agentic.py`: when `GITHUB_MODELS_TOKEN` is supplied,
  boot the GitHub Models sample and verify one live instruction-following response.
- [x] `eng/templates/official/jobs/e2e-tests.yml`: run the live smoke test in a separate
  conditional step when the secure pipeline variable `GITHUB_MODELS_TOKEN` is configured.
- [x] Full Ruff, mypy, and pytest gate.

No config fixture is needed because the authoring schema is unchanged.

## 7. Docs impact

- [x] `docs/architecture.md` — update the `client_manager.py` module-map description.
- [x] `README.md` — add provider table entry, precedence, and configuration example.
- [x] `samples/README.md` — add GitHub Models to shared provider guidance.
- [x] `samples/github-models-chat/` — add a minimal local-first chat sample.
- [x] `docs/frds/README.md` — index FRD 0008.
- [x] `docs/front-matter-spec.md` — no change.
- [x] `docs/triggers.md` — no change.

## 8. Status & sign-off

- **Architecture review (phase 2):** First independent review requested confirmation of
  MAF `base_url` support and exact provider error contracts. Both were recorded, and the
  final independent architecture gate returned **APPROVE** with no blockers.
- **Human sign-off:** swapnil, 2026-08-26. Approved for implementation.