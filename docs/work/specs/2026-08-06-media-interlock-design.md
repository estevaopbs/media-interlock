# MediaInterlock initial product design

Status: candidate design consolidating approved product decisions. Technical
allocations explicitly marked provisional are resolved by their owning slice.

## Context

The downstream implementation that motivated this project combined reusable
media behavior with deployment-specific lifecycle, filesystem layout, catalog
correction, migration, reverse proxy, backup, and live promotion. Its
size and coupling make direct publication unsuitable: copying it would preserve
local assumptions, duplicate clients and stores, and impose a large test suite
whose count does not correspond to public product risk.

MediaInterlock extracts behavior rather than files. It is developed in its own
repository without modifying or deploying the live stack. Downstream adoption
happens only after a separately accepted release.

## Goals

1. Provide a configurable reconciler for bounded movie and episode upgrade
   searches through native Radarr and Sonarr behavior.
2. Provide a transactional publisher that verifies candidates and safely
   advances a canonical last-known-good media library.
3. Provide a fail-closed acquisition fence around qBittorrent admission,
   capacity, observation, and seeding constraints.
4. Support Jellyfin, Radarr, Sonarr, qBittorrent, Bazarr, Seerr, and Prowlarr
   through optional, typed adapters in the first complete release.
5. Be suitable for community use through provider-neutral contracts,
   configurable policy, standard packaging, bounded documentation, and a
   reproducible agent-assisted development lifecycle.

## Non-goals

- A Jellyfin plugin, modified Jellyfin build, private Jellyfin API, or direct
  database editor.
- Stack lifecycle, service or container topology, reverse proxy, firewall, egress,
  host-user provisioning, or deployment secret installation.
- Backup, snapshot, restore, disaster recovery, or a required restic workflow.
- Deployment-specific defaults, catalog/plugin retirement, provider metadata or
  user-state correction, clone rehearsal, promotion gates, or any other one-off
  migration.
- Tracker scraping, release ranking, a replacement for Arr, a web UI, a remote
  TCP control plane, or an embedded scheduler in version 1.
- Music acquisition in the first release.

## Critical design decisions

### One repository, three deployables

The public source is one MIT-licensed Python monorepo with a single SemVer
version. It produces a one-shot reconciler and two daemons, publisher and fence.
Components have independent identities, sockets, durable stores, health, and
side effects. Shared code is restricted to exact mechanisms; domain stores and
state machines remain explicit.

The repository name is `media-interlock`, the import namespace is
`media_interlock`, and the entrypoints are
`media-interlock-reconciler`, `media-interlock-publisher`, and
`media-interlock-fence`.

### Contracts instead of shared stores

No component imports another component's store types or opens its database.
Cross-component requests use versioned envelopes over Unix sockets with stable
idempotency and ownership identifiers. An unavailable service yields an
explicit unavailable or inhibited result; callers cannot synthesize success or
fall back to database inspection.

Fence retains ownership and capacity reservation after a download completes.
Its terminal acquisition observation carries stable correlation identity, not
path authority. Publisher first reserves capacity and records custody intent,
then correlates the operation with Radarr or Sonarr observations under a
configured staging root. Only its durable custody receipt lets Fence release
the original reservation. Until then, Publisher unavailability or ambiguity
backpressures new admission. The overlap may conservatively double-count space;
no crash may leave the payload unowned.

Version 1 has no TCP API. Bare-metal, Docker, and Podman deployments provide a
shared runtime directory only for the sockets explicitly needed.

### Arr release handoff and Fence ownership

The Reconciler uses Arr's public interactive release observation, preserving
Arr's ordered decisions and selecting only the first approved torrent when its
identity is unambiguous. Before requesting that exact public release, it
durably records the entity, selector fingerprint, causal watermark and expected
size, then asks Fence for a pre-admission reservation. The reservation has no
locator or download ID at that point.

Radarr and Sonarr continue to authenticate to indexers, acquire and validate
the torrent, track the grab, and import it. Their configured qBittorrent
download clients must use `InitialState=Stop`, with a distinct configured
category per source. Public Queue and History polling after the watermark binds
one exact Arr grab and real download ID/hash to the reservation. Only Fence
may tag and start or resume that stopped torrent after it observes the exact
hash, source category, staging path and positive size. Missing, unknown or
ambiguous observations never prove non-execution and never trigger a blind
repeat of release, tag, or resume effects.

### Configuration without configurable safety

TOML is parsed into typed component projections. Unknown keys and unsafe or
ambiguous combinations fail startup. Rules that express operator policy become
configuration: eligibility, cooldowns, temporal horizons, language and quality
preferences, resource budgets, capacity margins, concurrency, and bounded
timeouts.

Safety properties stay code: single writer, intent before effect, durable and
idempotent transitions, exact path containment, fail-closed ambiguity, store
ownership, bounded observations, and last-known-good retention. Configuration
cannot bypass them. Secrets are supplied only through environment variables or
files and never normalized into product state or output.

Readiness also depends on deployment-enforced preconditions. Canonical roots
are Publisher-only writable and read-only to playback; acquisition services
write disjoint staging roots. Every download client adds work stopped,
qBittorrent auto-resume is disabled, and Fence is the only resume writer.
MediaInterlock validates root separation and observable upstream settings;
downstream identities, mounts, ACLs, and credentials enforce and negatively
test the remainder. Failure to establish either side keeps the component
unready.

