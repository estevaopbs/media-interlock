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

An acquisition intent carries a transient locator plus its SHA-256 fingerprint;
Fence persists the fingerprint and correlation identities, never the locator.
Fence binds qBittorrent work to one observed lowercase torrent hash, its Fence
category, and its configured staging root before resuming it. Terminal
observation contains stable operation and upstream correlation identities, not
an authoritative external path.

## Repository boundary

The intended package layout is:

```text
src/media_interlock/
  config.py
  contracts.py
  observability.py
  _infra/{state,safe_fs,unix_rpc}.py
  reconciler/{model,service,store,cli}.py
  publisher/{model,service,store,filesystem,generation,observability,daemon,cli}.py
  fence/{model,service,store,observer,cli}.py
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

## Configuration and secrets

TOML is the single human-authored configuration format. One file contains a
shared runtime section and optional component and adapter sections, while each
process consumes only its typed projection. The loader rejects
unknown keys, invalid combinations, duplicate identities, unsafe paths, and
unbounded values. Reconciliation policy is configurable, including eligibility
windows, cooldowns, language preferences, quality constraints, and resource
budgets.

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
- every client that can add qBittorrent work adds it stopped under a fenced
  identity/category, automatic resume is disabled, and Fence is the only actor
  allowed to start or resume it;
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

Each component exposes a CLI with human-readable and JSON output, health
status, and metrics. Distribution targets are a Python wheel suitable for
`pipx` and OCI images for the daemons. Linux is the version-1 platform, on bare
metal, Docker, or Podman with explicit filesystem and socket mounts.

Backup and disaster recovery are intentionally absent. Operators provision an
external backup or snapshot system and use upstream-native facilities where
available. MediaInterlock implements only crash recovery necessary to finish or
adopt its own durable effects; that is transaction correctness, not backup.
