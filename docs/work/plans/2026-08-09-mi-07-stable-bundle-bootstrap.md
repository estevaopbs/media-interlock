# MI-07 stable bundle, custody, and bootstrap implementation plan

**Goal:** Replace the single-file Publisher candidate with a durably sealed,
policy-checked asset bundle; permit a hardlinked staging payload only under an
exact Fence freeze; and provide bounded owner-controlled bootstrap and assisted
intake without importing deployment migration behavior.

**Architecture:** Publisher records a canonical bundle manifest and provenance
before a filesystem effect. It performs two no-follow, bounded enumerations
separated by a settle interval, copies every accepted source to private
temporary generation inodes, verifies the copy and source stability, then
atomically publishes the complete generation. Bootstrap and assisted intake use
the same durable publication transitions but distinct versioned envelopes and
provenance; neither can fabricate Fence custody.

**Tech stack:** Python 3.14 standard library, existing SQLite stores and Unix
contracts, descriptor-relative filesystem operations, bounded media-inspection
adapter seams, and disposable `unittest` filesystem/HTTP fixtures.

## Global constraints

- Retain the MI-06 compatibility pins unchanged.
- Keep scope inside Publisher, the minimum exact Fence-freeze contract, and
  existing public adapters. Do not add deployment migration utilities,
  database edits, lifecycle control, a music policy, or an alternate media
  server integration.
- Treat every source path as a comparison input until descriptor-relative
  containment proves it beneath the configured source profile root.
- A hardlink is evidence requiring a Fence-owned hash freeze, never an
  authorization by itself. Every canonical file must have an inode independent
  of staging, Arr, qBittorrent, and assisted input.
- Follow TDD: record a focused red test before each behavior, implement the
  smallest change, then run only affected regressions until stabilization.

## Task 1: Seal and verify complete candidate bundles

**Files:** `publisher/{filesystem,model,service,daemon,cli}.py`, typed source
configuration/projections as required, focused Publisher tests, and the
Publisher current document.

- Define a strict bundle manifest with exactly one selected video, approved
  sidecars, bounded inventory, inode/size/allocation/digest observations, and
  inspection evidence. Reject unknown files, symlinks, traversal, duplicate
  logical names, unsupported extensions, overflow, and ambiguous selection.
- Add policy for accepted sidecar extensions/languages, required languages,
  bounded sidecar allowance, media inspection, and settle interval. Preserve
  mandatory containment, double observation, and digest verification as
  invariants rather than policy switches.
- Write focused red tables for source drift between observations, changing
  sidecars, altered metadata/allocation, inspection failure, and a stable
  mixed video/sidecar bundle. Persist the sealed bundle before generation
  effects and recover without silently re-enumerating a changed candidate.

## Task 2: Freeze hardlinked staging and copy independent generations

**Files:** minimum exact Fence contract/service/adapter additions,
`publisher/{filesystem,service,model}.py`, focused Fence/Publisher integration
tests, and architecture/Fence/Publisher documentation.

- Add one versioned, owner-bound Fence freeze observation that proves the exact
  terminal governed hash is stopped for the copy interval; it never pauses or
  controls an unowned hash. Model durable intent and observation-before-retry
  at every freeze boundary.
- Write red tests for a singly linked source, valid hardlinked governed source,
  foreign or hash/category/path/tag drift, lost response, crash before and
  after freeze, and a changing source during copy. Fence contention or
  ambiguity keeps the candidate pending.
- Copy every sealed bundle member to private temporary files, verify copied
  bytes and independent inodes, fsync files/directories, and atomically expose
  a full generation. Preserve asset-local last-known-good retention and reject
  any writer-owned canonical inode.

## Task 3: Add generic bootstrap and assisted provenance

**Files:** `contracts.py`, Publisher model/store/service/daemon/CLI, focused
contract and Publisher tests, and operations/acceptance documentation.

- Define strict versioned bootstrap and assisted envelopes. Bootstrap seals
  source profile, stable asset/catalog/Arr evidence or explicit provider
  absence, contained source identity, bundle manifest/digest, and publication
  reservation. The operation ID plus manifest digest is idempotent.
- Make assisted intake distinct from terminal Fence custody: it requires
  pre-recorded Publisher intent, exact Arr import evidence, contained bundle
  evidence, and publication-space reservation, and cannot invent a torrent
  hash or Fence receipt.
- Add red tests for replay, manifest drift, ambiguous catalog identity,
  wrong source profile, path escape, missing evidence, crash recovery, and
  rejection of deployment-specific correction fields.

## Task 4: Converge MI-07

- Stabilize, then perform one consolidated review of bundle atomicity,
  hardlink/freeze authority, provenance separation, recovery, documentation,
  and scope. Resolve material findings with a focused correction review.
- Run focused Publisher/Fence/contract regressions, the affected synthetic
  vertical and release rehearsal, full suite, documentation checker,
  compilation, diff check, and secret-pattern scan.
- Update permanent owners to implemented behavior, mark MI-07 completed,
  remove this plan, and commit locally. Do not push, publish, or mutate a
  downstream deployment. Then derive and index the sole MI-08 plan.
