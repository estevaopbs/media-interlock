# Architecture

MediaInterlock is one versioned Python monorepo that produces three independent
entrypoints. The components share small infrastructure mechanisms and contract
types, but no component imports or writes another component's store.

```text
                 +----------------+
                 |   Reconciler   |
                 +--------+-------+
                          | versioned intent
                          v
  upstream APIs --> +-----+-----+  terminal acquisition + custody hold
                    |   Fence   +------------------------------+
                    +-----+-----+                              v
                          ^                              +-----+-----+
                          +---- custody receipt --------| Publisher |--> canonical library
                                                       +-----+-----+
                                                             |
                         Arr candidate observation -----------+
                                                             +--> catalog adapters
```

The diagram is a contract flow, not a shared transaction. Each service records
its own intent before its own effects, uses idempotent operation identifiers,
and exposes status over a versioned Unix socket. Failure or unknown state stops
new work at the affected boundary; it does not make another service's database
authoritative.

Fence's daemon reoffers durable terminal acquisitions directly to Publisher's
versioned socket. A pending or lost response is retried from Fence state, so a
deployment does not supply a recurring relay. The socket exchange does not
block Fence's server, allowing Publisher's exact freeze callback to finish
before it returns custody.

The fence owns a completed download until an idempotent custody handshake
finishes. It emits a terminal acquisition observation and keeps the payload's
space reservation active. Publisher records a conservative reservation and
custody intent, correlates the operation with Radarr or Sonarr through public
API observations, derives the candidate only under a configured staging root,
and then returns a durable custody receipt. Fence releases its reservation only
after observing that receipt. During overlap both reservations may count; a
crash therefore fails conservatively instead of losing accounting. If Publisher
is unavailable or rejects ambiguity, Fence retains the hold and backpressures
new admission according to available capacity.

If Arr's staging import is hardlinked to a completed torrent, Publisher first
requests the Fence's owner-bound exact freeze. Fence records the freeze intent,
holds the shared qBittorrent mutation lease while it pauses and observes the
governed hash, and retains that state through recovery. Publisher then seals,
copies, verifies, fsyncs, and atomically commits independent canonical bundle
inodes; a pending copy returns no custody receipt. Bootstrap and assisted
existing-candidate intake enter Publisher through separate owner-bound manifests
and never enter this Fence custody flow.

Reconciler persists an Arr release selector, causal watermark and expected size
before Fence pre-admission, which deliberately has no locator or download ID.
Arr then owns the authenticated release grab and download tracking with its
configured qBittorrent client stopped. Fence polls bounded public Arr Queue and
History both to bind that exact later grab and to adopt post-watermark external
Arr grabs through a separate durable observation fingerprint. It preserves
Arr's real download ID for the terminal contract and derives one matching
canonical lowercase torrent hash solely for qBittorrent observation, tagging,
pause, and resume. It observes that stopped hash, source-specific category and
qBittorrent save path before it tags or resumes it. A magnet may still report
zero bytes before metadata arrives; only an exact pre-admitted grab can use the
positive Arr release size already reserved to enter metadata download. Terminal
observation contains the real Arr download ID and stable operation correlation
identities, not an authoritative external path.

After a deployment's post-PNR authorization, its local authority may submit one
`post_pnr_adoption` envelope to Fence's version-1 Unix socket. This is a
separate, explicit path; observer-first polling never synthesizes it. The
envelope names the configured source, Arr client/entity, canonical torrent
hash, category, and qBittorrent save path. Fence re-observes one exact public
Arr History/Queue grab, then re-observes the stopped, unowned qBittorrent
torrent under `shared-qbittorrent-mutation/v1`, persists intent before adding its one
owner tag, and returns `post_pnr_adoption_receipt` only after durable tag
read-back. It does not resume the torrent. Querying the same operation with
`post_pnr_adoption_query` recovers that receipt after a lost response or
restart; conflicting replay, ambiguous Arr identity, qBittorrent drift, and
pre-existing owner tags fail closed.

