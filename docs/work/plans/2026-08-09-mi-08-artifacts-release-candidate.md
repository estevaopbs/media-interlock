# MI-08 artifact and local-release-candidate implementation plan

**Goal:** Produce and prove the complete local MediaInterlock release candidate:
one reproducible wheel plus Reconciler, Fence, and Publisher OCI images, with
the full disposable two-profile synthetic rehearsal and no external publication.

**Architecture:** Keep images as thin wheel consumers. The build manifest
records local wheel and OCI identity from one clean source commit; it never
pushes, signs remotely, tags Git, or selects a downstream deployment. The
rehearsal exercises production HTTP adapters and Unix daemons against disposable
fixtures, a shared mounted mutation lease, two governed profiles, and an opaque
unrelated qBittorrent transfer.

**Tech stack:** Existing hash-locked Python build bootstrap, Podman OCI archive
builder, Containerfile multi-stage targets, standard-library disposable HTTP,
Unix socket, filesystem, and subprocess test fixtures.

## Global constraints

- Preserve all MI-06 and MI-07 source-profile, bundle, custody, and provenance
  invariants. Do not substitute an image test for the focused product gates.
- OCI images are public product artifacts, not deployment manifests: no stack
  lifecycle, reverse proxy, secrets, service manager, host path, or downstream
  migration behavior.
- Artifact production is local only. Do not push images, publish a wheel, create
  a Git tag/release, or change a downstream repository. Those actions remain
  exclusive to MI-09 after explicit authorization.
- Follow TDD. Use small static definition tests first, then disposable runtime
  tests, and reserve the full build/rehearsal for candidate stabilization.

## Task 1: Complete constrained three-image delivery

**Files:** `Containerfile`, `scripts/build-artifacts.py`, artifact tests, and
current architecture/operations/acceptance documents.

- Add the Reconciler target alongside Fence and Publisher. Every target must
  execute only its declared entrypoint, expose no TCP listener, and work as an
  arbitrary non-root numeric runtime identity without a hard-coded UID-owned
  state path.
- Apply standard OCI source, revision, version, and license labels derived from
  the exact local source. Keep the base and build requirements hash locked.
- Extend the local builder to emit exactly one wheel and three OCI archives plus
  durable manifest-digest evidence. Define bounded component version,
  configuration, and readiness probes without adding a supervisor.
- Write red tests for all target names, labels, non-root behavior, artifact
  inventory, absence of push/publish behavior, and clean source identity.

## Task 2: Expand the disposable integrated rehearsal

**Files:** release rehearsal fixtures and focused tests only.

- Run both Radarr movie and Sonarr episode profiles through the real adapters
  and daemon Unix contracts, including sealed bundles and exact catalog/direct
  play observations.
- Bind the configured shared mutation-lock inode into the exercise. Use a
  synthetic peer writer to prove contention and crash release; retain the peer
  as opaque and ensure it can mutate only its own unrelated hash.
- Assert the unrelated transfer's hash, category, tags, state, save path, and
  counters are unchanged. Cover required restart boundaries without claiming
  that fixtures are a live deployment.

## Task 3: Converge the local release candidate

- Run focused artifact and rehearsal regressions, full tests, documentation
  validation, compilation, diff/secret scans, clean-checkout artifact
  reproduction, and the local OCI rehearsal.
- Perform one consolidated review of image boundary, arbitrary-UID behavior,
  artifact identity, fixture isolation, source-profile integration, and release
  proof. Resolve material findings with focused re-review.
- Update permanent owners to the implemented candidate, mark MI-08 completed,
  remove this plan, and commit locally. MI-09 must not begin until the user
  separately authorizes its external release actions.
