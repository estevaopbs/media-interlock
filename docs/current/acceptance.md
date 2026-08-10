# Acceptance

Acceptance is risk-based. A high test count, agent report, fixture, commit, or
modeled upstream response is not evidence that a live deployment is safe.

## Test layers

1. Pure and table-driven tests cover domain invariants, legal transitions,
   invalid configuration, idempotency, and policy boundaries.
2. Adapter contract tests cover exact requests, responses, version capability,
   ambiguity, timeouts, redaction, and unsupported upstream behavior.
3. Disposable integration tests cover the selected local-store durability, Unix sockets,
   filesystem containment and atomicity, process restart, and synthetic
   upstream services.
4. Three vertical flows cover reconciler, publisher, and fence end to end.
5. A bounded release rehearsal integrates all configured adapters using
   synthetic media and disposable state.

The release rehearsal executes both governed source profiles against production
HTTP adapters and the real Unix daemon contracts. It uses a shared lock inode,
proves a synthetic peer's cross-process contention and crash release, and
asserts that its opaque unrelated qBittorrent transfer remains unchanged.

The integrated flow includes completed-download custody transfer: Fence retains
its reservation until Publisher durably adopts it, Publisher unavailability
backpressures admission, and every crash point conserves at least one owner.
The Publisher gates also cover double-observed bundles, sidecar and inspection
policy, source drift, independently copied canonical inodes, and exact
hardlink freezes. Bootstrap and assisted intake prove separate provenance,
manifest replay, and recovery rules.
Publisher socket black-box gates additionally require accepted/pending/catalog-
confirmed separation, conflict and unavailable replies, wrong-binding
inhibition, idempotent retry after a lost response, and one exact terminal
receipt only after direct-play verification. Metrics are checked separately for
absence of paths, titles, hashes, and operation IDs.
Conflict recovery also injects a durable-store write failure: re-query must
expose the original accepted identity and manifest binding, never an unbound
success that the conflicting caller could mistake for its own request.
Fence post-PNR adoption gates use its real Unix daemon, durable SQLite store,
production qBittorrent and Arr HTTP adapters, and a restart. They cover exact
source/client/entity/hash/category/save-path binding, intent-before-tag,
stopped/unowned read-before-write and tag read-back under the shared lease,
lost-response replay, restart recovery, conflicting replay, and unchanged
foreign music ownership. Fence metrics are checked for absence of paths,
hashes, and operation IDs.
Deployment tests also reject overlapping roots, competing canonical writers,
auto-resuming downloads, and a competing qBittorrent resume credential.

Crash tests enumerate durable intent/effect boundaries through shared
state-machine tables. They do not create one bespoke test per source function.
Behavior is not repeated at multiple layers unless each layer detects a
different failure class.

The shared mechanism gate verifies strict configuration and secret redaction,
contract version and field rejection, exclusive SQLite ownership and restart
persistence, atomic contained writes, Unix-socket framing, and every modeled
custody reservation boundary. This proves neither a live upstream integration
nor deployment filesystem permissions.

## CI time budgets

| Gate | Maximum wall time | Use |
| --- | ---: | --- |
| Pull request fast gate | 10 minutes | unit, configuration, docs, lint/type, affected contracts |
| Affected integration | 20 minutes | changed service, adapter, filesystem, process, or restart path |
| Nightly full gate | 60 minutes | all adapters and integrated synthetic flows |
| Release rehearsal | 90 minutes | clean build, packages, OCI images, upgrade/restart and full synthetic system |

Mutation testing is scheduled and targeted at safety-critical pure logic. It is
not a per-PR gate or a repository-wide coverage ritual.

## Release boundary

A release candidate requires:

- focused and affected test gates green from a clean checkout;
- all adapters declared for that release pinned and contract-tested;
- wheel byte identity and OCI manifest digests reproduced from hash-locked
  build inputs, with one canonical local artifact manifest binding source and
  all three image identities and archive tar files treated only as transport;
- documentation index, byte budgets, link checks, diff check, and secret scan;
- negative tests for fail-closed ambiguity and missing capability;
- disposable restart/crash rehearsal for effects changed in the cycle;
- one consolidated independent review with findings resolved.

The 0.1.6 release records its immutable source tag, wheel SHA-256, and three
public OCI digests in the current-state authority after immutable remote and
anonymous-access verification.
Its artifact build executes
all three images and rejects a
runtime Python version that differs from the fixed compatibility profile.
Those records prove only the published artifact identity and
disposable release gates, not a downstream or live deployment acceptance.

Version `1.0.0` additionally requires all three components integrated and
operator-facing configuration and upgrade guidance in their existing
documentation owners. A later downstream pilot proves only that pinned
deployment and cannot be generalized to every host, upstream version, or media
library; it is not an implicit public release gate.