`post_pnr_historical_adoption` is an additive, separately named version-1
operation; it does not change `post_pnr_adoption`'s singleton Queue-required
semantics. It names a nonempty, numeric-order canonical set of Arr entity IDs:
one Radarr or Sonarr entity, or a Sonarr episode pack. Fence re-reads every
bounded public History page and requires the complete `grabbed` entity set for
the exact canonical hash. Queue absence is permitted only here. If Queue
records remain, their complete entity set, configured client, torrent protocol,
hash, and one exact size must agree. With Queue absent, the exact stopped,
unowned qBittorrent observation supplies the durable expected size. Under the
shared mutation lease Fence re-observes that exact profile identity, writes one
intent and one tag, and returns one receipt binding the complete entity set.

`post_pnr_historical_activation` is a second identity-free version-1 authority
using the same operation ID. Fence derives every identity from its sealed
historical intent, durably records activation before the exact leased start,
then returns a `managed` receipt only after active read-back. Managed historical
reservations retain ownership and bytes but are excluded from logical inflight
admission; normal quiescence may pause and resume only their exact owned hash.

## Repository boundary

The intended package layout is:

```text
src/media_interlock/
  config.py
  contracts.py
  observability.py
  _infra/{state,safe_fs,unix_rpc,advisory_lease}.py
  reconciler/{model,service,store,cli}.py
  publisher/{model,service,store,filesystem,generation,observability,daemon,cli}.py
  fence/{model,service,store,daemon,headroom,observability,cli}.py
  adapters/{radarr,sonarr,jellyfin,qbittorrent,bazarr,seerr,prowlarr}.py
```

Shared code is limited to mechanisms with identical semantics: typed
configuration primitives, contract envelopes, durable local-state helpers,
safe filesystem primitives, Unix RPC, and observability. Each component owns a
private SQLite store under an exclusive writer lock. Intent and transition
writes use immediate durable transactions; no component can open another
component's store. There is no generic saga framework, cross-service ORM,
shared domain model, or universal client.

## Component ownership

- The reconciler owns eligibility, bounded search policy, reconciliation
  checkpoints, and search intents.
- The publisher owns candidate verification, canonical generations,
  acquisition custody intake, last-known-good retention, catalog delivery, and
  publication state.
- The fence owns acquisition admission, reservations, qBittorrent effects,
  observed transfer state, and seeding constraints.

The component pages own their detailed behavior. Stack lifecycle, scheduler
installation, reverse proxies, network namespaces, firewall rules, backups,
snapshots, restore, and local migrations remain outside the package.

## Adapters

The first complete release includes adapters for Jellyfin, Radarr, Sonarr,
qBittorrent, Bazarr, Seerr, and Prowlarr. An adapter is loaded only when its
typed configuration exists. Missing optional adapters do not produce mock
success; capabilities requiring them are unavailable and reported explicitly.
Radarr and Sonarr are used by Reconciler for search and by Publisher for
candidate correlation through independently configured clients. The exact
allocation of optional Prowlarr, Bazarr, and Seerr capabilities is provisional
until their vertical slices prove the smallest useful contract.

Adapters use documented public upstream APIs. Fence's qBittorrent adapter uses
the authenticated WebUI cookie API; Prowlarr is limited to health plus at least
one enabled configured indexer. If an essential capability is
missing, prefer an upstream contribution. A Jellyfin plugin or private API is
not part of the initial architecture and requires a new design decision.

Publisher derives an asset identity from Arr public APIs, publishes an immutable
bundle to that asset's stable logical slot, and retains an asset-local
last-known-good predecessor. Jellyfin notification is only a submitted effect:
the Publisher observes one exact library item and media source and hashes a full
static direct-play response before it records delivery. Recovery observes a
possibly consumed effect before changing any filesystem state; it does not
retract a slot blindly.

