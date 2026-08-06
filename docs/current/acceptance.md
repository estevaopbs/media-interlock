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

The integrated flow includes completed-download custody transfer: Fence retains
its reservation until Publisher durably adopts it, Publisher unavailability
backpressures admission, and every crash point conserves at least one owner.
Deployment tests also reject overlapping roots, competing canonical writers,
auto-resuming downloads, and a competing qBittorrent resume credential.

Crash tests enumerate durable intent/effect boundaries through shared
state-machine tables. They do not create one bespoke test per source function.
Behavior is not repeated at multiple layers unless each layer detects a
different failure class.

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
- wheel and OCI artifacts built reproducibly and scanned;
- documentation index, byte budgets, link checks, diff check, and secret scan;
- negative tests for fail-closed ambiguity and missing capability;
- disposable restart/crash rehearsal for effects changed in the cycle;
- one consolidated independent review with findings resolved.

Version `1.0.0` additionally requires all three components integrated and
operator-facing configuration and upgrade guidance in their existing
documentation owners. A later downstream pilot proves only that pinned
deployment and cannot be generalized to every host, upstream version, or media
library; it is not an implicit public release gate.
