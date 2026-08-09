# MI-06 source profiles and shared qBittorrent implementation plan

**Goal:** Make Fence safely govern Radarr and Sonarr on a qBittorrent instance
that also contains unrelated transfers, with exact per-source paths,
observer-first external-grab adoption, physical headroom, and bounded
quiescence while coordinating qBittorrent mutations with a neutral
deployment-supplied file lease.

**Architecture:** Parse one strict source-profile table and project only the
fields owned by each component. Fence identifies governed work by durable
reservation plus owner tag and exact hash, never by category alone. Reconciler
keeps its reservation-first path; normal Arr grabs enter through a polling
observer and remain stopped until adoption. Capacity combines the existing
logical ledger with a read-only `statvfs` observation. Quiescence is a local
Fence state transition, not stack lifecycle. Every qBittorrent mutation also
uses the provider-neutral `shared-qbittorrent-mutation/v1` advisory lease; this
mechanism serializes peer writers without making Fence authoritative for their
domains.

**Tech stack:** Python 3.14 standard library, strict TOML, SQLite, canonical
JSON over Unix sockets, public Radarr/Sonarr/qBittorrent APIs, `os.statvfs`, and
`unittest` disposable adapters.

## Global constraints

- Work only in the MediaInterlock repository. Do not edit or run a downstream
  deployment.
- Freeze the latest mutually compatible stable upstream versions from primary
  sources before changing adapter behavior; record exact pins in current
  compatibility owners.
- No Lidarr/music adapter, category name, mount, credential, or policy enters
  product code or examples. Tests use a synthetic unrelated category.
- Do not add a second qBittorrent, proxy, TCP control plane, scheduler, systemd
  unit, backup feature, or deployment lifecycle code.
- Do not add a music/Lidarr identity, peer registry, policy callback, or
  MediaInterlock-owned lock daemon. The shared lease is one deployment-supplied
  regular file and bounded `flock` protocol only.
- Every production behavior begins with a focused failing test and observed red
  result. Expand only to affected regressions until the candidate stabilizes.
- A qBittorrent effect names one exact ledger-owned hash after a durable intent.
  No `all` selector, category-wide mutation, inferred absence, or blind retry.
- Preserve current Reconciler ordering, custody contracts, catalog proof,
  last-known-good retention, secret redaction, and store ownership.
- No fetch, push, tag, package publication, release creation, or live mutation.

### Task 1: Freeze compatibility and add source-profile projections

**Files:**
- Modify: `src/media_interlock/config.py`
- Modify: `src/media_interlock/{fence,publisher,reconciler}/cli.py`
- Modify: `tests/test_config.py`
- Modify: affected CLI tests
- Modify: `docs/current/{state,architecture,operations}.md`

**Interfaces:**
- `ProductConfig.sources` contains exactly one typed `radarr` movie profile and
  one typed `sonarr` episode profile when the integrated system is configured.
- Each profile distinguishes a positive Arr download-client ID, category,
  qBittorrent save path, Arr import path prefix, local staging root, canonical
  root, download/staging/canonical capacity-pool references, namespace,
  Jellyfin library ID, and Jellyfin path prefix.
- The instance-level qBittorrent projection includes one absolute
  `mutation_lock_path`, protocol version `shared-qbittorrent-mutation/v1`, and a
  bounded acquisition timeout. It carries no downstream-domain label.
- Named capacity pools carry a read-only probe path and bounded headroom
  policy. Root-to-pool readiness proves filesystem identity; equal filesystems
  are one supply and distinct filesystems are evaluated independently.
- Fence, Publisher, and Reconciler receive immutable minimal projections rather
  than unrestricted filesystem fields.

- [ ] Resolve current stable Python and seven upstream versions from official
  sources; prove that Arr exposes stable positive download-client IDs and the
  Queue/History fields needed for exact post-watermark adoption. Record a
  `DecisionGap` if that oracle is absent; do not weaken it. Add a focused
  documentation/config test binding the profile and confirm the old pin
  expectation fails when appropriate.
- [ ] Write failing configuration tables for two valid profiles and for
  missing/extra source, invalid or duplicate download-client ID, duplicate
  category, source-kind mismatch, ambiguous translation, missing pool,
  root/probe filesystem mismatch, collapsed distinct filesystems, aliased
  duplicate pools, overlapping/aliased roots, unsafe path, duplicate namespace
  or library identity, reserved synthetic library IDs, missing/relative lock
  path, unknown lease version, unbounded timeout, symlink/non-regular lock, and
  replaced lock identity at readiness.
- [ ] Run `python -m unittest discover -s tests -p 'test_config.py' -v` and
  record the expected failures caused by the singular schema.
- [ ] Introduce the smallest immutable source-profile types, strict parser, and
  component projections. Preserve unknown-key rejection and on-demand secret
  resolution.