Arr import history and Publisher staging use distinct namespaces. The source
profile's `arr_import_path_prefix` is the absolute Arr-visible boundary. For an
exact correlated import, Publisher accepts only a canonical path strictly below
that boundary, derives its non-empty relative suffix lexically, and applies the
suffix below that source's configured `staging_root`. It never requires the two
absolute prefixes to be equal, resolves the Arr path through the host
filesystem, or accepts per-item mappings and textual-prefix fallbacks.

The Publisher's version-1 Unix surface projects that private state by
`operation_id` as accepted, pending, catalog-confirmed, visible-confirmed,
conflict, or unavailable. Visible confirmation is a separate terminal receipt
bound to the exact generation digest and Jellyfin identities; no aggregate
health or metric is delivery evidence. This projection is recovered from
Publisher-owned durable state, so clients retry the query after timeout rather
than reading SQLite or inferring success from a filesystem path.

## Configuration and secrets

TOML is the single human-authored configuration format. One file contains a
shared runtime section and optional component and adapter sections, while each
process consumes only its typed projection. The loader rejects
unknown keys, invalid combinations, duplicate identities, unsafe paths, and
unbounded values. Reconciliation policy is configurable, including eligibility
windows, cooldowns, language preferences, quality constraints, and resource
budgets.

The supported source set is one typed Radarr movie profile and one typed Sonarr
episode profile. A profile binds its Arr download-client identity and category,
qBittorrent save path, Arr-visible import prefix, Publisher roots and catalog
binding, and named physical-capacity pools. Multiple sources may share the same
Arr-visible import prefix while retaining distinct Publisher staging roots.
Fence receives only the acquisition and pool projection; Publisher and
Reconciler receive their own minimal fields.
Materialized roots and pool probes must agree on filesystem identity, while
distinct named pools cannot silently share a free-space supply.

Safety invariants are not configuration: single-writer ownership, intent before
effect, path containment, durable state transitions, idempotency, fail-closed
ambiguity, and last-known-good retention cannot be disabled. Secrets are
referenced from environment variables or files and never copied into normalized
configuration, state, logs, metrics, or artifacts.

## Deployment preconditions

Product readiness requires an enforceable ownership topology:

- canonical roots are writable only by Publisher and are mounted read-only into
  playback services; Arr, qBittorrent, and Bazarr cannot write or mount them;
- acquisition and subtitle services write only configured staging roots, which
  are disjoint from canonical and private publication roots;
- every governed Arr client adds qBittorrent work stopped under its exact
  profile category; Fence is the only writer for its owner-tagged hashes and
  serializes every mutation through the configured shared inode lease;
- no competing process may acknowledge custody, release a fence reservation,
  or mutate component state.

Configuration validation rejects overlapping roots and incompatible observed
upstream settings. Adapter readiness verifies enforceable paused-on-add and
resume ownership where the public API exposes them. The downstream deployment
must enforce identities, mounts, and ACLs and prove them with negative probes;
if either side cannot establish its part, the component remains unready. These
preconditions are not optional policy and cannot be replaced by operator trust.

## Interfaces, packaging, and platform

The reconciler is a one-shot job. Publisher and fence are daemons. Version 1
uses newline-delimited canonical JSON envelopes over local Unix sockets only.
Every envelope has a version, kind, operation identity, and body; unknown
fields and unsupported versions fail closed. Container deployments mount an
explicit shared runtime directory. TCP APIs, a web UI, and an embedded scheduler
are not initial features.

Each component exposes bounded `--version` and `--check-config` probes; the
daemons additionally expose local status and metrics. Distribution targets are
a Python wheel suitable for `pipx` and OCI images for all three components.
Every image carries standard source, revision, version, and license labels and
uses an arbitrary non-root numeric runtime identity; it supplies no deployment
manifest or privileged ownership setup. Linux is the version-1 platform, on
bare metal, Docker, or Podman with explicit filesystem and socket mounts.

Backup and disaster recovery are intentionally absent. Operators provision an
external backup or snapshot system and use upstream-native facilities where
available. MediaInterlock implements only crash recovery necessary to finish or
adopt its own durable effects; that is transaction correctness, not backup.
