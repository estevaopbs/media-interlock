# MI-04 upgrade Reconciler implementation plan

**Goal:** Deliver a fail-closed one-shot upgrade Reconciler that evaluates
typed movie and episode policies, persists attempts and checkpoints before
native Arr search effects, and reports bounded machine-readable results.

**Architecture:** Reconciler owns an isolated SQLite state and observes Arr's
ordered interactive releases without local ranking. It writes entity, selector,
size and causal-watermark intent before Fence pre-admission, posts only that
exact release back to Arr, then uses bounded Queue/History polling to bind the
real download ID/hash to Fence. Recovery observes before repeating no external
effect and absence never proves non-execution.

**Tech stack:** Python 3.14 standard library, existing strict configuration,
durable SQLite helper, canonical Unix RPC helpers, and disposable HTTP/Unix
socket tests.

## Global constraints

- Retain the fixed compatibility profile: Radarr 6.3.0.10514 and Sonarr
  4.0.19.2979 through their documented public v3 APIs.
- Reuse the existing `radarr` and `sonarr` adapter configuration; add no
  secret values or per-item manual mappings.
- Policy is typed TOML; unknown fields, ambiguous observations, unsupported
  schema, and missing required readiness inhibit work.
- A selected release POST is an attempted effect, never proof that a candidate
  was acquired, published, or delivered.
- Keep scheduling, stack lifecycle, direct database access, download-client
  control, catalog correction, and filesystem publication outside Reconciler.

### Task 1: Correct the cross-component protocol before implementation

**Files:**
- Modify: `docs/work/specs/2026-08-06-media-interlock-design.md`
- Modify: `docs/current/architecture.md`
- Modify: `docs/current/domains/{reconciler,fence}.md`
- Modify: `src/media_interlock/{contracts.py,fence/model.py,fence/service.py}`
- Test: `tests/test_contracts.py`
- Test: `tests/test_fence.py`

- [ ] Write failing contract tests for owner-bound pre-admission, exact observed
  Arr grab binding, real download ID terminal transfer, and distinct source
  categories.
- [ ] Replace locator-first admission with durable pre-admission, observed-grab,
  tag-intent and resume-intent transitions. Model absent, unknown, ambiguous and
  observed separately; recovery never treats absence as a negative result.
- [ ] Run focused contract/Fence tests and commit the protocol correction.

### Task 2: Typed configuration and causal Reconciler model

**Files:**
- Modify: `src/media_interlock/config.py`
- Create: `src/media_interlock/reconciler/model.py`
- Test: `tests/test_config.py`
- Test: `tests/test_reconciler.py`

**Interfaces:**
- Produces `ReconcilerConfig` with bounded movie and episode policy values.
- Produces immutable `UpgradePolicy`, `ReconciliationState`, and
  `SearchIntent` with no external path authority.

- [ ] Write failing tests that load a movie/episode policy, reject unknown,
  unbounded and contradictory fields, and reject a non-canonical source ID.
- [ ] Run `python -m unittest discover -s tests -p 'test_config.py' -v` and
  `python -m unittest discover -s tests -p 'test_reconciler.py' -v`; confirm
  the new assertions fail because the fields and model do not exist.
- [ ] Add frozen dataclasses and strict parsers. Model one source as
  `SearchIntent(operation_id, source, entity_id, force, checkpoint)` and
  validate UUID operation identity, `radarr`/`sonarr` source, positive public
  integer entity ID, and a bounded immutable checkpoint.
- [ ] Re-run the focused tests; then commit this independently testable policy
  foundation.

### Task 3: Durable reconciliation intent and suppression state

**Files:**
- Create: `src/media_interlock/reconciler/store.py`
- Create: `src/media_interlock/reconciler/service.py`
- Test: `tests/test_reconciler.py`

**Interfaces:**
- Consumes `UpgradePolicy`, `SearchIntent`, and a `ReconcilerStore`.
- Produces `ReconcilerService.plan(...) -> SearchIntent | None` and durable
  states `eligible`, `suppressed`, `search_intent`, and `observed`.