- [ ] Adapt component construction without adding cross-store or cross-root
  access. Re-run focused config and affected CLI tests.
- [ ] Commit the profile/configuration boundary after `python
  scripts/check-docs.py` and `git diff --check` pass.

### Task 2: Separate qBittorrent paths and prove foreign-transfer isolation

**Files:**
- Modify: `src/media_interlock/adapters/{arr,qbittorrent,radarr,sonarr}.py`
- Add: `src/media_interlock/_infra/advisory_lease.py`
- Modify: `src/media_interlock/fence/{model,service,store,daemon,cli,observability}.py`
- Add: `tests/test_advisory_lease.py`
- Modify: `tests/test_{arr_adapters,qbittorrent_adapter,fence}.py`
- Modify: `tests/test_fence_{integration,observability}.py`
- Modify: `docs/current/domains/fence.md`

**Interfaces:**
- Arr readiness proves the exact configured positive client ID is enabled,
  torrent/qBittorrent, `InitialState=Stop`, and has the exact category; a
  second enabled source-capable client inhibits.
- qBittorrent observations compare `save_path` with the profile's
  `qbittorrent_save_path`, never Publisher staging.
- Fence owns only a durable reservation whose exact hash carries its
  owner-bound tag. Unrelated hashes/categories are immutable to every Fence
  effect and recovery path.
- Every qBittorrent mutation holds the same safely opened
  `shared-qbittorrent-mutation/v1` inode lease used by a synthetic peer writer.
  The lease is mechanism only and cannot authorize an unowned hash.

- [ ] Write failing tests showing global `start_paused_enabled=false` is
  compatible when both Arr clients are stopped-on-add, while a Start-configured
  Radarr/Sonarr client inhibits its source.
- [ ] Write a failing mixed snapshot containing two governed transfers and one
  unrelated transfer. Exercise tag, resume, terminal observation, recovery,
  containment and invalid input; assert the foreign hash, category, tags,
  state, path and counters never change.
- [ ] Add negative cases for save-path/staging conflation, category-only or
  broad mutation, duplicate match, tag collision, category drift, hash drift,
  active unowned work, redirects, oversized bodies and reauthentication.
- [ ] Write failing cross-process lease tests: exactly one holder; bounded busy
  result; release on normal exit and kill; stable `fstat` identity; refusal of
  symlink, non-regular file, link count other than one, inode replacement and
  path drift. Hold the lease in a synthetic peer process and prove Fence makes
  zero qBittorrent mutation without learning a peer domain.
- [ ] Require the lease around exact tag/resume/pause/release/delete/containment
  effects and recovery. Reobserve intent, ledger identity, hash, category, path,
  tags and state after acquisition; busy or drift inhibits without a broad
  fallback. Expose only a bounded non-mutating local contention/identity probe
  for downstream cross-view proof.
- [ ] Implement exact per-profile observations and owner-tag validation with
  no global selector. Narrow readiness from global paused preference to the
  two Arr client contracts.
- [ ] Parameterize Fence state/store records by the source profile identity and
  add store migration or explicit incompatible-store refusal; never reinterpret
  old records silently.
- [ ] Run focused adapter/Fence tests and the existing Reconciler-Fence vertical;
  commit only after the mixed-snapshot negative proof passes.

### Task 3: Adopt external Arr grabs observer-first

**Files:**
- Modify: `src/media_interlock/adapters/arr.py`
- Modify: `src/media_interlock/fence/{model,service,store,daemon,cli}.py`
- Modify: `src/media_interlock/contracts.py` only for a new versioned adoption
  envelope
- Modify: `tests/test_arr_adapters.py`
- Modify: `tests/test_fence*.py`
- Modify: `tests/test_release_rehearsal.py`

**Interfaces:**
- Fence polls bounded public Queue/History observations per profile and records
  a causal watermark before adopting a later stopped transfer.
- One exact source entity, post-watermark history event, configured client ID,
  Arr download ID, canonical hash, positive size, category, save path and
  stopped state create a canonical persisted `observation_fingerprint` and an
  idempotent reservation before tag/resume. It is not the Reconciler
  `/release` fingerprint.
- Reconciler pre-admission and external adoption converge on the same governed
  reservation model without sharing Reconciler state.

- [ ] Write failing normal-flow tests for Seerr/RSS/operator-style Arr grabs
  that have no Reconciler pre-admission and remain stopped until adopted.
- [ ] Add zero/multiple/lost-event, stale watermark, wrong entity/protocol/
  client/category/path/hash/size, duplicate operation and concurrent-observer
  tests. Webhook absence must not affect eventual polling recovery.
- [ ] Race external adoption of the same initially unowned hash against a
  synthetic peer using the published lease. Persist prospective intent before
  effect, reacquire and reobserve under the lease, and allow at most one exact
  owner tag to become authoritative; the loser keeps the transfer stopped and
  reconciles its uncommitted reservation without touching the winner.
