# MI-05 integrated release candidate implementation plan

**Goal:** Produce a local, hermetic MediaInterlock release candidate with all
three components and declared adapters exercised through real runtime
boundaries, reproducible wheel and OCI artifacts, and no downstream or live
adoption.

**Architecture:** Keep components independent and connect them only through
their versioned Unix sockets and documented HTTP adapters. A disposable
integration fixture supplies pinned-shape upstream responses and a synthetic
staging media file; it drives Reconciler through Fence terminal custody into
Publisher's exact catalog/direct-play proof. OCI packaging is a minimal
provider-neutral wrapper around the existing wheel, with one explicit target
per long-running daemon and no service-manager or topology assumptions.

**Tech stack:** Python 3.14 standard library, `unittest`, local Unix sockets,
disposable HTTP servers, `python -m build`, and local Podman image builds.

## Global constraints

- Keep the fixed compatibility profile: Radarr 6.3.0.10514, Sonarr
  4.0.19.2979, Jellyfin 10.11.11, qBittorrent 5.2.3, Bazarr 1.6.0, Seerr
  3.4.1, and Prowlarr 2.5.2.5491.
- Do not fetch, push, tag, publish, install, or mutate a live stack or a
  downstream repository.
- Preserve provider-neutral contracts: no deployment paths, credentials,
  databases, trackers, or service lifecycle code enter the product.
- Add a test before each production behavior; prove an expected red failure,
  then the focused green result before broad gates.
- Build artifacts locally only. Scan artifact contents and repository sources
  without printing secret values.

### Task 1: OCI artifact definitions and executable image probes

**Files:**
- Create: `Containerfile`
- Create: `scripts/build-artifacts.py`
- Create: `tests/test_artifacts.py`
- Modify: `pyproject.toml`
- Modify: `docs/current/{architecture,operations,acceptance}.md`

**Interfaces:**
- `scripts/build-artifacts.py --output DIR --oci-engine podman` builds the
  wheel/sdist plus local `fence` and `publisher` OCI targets without pushing.
- The Containerfile accepts a closed `COMPONENT` build argument (`fence` or
  `publisher`) and has a non-root runtime entrypoint matching its target.

- [ ] Write a failing artifact-layout test that rejects an absent Containerfile,
  an unpinned component target, a root runtime user, or an image definition
  that invokes an undeclared entrypoint.
- [ ] Run `python -m unittest discover -s tests -p 'test_artifacts.py' -v` and
  confirm the absence failure.
- [ ] Add the smallest multi-stage Containerfile and bounded local build helper;
  it copies only the built wheel, installs without build tools, exposes no
  network listener, and selects exactly Fence or Publisher.
- [ ] Re-run the focused artifact test and invoke each image with `--help` via
  Podman; retain only local image IDs in test output.
- [ ] Commit the independently testable packaging boundary.

### Task 2: Full disposable runtime handoff

**Files:**
- Create: `tests/test_release_rehearsal.py`
- Modify: `tests/test_reconciler_integration.py`
- Modify: `tests/test_publisher_integration.py`
- Modify: `src/media_interlock/{fence,publisher}/cli.py` only if the real
  entrypoint lifecycle exposes a defect found by the runtime test.

**Interfaces:**
- The release rehearsal starts the real Fence and Publisher Unix daemon
  handlers with their production adapters against one disposable HTTP server.
- It invokes the Reconciler CLI, sends the durable terminal observation over
  the Fence-to-Publisher Unix contract, and requires one exact Jellyfin item,
  source and direct-play hash before delivery.

- [ ] Write a failing end-to-end test that uses synthetic media and the real
  Unix envelopes from Reconciler through Fence and Publisher; include
  configured readiness for all seven adapters and prove no request crosses an
  adapter's public boundary unexpectedly.
- [ ] Run `python -m unittest discover -s tests -p 'test_release_rehearsal.py'
  -v` and confirm it fails because the combined runtime fixture is absent.
- [ ] Add only reusable disposable-server helpers and the smallest runtime
  wiring needed for the test. Preserve the existing component-owned stores and
  never directly inspect one component's database from another.
- [ ] Extend the same rehearsal with crash/restart points before and after
  Fence terminal, Publisher custody, catalog submission, catalog observation,
  and final delivery persistence. Each uncertain external effect is observed
  before retry and never causes a blind rollback or duplicate release grab.
- [ ] Re-run the rehearsal and affected component integrations; commit the
  runtime proof.

### Task 3: Release candidate gates and permanent documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/current/{architecture,operations,acceptance,state}.md`
- Modify: `docs/current/domains/{reconciler,publisher,fence}.md` only where the
  final runtime proof changes a current behavior claim
- Modify: `docs/index.json`
- Delete: `docs/work/plans/2026-08-08-mi-05-release-candidate.md`
- Delete: `docs/work/specs/2026-08-06-media-interlock-design.md`
- Delete: `docs/work/slices/media-interlock-bootstrap.md`

**Interfaces:**
- A release candidate is a local commit with reproducible wheel/sdist and OCI
  images, clean-checkout gates, and no claim of live or downstream acceptance.

- [ ] Run targeted tests, all tests, the full synthetic runtime rehearsal,
  `python scripts/check-docs.py`, `git diff --check`, bytecode compilation,
  and a repository plus artifact secret scan.
- [ ] Export the candidate with `git archive`, rerun all tests and documentation
  checks in that clean export, rebuild wheel/sdist and both OCI targets there,
  and run all three installed entrypoints or image entrypoints with `--help`.
- [ ] Request one consolidated independent review of the stabilized candidate;
  resolve every material finding with focused red/green tests and repeat the
  affected review.
- [ ] Update permanent current documents to state the exact local release
  candidate boundary, mark MI-05 and MEDIAINTERLOCK complete, remove this plan,
  the program specification and slice catalog from the index and filesystem,
  then run the final documentation gate.
- [ ] Commit the converged MI-05 release candidate locally without creating a
  tag, pushing, publishing artifacts, or changing a downstream consumer.