- [ ] Write failing state-table tests for cooldown/attempt suppression, forced
  search bypassing only policy suppression, idempotent operation identity, and
  restart retention of every uncertain intent.
- [ ] Run `python -m unittest discover -s tests -p 'test_reconciler.py' -v`;
  confirm failures are caused by missing store/service behavior.
- [ ] Implement an owner-checked private store and a service that writes the
  intent atomically before delegating any external effect. Treat a missing or
  changed checkpoint as ineligible rather than guessing.
- [ ] Re-run the focused test and commit the durable state layer.

### Task 4: Interactive Arr and hardened qBittorrent adapters

**Files:**
- Modify: `src/media_interlock/adapters/radarr.py`
- Modify: `src/media_interlock/adapters/sonarr.py`
- Test: `tests/test_radarr_adapter.py`
- Test: `tests/test_sonarr_adapter.py`

**Interfaces:**
- Produces bounded interactive `GET /api/v3/release` and exact
  `POST /api/v3/release` methods plus public Queue/History polling for Radarr
  and Sonarr. It verifies their qBittorrent clients use `InitialState=Stop`.

- [ ] Write disposable-HTTP tests for first-approved torrent selection, release
  selector identity, watermarked Queue/History correlation, configured stopped
  clients, redirect/response bounds, and all mismatch/ambiguity cases.
- [ ] Run each new adapter test; confirm it fails before methods exist.
- [ ] Revalidate the release, queue, history and download-client paths/body
  against fixed upstream public APIs; add only explicit methods and no generic
  dispatch, local ranking, locator or magnet handling.
- [ ] Re-run adapter tests and commit the adapter boundary.

### Task 5: One-shot recovery and bounded JSON CLI

**Files:**
- Create: `src/media_interlock/reconciler/cli.py`
- Create: `src/media_interlock/reconciler/__init__.py`
- Modify: `pyproject.toml`
- Test: `tests/test_reconciler_cli.py`

**Interfaces:**
- Produces `media-interlock-reconciler --config FILE [--force] [--json]`.
- Consumes only configured Radarr/Sonarr adapters and the private Reconciler
  store; it has no Unix listener and no Fence/Publisher store dependency.

- [ ] Write failing disposable-service tests proving CLI writes intent and
  watermark before pre-admission and release POST, then polls after a lost
  response without duplicating grab, tag or resume.
- [ ] Run `python -m unittest discover -s tests -p 'test_reconciler_cli.py' -v`
  and confirm the entrypoint is absent.
- [ ] Add the smallest CLI that loads typed configuration, verifies required
  adapter readiness, executes one bounded reconciliation pass, and returns
  `ok`, `inhibited`, or `unavailable` without paths, titles, secrets, or IDs in
  output fields.
- [ ] Re-run the focused CLI test, then affected Reconciler tests and commit.

### Task 6: Vertical proof, current docs, and MI-04 convergence

**Files:**
- Test: `tests/test_reconciler_integration.py`
- Modify: `docs/current/architecture.md`
- Modify: `docs/current/domains/reconciler.md`
- Modify: `docs/current/operations.md`
- Modify: `docs/current/state.md`
- Modify: `docs/index.json`
- Delete: `docs/work/plans/2026-08-08-mi-04-upgrade-reconciler.md`

- [ ] Write a failing disposable HTTP/Unix vertical test from typed CLI through
  pre-admission, exact Arr release POST, causal polling, observed stopped
  qBittorrent tag/resume, and real download-ID terminal handoff; assert no
  grab acknowledgement is interpreted as acquisition, publication or delivery.
- [ ] Run the integration test and confirm it fails before vertical wiring.
- [ ] Complete only the wiring needed for that test, preserving the durable
  recovery and suppression semantics established above.
- [ ] Run focused/affected tests, the full suite, `python scripts/check-docs.py`,
  `git diff --check`, and a repository secret scan. Request one consolidated
  independent review, resolve material findings, update permanent current
  owners, mark MI-04 complete, remove this plan, and commit the converged slice.
