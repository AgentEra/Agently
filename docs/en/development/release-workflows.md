---
title: Release Workflows
description: Current Agently repository automation for docs, installers, and PyPI publishing.
keywords: Agently, release, GitHub Actions, PyPI, docs
---

# Release Workflows

> Languages: **English** · [中文](../../cn/development/release-workflows.md)

This page records the current main-repository automation surfaces.

## Development Version And Branching

Agently development work should separate three concepts:

- **Current work version**: the next intended public release batch, recorded in
  `compatibility/in-development.json`. For example, `4.1.3.3` can be the
  current work version even when the larger roadmap target is `4.1.4`.
- **Version target**: the product or architecture goal the current batch moves
  toward, such as AgentTask V1 for `4.1.4`.
- **Task branches**: implementation branches named by work type and scope, not
  by the version number.

Do not create routine development branches named after the version, such as
`4.1.3.3` or `release/4.1.3.3`, unless the work is an actual release-prep
branch. Use task-scoped branch names instead:

- `feature/<scope>`
- `bug-fix/<scope>`
- `update/<scope>`
- `refactor/<scope>`

A current work version may contain several accepted task branches. Each branch
should carry one coherent feature, fix, update, or refactor; after acceptance it
is merged into `dev`. The version release candidate is then assembled from
`dev`, with release notes, compatibility manifests, examples, docs, and
companion repositories reconciled against the current work version.

When starting new version-development work after the previous public version has
already been released, treat `compatibility/in-development.json` as the explicit
current work version while no release-prep or release-promotion action has
started for a newer batch. Do not ask the maintainer to restate that version on
each task. If the in-development manifest is missing, stale, or intentionally
being replaced, stop and ask for the current work version. If the intended task
branch is not explicitly specified, stop and ask for the branch before changing
code, specs, docs, examples, or compatibility metadata.

When a branch implements a slice of a larger roadmap target, record both facts:
the feature spec should describe the larger target, while
`compatibility/in-development.json` should describe the current work version
that will publish the accepted slice.

## Managed Companion Repositories

Agently coordinates repositories by role rather than treating every companion
as the same dependency type:

| Repository | Role | Release gate |
|---|---|---|
| Agently-Stage | Required runtime dependency | Publish and verify Stage before raising Agently's minimum version |
| Agently-Skills | Coding-agent guidance | Update the affected Skill, catalog, bundle, compatibility support, and Codex-local copy with user-visible behavior |
| Agently-Devtools | Optional observation companion | Review when RuntimeEvent, ObservationEvent, or run-lineage contracts change |

Agently-Stage and Agently versions are independent. Agently records a compatible
Stage range in `pyproject.toml` and `runtime_support.agently_stage`; the lock
records the exact artifact used by release validation.

When a Stage change is required, use this order:

1. validate Stage across its supported Python matrix and build the candidate wheel;
2. run the affected Agently integration and full suites against that wheel;
3. publish the immutable Stage tag, verify PyPI, and install it in a clean environment;
4. update Agently's minimum Stage version, lock, compatibility manifest, tests, and docs;
5. rerun Agently validation with Stage resolved from PyPI rather than a local path.

A tag or local wheel is candidate evidence, not publication evidence. Do not
merge an Agently minimum-version increase while the required Stage version is
unavailable from PyPI.

## Documentation

GitHub Pages uses the `docs/` directory from the `main` branch.

The `docs/_config.yml` file records the Pages/Jekyll settings for this branch source, including the site base URL and relative Markdown link handling.

The old `docs` branch is retired and should not be used as a documentation source. Do not add workflows or release steps that publish documentation from the retired branch.

For every framework release, update and review these public documentation
surfaces before merging the release PR:

- final release notes in both languages, for example
  `docs/en/development/release-notes-<version>.md` and
  `docs/cn/development/release-notes-<version>.md`
- the root `README.md`
- the root `README_CN.md`

The README files should keep their existing structure, but they must reflect the
release's final product positioning, current version number, recommended
capability entrypoints, example directories, companion compatibility line, and
business value. Do not leave README updates as a post-release marketing task:
the root README is also the PyPI long description because `[project].readme`
points at `README.md`.

Before opening the release PR, compare release notes with both README files and
check for stale version numbers, outdated companion protocol names, removed
examples, deprecated recommended APIs, and mismatched business claims.

Before merging the release PR, do a separate human release-note review. This is
not a mechanical checklist. The reviewer should confirm that the release notes
describe the final product story, code shape, examples, and business value that
the release is actually shipping. If this review finds wording gaps, stale
claims, missing README updates, confusing examples, or small API/documentation
adjustments, make those changes in the release PR, rerun the relevant
validation, and only then merge. Do not merge first and treat release-note fixes
as follow-up marketing work.

## Upgrade Information Format

