# MEDIAINTERLOCK vertical slices

Status: proposed ordered program catalog derived from the approved product
boundaries. Each slice receives a reviewed implementation plan before code.

Each slice ends in an integrated, reviewable capability. A slice may introduce
only the tests and documentation needed by its own risks. All three components
and seven configured adapters are required before the first complete release;
an intermediate slice is not advertised as the finished product.

## MI-00 Repository and development program

Create the MIT-licensed repository, exhaustive documentation index, hard
budgets, current authorities, candidate design, slice catalog, and focused
validator tests. No product package or compatibility claim is created.

Exit: docs validator and its tests pass; all Markdown is indexed; current state
says preimplementation; no downstream or live state changed.

## MI-01 Contracts and typed configuration

Introduce the package skeleton, strict TOML configuration, secret references,
versioned Unix envelopes, status codes, operation identity, selected durable
local-state and atomic IO primitives, and CLI/JSON conventions. Define the
terminal-acquisition/custody handshake and its conservative capacity transfer.
Define Prowlarr's provider-neutral capability and assign its implementation to
MI-02, MI-04, or both; omission requires a new product decision.
Pin current stable development and upstream versions for the cycle.

Exit: unknown/ambiguous configuration fails; secrets are redacted; contracts
round-trip and reject incompatible versions; no domain component reads another
store; no custody crash leaves a payload without at least one reservation.

## MI-02 Acquisition fence vertical

Deliver the fence daemon through qBittorrent admission, observation,
reservation, publisher handoff, crash reconciliation, health, and metrics.
Validate paused-on-add, disabled auto-resume, and fence-only resume ownership.
Implement the Fence-owned part of the approved Prowlarr adapter contract, if
MI-01 assigns one here.
Keep stack lifecycle and networking outside.

Exit: the representative admit-to-terminal flow and every durable external-
effect boundary pass under disposable services; unknown state closes admission;
Publisher unavailability retains the completed payload reservation and applies
backpressure.

## MI-03 Transactional publisher vertical

Deliver candidate intake, safe filesystem verification, generation commit,
last-known-good retention, restart recovery, GC, health, and metrics. Add
Radarr/Sonarr candidate correlation and Jellyfin catalog delivery, then the
smallest reviewed optional Bazarr and Seerr capabilities.

Exit: representative candidate-to-catalog flow passes; adversarial path and
crash matrices pass; custody adoption precedes the fence receipt; overlapping
roots or a competing canonical writer fail readiness; catalog failure cannot
destroy the last known-good media.

## MI-04 Upgrade reconciler vertical

Deliver typed movie/episode policy, attempts and checkpoints, native Radarr and
Sonarr searches, suppressions, force behavior, JSON output, and interaction
with fence/publisher contracts. Implement the Reconciler-owned part of the
approved Prowlarr adapter contract, if MI-01 assigns one here. Policy comes from
TOML; upstream ranking stays upstream.

Exit: representative eligible-to-search flow passes; ambiguity and technical
failure do not create false success or bypass fence admission.

## MI-05 Integrated release candidate

Integrate all three components and every configured adapter, complete wheel and
OCI packaging, compatibility checks, upgrade/restart behavior, negative
security cases, synthetic full-system rehearsal, documentation, and independent
review.

Exit: fast, affected, nightly, and release gates meet their budgets from a clean
checkout. Publish only after separate user authorization.

Downstream adoption is a separate cross-repository program after an immutable
release candidate exists. It is not another public-product slice or an implicit
`1.0.0` gate. At MediaInterlock program convergence, update permanent current
documents and remove this specification, catalog, active plans, routing, and
checkpoint.
