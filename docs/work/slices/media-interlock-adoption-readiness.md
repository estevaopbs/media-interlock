# MEDIAINTERLOCK adoption-readiness slices

Status: ordered continuation of the existing product program. Only the first
dependency-ready pending unit may have an active plan. No unit changes a
downstream or live deployment.

## Completed foundation

`MI-00` through `MI-05` remain completed in Git and in the program index. They
established the repository lifecycle, shared contracts, Fence, Publisher,
Reconciler, and the initial local release candidate. This catalog does not
reopen or duplicate their completed plans.

## MI-06 Source profiles and shared qBittorrent

Replace singular path/category configuration with strict Radarr and Sonarr
source profiles. Separate qBittorrent save paths from Arr-import and Publisher
staging namespaces. Narrow Fence ownership to exact profile, ledger, hash, and
owner tag; remove the global start-paused requirement; add observer-first
adoption for external Arr grabs, a neutral shared-qBittorrent mutation lease,
physical free-space headroom, and exact local quiescence.

Exit: both source profiles run in one Fence; unrelated qBittorrent transfers
remain byte-for-byte and state-for-state unchanged through every mutation and
recovery path; external stopped grabs are adopted idempotently; logical and
physical capacity gates fail closed; the public file-lease contract linearizes
Fence with a synthetic peer writer without exposing the peer domain;
quiescence pauses only governed hashes.

## MI-07 Stable bundle, custody, and bootstrap

Extend Publisher from one payload to a twice-observed complete asset bundle
with bounded media inspection and configurable language/sidecar policy. Accept
hardlinked staging only while Fence holds an exact observed freeze, copy every
canonical byte to independent inodes, and recover custody across crashes. Add
generic bootstrap plus distinct assisted-existing-candidate provenance without
deployment correction logic.

Exit: hardlinked seed payloads and changing sidecars cannot leak a writer inode
or unstable bundle into canonical storage; last-known-good and exact Jellyfin
delivery remain intact; bootstrap and assisted intake are durable, idempotent,
and provider-neutral.

## MI-08 Deployment artifacts and integrated release candidate

Add the Reconciler OCI target, arbitrary non-root numeric operation, standard
source/revision/version labels, bounded component health/config probes, and
three-image artifact manifests. Run the full disposable two-profile rehearsal
with an unrelated qBittorrent transfer, the shared mutation-lock mount, a
synthetic peer writer, and all declared adapters. Freeze the
latest compatible stable upstream profile at the start of `MI-06` and retain it
through this unit.

Exit: wheel and three OCI images reproduce from a clean checkout; all focused,
affected, documentation, secret, full-suite, and release-rehearsal gates pass;
one consolidated review has no unresolved material finding. This exit creates
only a local release candidate.

## MI-09 Immutable public release

After explicit user authorization, create the versioned Git release and
publish the accepted artifacts from the exact `MI-08` commit. Verify remote tag,
source commit, wheel identity, OCI manifest digests, labels, and public release
metadata without rebuilding or substituting inputs.

Exit: one immutable remote identity is independently resolvable and suitable
for downstream pinning. Update permanent current documents, mark the program
complete, and remove this specification, catalog, active plan, and checkpoint.
No downstream repository is modified by this unit.