Every user-facing upgrade explanation must include both sample code and a core
changes table. This applies to release notes, release PR bodies, issue closeout
comments, maintainer-facing upgrade summaries, and docs pages that explain a
new or changed public surface.

The sample code must show the recommended current usage shape. If an API shape
changed, include a before/after snippet. If the upgrade has no callable API,
show the relevant configuration, CLI command, manifest entry, or workflow YAML
instead. Do not use pseudo-code unless it is explicitly marked as conceptual.

The core changes table must include at least these columns:

| Area | What changed | Recommended usage | Compatibility / risk | Evidence |
|---|---|---|---|---|
| Public API / docs / runtime area | User-visible behavior or contract | Method, config, command, or example path | Additive, breaking, policy-gated, deferred, or no-op | Tests, examples, specs, compatibility metadata, or companion validation |

When a claimed slice is only partially implemented, the table must include a
deferred row with the remaining scope and the spec or issue that owns it. Do not
hide deferred work in prose after the table.

## Acceptance Argument

Before recommending a release, write a coverage-first acceptance argument for
each user-visible feature slice. Start from the target contract, not from the
examples or tests that already exist.

The release reviewer should first list the required behavior from the roadmap,
spec, issue acceptance criteria, compatibility manifest, docs, and example
rules. Then map each requirement to the evidence that proves it:

- scenario examples with output from an explicitly configured online model
  (DeepSeek by default) and stable `Expected key output`; local Ollama may be
  used for supplementary diagnostics but is not part of the default test suite
  or final release evidence
- deterministic tests for compatibility, stream/meta shape, route lifecycle,
  budget accounting, errors, and workspace records
- protocol/type tests for public contracts and dependency direction
- docs, compatibility manifests, spec reconciliation, and companion guidance
- DevTools or other companion validation when runtime events, observation
  payloads, lineage, or companion protocols changed
- public typing and IDE metadata checks when public APIs, data contracts,
  stream payloads, callbacks, handlers, facades, or companion package surfaces
  changed

Examples prove that the release solves a real scenario. They must not be the
only proof for compatibility behavior, protocol boundaries, route lifecycle,
error semantics, budget counting, or internal architecture ownership. If a
requirement has no evidence, either implement it before release, mark it as an
explicit deferral in the relevant spec and release notes, or remove the release
claim.

The release PR body or review notes should include this matrix or a concise
link to it. Do not accept a release by pointing directly at existing examples,
tests, or closed issues without first checking that those evidence sources cover
the target contract.

## Public Typing And IDE Support

Public typing completeness is a release gate. A framework release must keep
Pylance/pyright-compatible IDE hints useful for both the source checkout and the
installed package.

Before release:

- run `pyright` over `agently/`, `tests/`, and `examples/` with the release
  candidate interpreter before running `pytest`
- audit changed public surfaces for missing annotations and unjustified broad
  `Any`
- run the public typing allowlist check; `compatibility/public-typing-allowlist.json`
  is only for documented `Any` compatibility boundaries, not for listing public
  methods
- confirm `agently/py.typed` exists in the source tree and is included in the
  built or installed package
- run a Pylance-equivalent `pyright` smoke from outside the repository source
  path against an installed candidate package, importing representative public
  root APIs and changed public surfaces
- record commands, interpreter, package source, and result in the release PR
  body or review notes

If the release also publishes or recommends a companion Python package such as
`agently-devtools`, run the same source and installed-package typing/IDE smoke
for that companion before declaring the release line aligned.

## Foundation Example Effect Gate

Foundation-layer capabilities are release-critical framework substrate, not
application-level use cases. Examples include ModelRequest/ModelResponse,
TriggerFlow, Dynamic Task/TaskDAG, ActionRuntime, ExecutionResource,
TaskContext/ContextReader, TaskWorkspace, RecordStore, RuntimeEvent/EventCenter,
and provider protocols. When a
release touches or claims one of these substrate capabilities, tests are not
enough by themselves: the release reviewer must also run the corresponding core
example under `examples/` and confirm the real effect still works through the
recommended public API.

AgentExecution, AgentTask, Skills workflows, and business examples can be
release use-case checks, but they are not Foundation checks by themselves. Map
them to this gate only when the release also touches a Foundation substrate they
depend on, such as ModelRequest result materialization, TriggerFlow lifecycle,
or Dynamic Task DAG execution.

For each affected Foundation capability:

- name the protected Foundation capability and the user-visible effect
- list the runnable core example that proves that effect, adding one before
  release when none exists
- run the example against the release candidate after pyright and pytest
- use an explicitly configured real online model, DeepSeek by default, when the
  effect includes model-owned planning, routing, verification, or response
  generation; local Ollama is optional diagnostic evidence only
- record command, environment, and stable key output, artifact, stream,
  metadata, or side-effect evidence in the release PR body or review notes

