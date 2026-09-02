# Splitting feature work into reviewable PRs

Use this guide when a medium+ feature is too large or risky for one pull
request. Plan the split in the FRD before implementation so reviewers can
evaluate both the overall design and each delivery slice.

The goal is not the fewest changed lines. The goal is a sequence of focused,
independently reviewable changes that keeps the repository working after every
merge.

## Core rules

Each PR should:

- have one clear purpose that can be summarized in its title;
- leave the branch buildable and preserve supported behavior;
- include the tests needed to establish its own behavior;
- avoid unused public APIs, dead scaffolding, and speculative abstractions;
- state its dependencies and relationship to the overall feature;
- contain only changes needed for that delivery slice.

Prefer the smallest coherent PR, not an artificially small PR. Keep tightly
coupled code, tests, and documentation together when separating them would make
the change incomplete or harder to understand.

## Plan the split in the FRD

Add a delivery plan to the FRD's proposed design when multiple PRs are needed.
For each PR, record:

| Field | What to capture |
| --- | --- |
| Purpose | The single outcome the PR delivers |
| Scope | Modules, public surface, tests, and docs included |
| Dependencies | PRs that must merge first |
| Compatibility | How the repository remains usable after this PR |
| Review focus | The design or behavior reviewers should evaluate |

Record consequential slicing decisions in the FRD Decisions log. Architecture
review must approve the complete design, even when implementation is delivered
through several PRs.

## Choose a split strategy

### Vertical behavior slices

Prefer vertical slices when each slice can deliver a complete behavior through
the relevant pipeline stages. For example, implement one authoring capability
through discover, translate, register, and execute before adding another.

This is usually the easiest shape to review and validate because every PR has
an observable outcome.

### Enabling refactor, then behavior

Use a separate refactoring PR when existing structure blocks a clean feature
implementation. The refactor must preserve behavior and have sufficient test
coverage before the feature PR depends on it.

Do not mix broad renames, moves, or cleanup with new behavior. Reviewers should
be able to distinguish structural changes from semantic changes.

### Contract, then implementation

Split a contract or schema change from its implementation only when the first
PR is useful and safe on its own. Avoid merging unused public surface solely to
make the next diff smaller.

When consumers need time to migrate, use a compatibility-first sequence:

1. Introduce backward-compatible support and tests.
2. Migrate internal or external consumers.
3. Remove the old path in a later, explicitly approved change.

### Pipeline or module boundaries

Splitting by module can help when ownership or review expertise differs, but
only if every intermediate state is valid. Do not split mechanically by file
or pipeline stage when doing so creates a broken or untestable repository.

### Generated changes

Keep generated output with the source change that produces it unless the
generated diff is unusually large and can be reviewed mechanically. If it is
separate, document the generator, source commit, and reproducible command.

## Order dependent PRs

Treat each planned PR as a separate change under the `AGENTS.md` worktree rule.
Give every slice its own branch and worktree. Branch independent slices from
`main`; branch a dependent slice from its immediate dependency.

Use stacked PRs when later work genuinely depends on earlier unmerged work.
Keep the dependency chain shallow and make the base branch of each PR explicit.
After a dependency merges, rebase or retarget the next PR so its diff contains
only its own changes.

Avoid parallel slices that edit the same code heavily; merge conflicts can
erase the review benefit of splitting.

## Keep tests and docs aligned

- Include behavior tests in the PR that introduces or changes the behavior.
- Add characterization tests before a risky refactor when coverage is missing.
- Update architecture and authoring docs with the PR that makes their current
  description inaccurate.
- Defer only documentation that describes a capability not yet available.

Every PR must pass the gate required by `AGENTS.md`. A future PR is never the
remedy for a currently failing intermediate state.

## Avoid these splits

Do not:

- separate tests from the behavior they verify;
- divide work by arbitrary line or file counts;
- merge temporary no-op branches, dead flags, or unused APIs;
- hide required follow-up work outside the FRD or PR description;
- place unrelated cleanup into an otherwise focused feature PR;
- create a dependency chain when independent vertical slices are possible.

## Review the proposed sequence

Before implementation, confirm:

- each PR has one independently explainable outcome;
- the repository remains buildable and compatible after each merge;
- tests and relevant docs travel with the behavior;
- dependencies are necessary, explicit, and ordered;
- the final PR completes the FRD with no hidden cleanup;
- reverting any individual PR has a predictable effect.

If no sequence satisfies these conditions, keep the change together and obtain
reviewer agreement on the larger PR before implementation.

## Further reading

This repository-specific guidance is informed by Google's
[Small CLs](https://google.github.io/eng-practices/review/developer/small-cls.html)
engineering practice.