### Public upstream APIs

Adapters target the stable public APIs of the latest stable upstream versions
pinned at the start of each development cycle. The pin remains unchanged for
that cycle. A configured adapter negotiates and reports exact capability;
unsupported or ambiguous behavior blocks only the capability that requires it.

The initial adapter allocation to validate is:

| Adapter | Primary owner |
| --- | --- |
| Radarr | Reconciler search plus Publisher movie candidate correlation |
| Sonarr | Reconciler search plus Publisher episode candidate correlation |
| qBittorrent | Fence admission, transfer, and seeding effects |
| Prowlarr | Fence or reconciler readiness when policy requires it |
| Jellyfin | Publisher catalog observation and delivery |
| Bazarr | Publisher subtitle readiness when enabled |
| Seerr | Publisher availability projection when enabled |

The seven-adapter set is approved. Optional Prowlarr, Bazarr, and Seerr
capability placement in this table is provisional and may be narrowed by the
owning vertical slice without adding a component or weakening an invariant.
MI-01 must assign Prowlarr's provider-neutral capability to Fence, Reconciler,
or both; the adapter cannot be omitted from the first complete release without
a new decision.

If a required public capability is missing, contribute upstream before adding
a private workaround. A plugin is a later option only after a documented API
gap and a separate decision.

### Distribution and support

The CLI and one-shot job ship in a Python wheel suitable for `pipx`; daemon OCI
images use the same version. Linux is the first supported platform. The project
supports only the current stable compatibility profile, not an indefinite
matrix of old upstream releases.

Development uses `0.x`. Version `1.0.0` requires the complete three-component
system, all declared adapters, packaging, integrated gates, and operator
documentation. A downstream pilot is separate deployment evidence, not an
implicit public release gate. No release is described as installed merely
because hermetic tests pass.

## Extraction boundary

The existing downstream implementation is input to a rewrite:

| Public behavior to extract | Local behavior to leave downstream |
| --- | --- |
| upgrade domain and reconciliation policy | Jellyfin providerless/orphan corrections |
| native Radarr/Sonarr search and observation adapters | one-off catalog and user-state migration |
| secure publication, retention, catalog delivery | clone, promotion, reverse-proxy maintenance, live exchange |
| bounded acquisition admission and qBittorrent observation | stack lifecycle, systemd, Podman and network topology |
| generic adapter health and capability negotiation | installers, host paths, users, secrets and app defaults |
| transactional crash recovery for owned effects | backup, restic, restore and disaster recovery |

Do not copy deployment path registries, monolithic publication/fence stores,
service-manager runtime, duplicate Arr/qBittorrent clients, or tests for local
migration. Re-express each accepted reusable behavior behind the new component
boundary and introduce it only with its smallest failing test.

## Test strategy

Tests are necessary when they detect a distinct product risk:

- pure state and policy tables for invariants;
- adapter contracts for exact upstream boundaries;
- durable local-state, socket, process, and safe-filesystem integration;
- parameterized crash points immediately before and after durable effects;
- one representative vertical flow for each component;
- one bounded integrated release rehearsal with synthetic media.

Do not preserve a one-test-per-file shape, duplicate trivial accessors, or copy
local lifecycle/migration/backup acceptance. Per-PR, affected integration,
nightly, and release gates have 10, 20, 60, and 90 minute ceilings. Mutation
testing is scheduled and targeted to safety-critical pure logic.

## Agent-assisted development program

The program adapts the simplified Zeggy lifecycle without its former custom
agent machinery:

- index-first documentation with `load_when` routing;
- Git as source and integration authority;
- an optional compact ignored checkpoint derived from Git;
- one specification, one ordered slice catalog, and one active plan;
- ephemeral branches/worktrees, TDD, focused-first verification, one
  consolidated review, and focused correction review;
- current documents updated and work material deleted at convergence;
- process evidence kept separate from product and deployment proof.

There are no model attestations, delegation ledgers, review chains, gate graphs,
custom evidence signatures, vendored documentation parsers, or compatibility
readers. A small validator enforces exhaustive Markdown indexing and the hard
documentation budget.

## Documentation budget

The permanent surface is at most 12 Markdown files and 160 KiB total. Active
work is at most one 64 KiB specification, one 24 KiB slice catalog, and one
32 KiB plan. Git is the historical archive. Completed plans and decision logs
do not remain as permanent navigation surfaces.

## Cross-repository adoption

MediaInterlock is the authority for provider-neutral product behavior and
contracts. A downstream repository remains authority for live baseline,
deployment, local configuration, one-off correction, backup, and observed
acceptance.

For an interface-affecting unit, each repository has a separate plan under one
program identifier. MediaInterlock integrates and releases first. The downstream
repository then pins that immutable version, removes superseded copied product code, and
executes its own hermetic and live gates. No public test claims a successful
live migration, and no downstream rehearsal weakens a public invariant.

## Closed and provisional decisions

The product name, repository model, component split, adapter set, platform,
configuration format, IPC, packaging, license, version policy, backup boundary,
test budgets, documentation budget, and agentic lifecycle are approved. File
decomposition, durable-store technology, exact schemas, adapter capability
allocation, CLI option names, one-file versus per-component TOML layout, and
implementation libraries are provisional planning choices unless they would
change an approved boundary. The custody handshake and exclusive-writer
preconditions are safety requirements; MI-01 through MI-03 must specify their
exact observable contracts before implementation.