This gate fails closed. Do not release by arguing that unit tests passed if the
Foundation example effect check is missing or broken. Either fix the example or
the release candidate, remove the release claim, explicitly defer the affected
Foundation capability in specs and release notes, or record a maintainer waiver
with the residual risk.

## Pinned Developer Usage Example Gate

Some examples are release-pinned developer usage checks. They protect the
recommended ways developers are expected to build with Agently, not only the
framework substrate. The pinned set should be maintained across releases and
should not drift just because an internal implementation changed.
Pinned scripts live under `examples/release_pinned_usage/` and are selected by
`examples/release_pinned_usage/pinned_usage_manifest.json`.

Before recommending a release:

- identify the pinned developer usage examples affected by the release claims,
  README guidance, release notes, compatibility manifest, or changed public API
- run those scripts against the release candidate, using an explicitly
  configured real online model, DeepSeek by default, when the example contains
  model-owned routing, planning, verification, evaluation, or response
  generation; local Ollama is optional diagnostic evidence only
- run release examples with an explicit all-allowed test capability policy when
  they may exercise Skills, Actions, TaskWorkspace, RecordStore, network, Python, shell, HTTP,
  browse, search, or MCP capability loading; do not confuse this release-test
  posture with Agently's default fail-closed runtime permission posture
- record the command, interpreter, provider/model, environment assumptions,
  stable key output, and any artifact, stream, metadata, or side-effect evidence
- treat a failure as a release blocker when the recommended usage should still
  work
- if a failure shows that the recommended usage itself must change, stop before
  rewriting the example and ask the maintainer whether this release should
  accept that usage update

Live model calls are explicit release experiments, not default `pytest`
dependencies. The deterministic suite must not require a local Ollama service
or a preinstalled local model. Local Ollama may still be used for developer
diagnostics, but final release evidence should use the declared online
provider/model unless the maintainer records a one-off waiver and limitations.

The release PR body or review notes should list the pinned examples that were
run, the reason each one was included, and whether the result preserved the
existing developer-facing usage shape. Do not silently replace a pinned example
with a new pattern only to make the release gate pass.

## Release PR Body

The release PR from `dev` to `main` must include enough information for a
reviewer to accept or block the release without reconstructing the work from
commit history.

At minimum, include:

- release version, release level, and current PyPI published version
- change summary grouped by user-visible capability
- coverage-first acceptance argument or matrix
- validation commands and results, including any skipped or failed checks
- public typing and Pylance/pyright IDE metadata checks for source and installed
  package candidates
- Foundation example effect checks for touched Foundation-layer capabilities
- pinned developer usage example checks for affected recommended usage shapes
- clean install smoke environment and result
- compatibility manifest updates and companion repository status
- DevTools version or protocol recommendation when runtime events,
  observation payloads, lineage, or DevTools code changed
- issue closure or follow-up issue status
- known deferred scope and residual release risk
- post-merge companion promotion or publish steps

Do not use a terse PR body that only says "release" or only lists commits.
Release PRs are part of the durable acceptance record.

## Desktop installers

Desktop installers are not part of the current main-repository release flow.

Do not keep or re-enable desktop installer workflows only to support the retired `docs` branch flow.

## PyPI publishing

The main PyPI automation is `Publish on version change`. It runs on pushes to
`main` that touch `pyproject.toml`. Its validation matrix installs the locked
dependencies, including Agently-Stage from PyPI, and runs typing and tests
before the publish job. The publish job then detects whether the Agently package
version changed and publishes with Poetry only when it changed.

The PyPI project list page shows the package metadata `Summary`, which comes from `[project].description` in `pyproject.toml`. The full project page renders the README from `[project].readme`.

When preparing a release:

- keep `[project].description` non-empty and concise
- keep `[project].readme` pointed at `README.md`
- do not expect PyPI metadata for an existing uploaded version to update in place; publish a new version when metadata must change publicly

## Clean install smoke

During release testing, validate the package in a brand-new empty environment
created with `conda` or `uv`. Install the release candidate package and only the
necessary runtime dependencies for a minimal script. Do not reuse a developer
environment where optional packages may already be cached.

The smoke script must verify two things:

- A basic installed-package startup path works without optional dependencies
  that are explicitly protected by `agently.utils.LazyImport`.
- At least one LazyImport-protected missing dependency path is triggered on
  purpose, and the structured `LazyImportDependencyError` explains the missing
  dependency and install command correctly. An install prompt should appear only
  when the caller declares `auto_install=True` at the `LazyImport` call site in
  an interactive smoke.

Optional integrations such as DevTools, ChromaDB, FastMCP, SQLModel, Playwright,
or other provider-specific packages should not be installed unless the smoke is
testing that integration directly. Their absence must not break ordinary Agently
imports or minimal Agent/TriggerFlow startup.
