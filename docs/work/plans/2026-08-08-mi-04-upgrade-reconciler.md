# MI-04 upgrade Reconciler implementation plan

**Goal:** Deliver a fail-closed one-shot upgrade Reconciler that evaluates
typed movie and episode policies, persists attempts and checkpoints before
native Arr search effects, and reports bounded machine-readable results.

**Architecture:** Reconciler owns an isolated SQLite state and consumes only its
typed configuration plus public Radarr/Sonarr API clients. It derives no file or
Jellyfin identity, never releases Fence custody, and records a durable search
intent before sending one native Arr command. Recovery re-observes the exact
intent and never turns an uncertain effect into success.

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
- A native search acknowledgement is an attempted effect, never proof that a
  candidate was acquired, published, or delivered.
- Keep scheduling, stack lifecycle, direct database access, download-client
  control, catalog correction, and filesystem publication outside Reconciler.

### Task 1: Typed configuration and policy model

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

### Task 2: Durable reconciliation intent and suppression state

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

### Task 3: Narrow native Arr search adapters

**Files:**
- Modify: `src/media_interlock/adapters/radarr.py`
- Modify: `src/media_interlock/adapters/sonarr.py`
- Test: `tests/test_radarr_adapter.py`
- Test: `tests/test_sonarr_adapter.py`

**Interfaces:**
- Produces `RadarrAdapter.search_movie(movie_id: str) -> bool` using the
  documented v3 movie-search command and
  `SonarrAdapter.search_episode(episode_id: str) -> bool` using the documented
  v3 episode-search command.

- [ ] Write disposable-HTTP tests asserting the exact authenticated POST body
  and fail-closed handling of a lost response, non-2xx, malformed JSON, and an
  unexpected command response.
- [ ] Run each new adapter test; confirm it fails before methods exist.
- [ ] Revalidate the command paths/body against the fixed upstream public API
  source, then add only the two explicit command methods. Do not implement
  generic command dispatch or upstream ranking.
- [ ] Re-run adapter tests and commit the adapter boundary.

### Task 4: One-shot recovery and bounded JSON CLI

**Files:**
- Create: `src/media_interlock/reconciler/cli.py`
- Create: `src/media_interlock/reconciler/__init__.py`
- Modify: `pyproject.toml`
- Test: `tests/test_reconciler_cli.py`

**Interfaces:**
- Produces `media-interlock-reconciler --config FILE [--force] [--json]`.
- Consumes only configured Radarr/Sonarr adapters and the private Reconciler
  store; it has no Unix listener and no Fence/Publisher store dependency.

- [ ] Write failing disposable-service tests proving a CLI invocation writes an
  intent before exactly one directed Arr command, reports only bounded JSON,
  and on restart re-observes an uncertain intent without duplicating an
  unacknowledged command.
- [ ] Run `python -m unittest discover -s tests -p 'test_reconciler_cli.py' -v`
  and confirm the entrypoint is absent.
- [ ] Add the smallest CLI that loads typed configuration, verifies required
  adapter readiness, executes one bounded reconciliation pass, and returns
  `ok`, `inhibited`, or `unavailable` without paths, titles, secrets, or IDs in
  output fields.
- [ ] Re-run the focused CLI test, then affected Reconciler tests and commit.

### Task 5: Vertical proof, current docs, and MI-04 convergence

**Files:**
- Test: `tests/test_reconciler_integration.py`
- Modify: `docs/current/architecture.md`
- Modify: `docs/current/domains/reconciler.md`
- Modify: `docs/current/operations.md`
- Modify: `docs/current/state.md`
- Modify: `docs/index.json`
- Delete: `docs/work/plans/2026-08-08-mi-04-upgrade-reconciler.md`

- [ ] Write a failing disposable HTTP vertical test from typed one-shot CLI
  through a persisted intent and exact native Arr search request; assert no
  asserted search result is interpreted as acquisition, publication, or
  delivery.
- [ ] Run the integration test and confirm it fails before vertical wiring.
- [ ] Complete only the wiring needed for that test, preserving the durable
  recovery and suppression semantics established above.
- [ ] Run focused/affected tests, the full suite, `python scripts/check-docs.py`,
  `git diff --check`, and a repository secret scan. Request one consolidated
  independent review, resolve material findings, update permanent current
  owners, mark MI-04 complete, remove this plan, and commit the converged slice.