- [ ] Add parameterized crashes before/after watermark, adoption intent,
  reservation persistence, owner tag, tag observation, resume intent, resume,
  and active observation. A possibly consumed effect is observed before retry.
- [ ] Implement the smallest bounded polling/adoption state transitions and
  daemon tick. Do not rank releases, fetch locators, or act as download client.
- [ ] Re-run Fence, Arr, Reconciler and release-rehearsal tests; commit the
  external-grab vertical.

### Task 4: Add physical headroom accounting

**Files:**
- Modify: `src/media_interlock/config.py`
- Modify: `src/media_interlock/fence/{model,service,observability,cli}.py`
- Add or modify: one small infrastructure module only if `statvfs` observation
  cannot remain Fence-local
- Modify: `tests/test_{config,fence,fence_observability}.py`
- Modify: `docs/current/domains/fence.md`

**Interfaces:**
- Fence policy includes logical video capacity, inflight bound, named read-only
  free-space probes, per-pool minimum free bytes and safety margins, and bounded
  provisional staging/sidecar liabilities.
- Admission and every resume require both the logical ledger and the checked
  physical-headroom predicate for every affected filesystem from the design.
- Health reports aggregate logical reservation and physical inhibition without
  paths or unrelated-domain labels.

- [ ] Write failing pure tables for exact boundary, checked overflow, already
  allocated bytes, future download/staging/publication liabilities, hardlink
  liability reduction, sidecar allowance, retained predecessor, same-pool
  aggregation, and double-count rejection.
- [ ] Write failing observation tests for stale/changing `statvfs`, root/probe
  filesystem mismatch, two aliases falsely presented as independent pools,
  distinct pools incorrectly collapsed, read error, free-space loss
  immediately before resume, and unrelated consumption reducing free bytes
  without becoming a reservation.
- [ ] Implement a bounded injectable free-space observation and exact checked
  arithmetic. Unknown or divergent observations inhibit without mutating a
  foreign transfer.
- [ ] Document the residual concurrent-write race and explicitly deny a
  filesystem quota guarantee.
- [ ] Run focused capacity, config and observability tests plus Fence vertical;
  commit the capacity boundary.

### Task 5: Add exact Fence quiescence

**Files:**
- Modify: `src/media_interlock/contracts.py`
- Modify: `src/media_interlock/adapters/qbittorrent.py`
- Modify: `src/media_interlock/fence/{model,service,store,daemon,cli,observability}.py`
- Modify: `tests/test_{contracts,qbittorrent_adapter,fence}.py`
- Modify: `tests/test_fence_{integration,observability}.py`
- Modify: `docs/current/{architecture,operations}.md`
- Modify: `docs/current/domains/fence.md`

**Interfaces:**
- A versioned local request durably inhibits new admission, pauses each exact
  active ledger-owned hash, observes all terminal stopped states, and reports
  unresolved count.
- Recovery observes a possibly consumed pause before retry. Exit from
  quiescence revalidates ownership, source readiness, logical capacity and
  physical free space before any resume.

- [ ] Write failing quiescence tests with zero, one, and several governed
  hashes plus unrelated active work. Include drift and ambiguous observations.
- [ ] Add crashes before/after inhibit persistence, each pause intent/effect/
  observation, terminal quiescence and reopen. No crash may resume work or
  touch an unowned hash.
- [ ] Implement the smallest state/contract/CLI additions. Do not stop
  processes, services or qBittorrent itself.
- [ ] Re-run focused and affected vertical tests; commit the quiescence
  primitive.

### Task 6: Converge MI-06

**Files:**
- Modify: `README.md` only if public status wording changes
- Modify: `docs/current/{state,architecture,operations,acceptance}.md`
- Modify: `docs/current/domains/{fence,reconciler}.md` as required
- Modify: `docs/index.json`
- Delete: `docs/work/plans/2026-08-09-mi-06-source-profiles-shared-qbittorrent.md`

- [ ] Stabilize the candidate, then request one consolidated independent review
  covering ownership partition, observer causality, capacity arithmetic,
  quiescence recovery, bloat and documentation claims.
- [ ] Resolve every material finding with focused red/green tests and one
  focused correction review whose inputs are named.
- [ ] Run all 126-or-more tests, `python scripts/check-docs.py`, bytecode
  compilation, `git diff --check`, and a repository secret-pattern scan.
- [ ] Run the affected integrated rehearsal from a clean export with the mixed
  qBittorrent snapshot and a synthetic peer holding the shared inode; prove
  contention, crash release and exact foreign-hash invariance without contacting
  a downstream or live service.
- [ ] Update permanent owners to describe only implemented behavior, mark
  `MI-06` completed and `MI-07` pending in the index, remove this active plan,
  and retain the specification/catalog.
- [ ] Commit the converged unit locally. Do not push, tag, publish, or modify a
  downstream repository.
