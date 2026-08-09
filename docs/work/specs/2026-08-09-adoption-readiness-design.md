# MediaInterlock adoption-readiness design

Status: approved product adjustments required before a downstream deployment
may adopt MediaInterlock. This document owns pending product behavior only. It
does not authorize a package release, push, or downstream/live mutation.

## Context

The initial `MI-00` through `MI-05` program produced a local release candidate
with three component entrypoints, but deployment analysis exposed generic
topologies that the current candidate cannot represent safely:

- one qBittorrent instance may serve governed video and unrelated domains;
- Radarr and Sonarr may use different categories, download roots, import path
  namespaces, staging roots, canonical roots, and Jellyfin libraries;
- an Arr import may be a hardlink to a completed seeding payload;
- a published asset is a stable bundle, not one media payload;
- existing canonical libraries require a Publisher-owned bootstrap path;
- all three components need OCI delivery and arbitrary non-root runtime
  identities.

These are public product gaps. They are corrected here rather than hidden in a
downstream wrapper. Deployment lifecycle, service topology, local migration,
backup, reverse proxy, credentials, and live acceptance remain downstream.

## Goals

1. Represent Radarr and Sonarr as independent typed source profiles.
2. Govern only explicitly adopted transfers while sharing qBittorrent safely
   with unrelated categories and writers.
3. Adopt normal external Arr grabs by durable, bounded observation rather than
   requiring every grab to originate in Reconciler.
4. Combine a logical governed-video budget with observed physical free-space
   headroom without claiming filesystem quota isolation.
5. Publish complete stable bundles from safe hardlinked staging sources into
   independent canonical inodes.
6. Provide generic Publisher bootstrap and assisted-existing-candidate
   contracts without importing deployment correction rules.
7. Publish wheel and OCI artifacts for Reconciler, Fence, and Publisher with
   source identity, arbitrary non-root operation, and reproducible digests.
8. Produce an immutable public release before any downstream pin changes.

## Non-goals

- Music-specific policy, Lidarr or Navidrome adapters, or a hard-coded music
  category name.
- A second qBittorrent instance, qBittorrent proxy, or claim of category-scoped
  API credentials.
- Stack lifecycle, systemd or Quadlet files, host paths, ACLs, reverse proxy,
  backup, restore, disaster recovery, or live promotion.
- One-off Jellyfin provider repair, virtual-library retirement, UserData merge,
  database editing, or migration orchestration.
- Local release ranking, indexer scraping, or replacement of Arr selection.
- Copying a downstream publisher, fence, migration framework, or its tests.

## Source profiles

Configuration introduces exactly one profile per supported source identity.
The first release supports `radarr` and `sonarr`; profiles cannot declare
Lidarr or an arbitrary adapter as a governed source. Each profile contains:

```toml
[sources.radarr]
kind = "movie"
arr_download_client_id = 17
category = "video-movies"
qbittorrent_save_path = "/downloads/complete/movies"
arr_import_path_prefix = "/arr/library"
staging_root = "/staging/movies"
canonical_root = "/canonical/movies"
download_capacity_pool = "media"
staging_capacity_pool = "media"
canonical_capacity_pool = "media"
namespace = "movies"
jellyfin_library_id = "11111111-1111-4111-8111-111111111111"
jellyfin_path_prefix = "/media/movies"

[sources.sonarr]
kind = "episode"
arr_download_client_id = 27
category = "video-shows"
qbittorrent_save_path = "/downloads/complete/shows"
arr_import_path_prefix = "/arr/library"
staging_root = "/staging/shows"
canonical_root = "/canonical/shows"
download_capacity_pool = "media"
staging_capacity_pool = "media"
canonical_capacity_pool = "media"
namespace = "shows"
jellyfin_library_id = "22222222-2222-4222-8222-222222222222"
jellyfin_path_prefix = "/media/shows"

[capacity_pools.media]
probe_path = "/capacity/media"
minimum_free_bytes = 21474836480
safety_margin_bytes = 5368709120
```

The UUIDs are reserved synthetic examples and must not pass runtime library
readiness. A materialized deployment uses exact observed identifiers.

Each positive download-client ID is the stable public Arr resource identity
sealed by deployment configuration. Each process consumes only its typed
projection. Fence receives that client ID, category, qBittorrent save path,
source identity, referenced physical-capacity pools, and the Arr observation
adapter it needs. Publisher receives Arr path translation,
staging/canonical roots, namespace, and Jellyfin binding. Reconciler receives
source identity and search policy. A component does not gain another
component's filesystem access merely because the source profile is parsed from
one TOML file.

Unknown fields, missing supported profiles, duplicate identities or categories,
missing pool references, overlapping writable roots, ambiguous prefix
translations, aliases through existing symlinks, unsafe paths, and inconsistent
source kinds fail before side effects. Every materialized download, staging and
canonical root must be bound to the same filesystem identity as its declared
pool probe; one pool may serve several roots only when their observed
filesystem identity is equal. Strings observed from Arr, qBittorrent, or
Jellyfin are comparison inputs and never path authority.

## Shared qBittorrent ownership

The qBittorrent credential is instance-wide, so isolation is a product behavior
and deployment-writer contract, not an RBAC claim. Fence owns an effect only
for a transfer bound to all of these facts:

- a supported configured source profile;
- the profile's exact category and qBittorrent save path;
- one canonical lowercase torrent hash and real Arr download identity;
- one durable operation and reservation;
- one MediaInterlock owner tag bound to that reservation.

The owner tag plus the durable ledger defines governed ownership. Category is
required admission evidence but is not enough by itself to authorize an
effect. Every read uses an exact source/category/hash filter where the public
API supports it; every mutation names one exact ledger-owned hash. No code path
uses qBittorrent's all-torrents selector or mutates by category alone.

An unrelated category and every hash absent from the MediaInterlock ledger are
out of scope even though the credential could technically see them. Readiness
requires no competing start/resume writer for governed hashes, not exclusive
control of every hash in the qBittorrent instance. A downstream writer may own
a disjoint set only when its own contract excludes MediaInterlock owner tags.
Concurrent break-glass administration remains outside this guarantee and must
quiesce automated writers.

Disjoint selectors do not linearize the first claim of a still-unowned hash.
MediaInterlock therefore publishes one provider-neutral
`shared-qbittorrent-mutation/v1` advisory-lease contract. The deployment
supplies a regular, non-symlink lock file through an instance-level typed path;
Fence and every automated peer writer bind the same inode and use bounded
exclusive `flock` critical sections. The file is a neutral mechanism, not a
MediaInterlock-owned service: it contains no operation, hash, category, media
domain, credential, or policy, and Fence does not learn the peer writer's
domain.

Fence persists its prospective intent before any external effect, acquires the
lease, repeats the exact qBittorrent observation, rejects every foreign owner
tag, and holds the lease through its exact tag effect/read-back and durable
ownership transition. Pause, resume, tag, release, delete, containment, and
their recovery paths also acquire the lease and reobserve before effect. A
lease by itself never authorizes a hash absent from the ledger and exact
intent. Busy, missing, replaced, ambiguous, or stale lock identity inhibits the
effect; there is no blind wait or fallback. Process death releases `flock`, but
recovery still observes every possibly consumed qBittorrent effect before
retry.

Fence exposes a bounded, non-mutating local lock probe and reports the opened
file identity needed for a deployment to prove that a host-side peer and the
container see the same inode. The deployment owns creation, permissions,
mounting, and the cross-view contention test. Fence opens safely, pins the
identity for its process lifetime, and refuses symlinks, non-regular files,
replacement, or multiple links. No TCP endpoint, proxy, qBittorrent duplicate,
peer registration, or downstream lifecycle enters the product.

The current global `start_paused_enabled=true` readiness requirement is removed.
Radarr and Sonarr readiness instead proves the exact configured positive client
ID is enabled, uses the torrent protocol and qBittorrent implementation, has
`InitialState=Stop`, and has the exact source category. A second enabled client
capable of taking the same source is ambiguous and inhibits that source.
Unrelated clients are neither reconfigured nor required to add stopped. Fence
still observes the exact governed torrent stopped before tag or resume.

Category, path, tag, hash, state, or identity drift inhibits resume, release,
and deletion. Containment may pause an active ledger-owned hash, because a
mutable category label cannot turn an already sealed governed transfer into an
unowned one. An unowned transfer is never a containment target.

## External-grab adoption

Seerr, RSS, monitored entities, and operator-triggered Arr grabs are normal
flows. Arr adds them stopped using its configured video client. Fence observes
bounded Queue and History pages after a durable watermark and correlates one
exact source entity, post-watermark history event, configured client ID,
download ID, canonical hash, positive size, category, save path, and stopped
transfer. It persists a canonical `observation_fingerprint` over those exact
public observations. This is deliberately distinct from the `/release`
resource fingerprint available to Reconciler before a grab; the external path
must not claim to reconstruct that earlier resource.

Fence records an owner-bound adoption intent and reserves capacity before it
adds its tag or resumes. A webhook may reduce latency but polling is the
recovery authority. Zero matches, multiple matches, missing fields, a lost
event, another protocol/client, or divergent observations keep the transfer
stopped and create no guessed binding. The pinned compatibility audit must
first prove that public Queue/History fields can construct this exact
observation; if not, work stops at a `DecisionGap` rather than weakening
correlation. A crash before or after observation, intent, tag, or resume is
recovered by observing before repeating a possibly consumed effect.

Reconciler retains its stronger pre-admission path and native Arr ordering. The
external-adoption path does not let Reconciler rank or select releases and does
not make Fence the download client.

## Capacity and physical headroom

`video_capacity_bytes` and `max_inflight` remain logical bounds for governed
video only. Fence additionally receives one or more named physical capacity
pools. Each pool has a read-only `probe_path`, `minimum_free_bytes`, and
`safety_margin_bytes`; source profiles bind their download, staging and
canonical allocations to named pools. A probe reads free space with `statvfs`
and binds filesystem identity with `stat().st_dev`; it grants no media-tree
authority. Distinct filesystem identities
must never be collapsed into one pool, while aliases of the same filesystem
must never be counted as independent free-space supplies.

Each operation assigns future bytes not yet allocated on disk to the pool where
that allocation can occur:

- remaining governed download bytes in the download pool;
- a conservative staging allocation until an exact hardlink observation makes
  it zero, in the staging pool;
- the independent canonical bundle allocation not yet materialized, in the
  canonical pool;
- an explicit bounded sidecar allowance until exact bundle enumeration;
- any extra temporary allocation if the implementation creates more than the
  final fsynced generation.

When several liabilities share a pool they are summed once for that physical
filesystem. Already allocated downloads and retained predecessors are reflected
in observed free space and are not counted again as future allocation. Checked
integer arithmetic is mandatory. A candidate is admitted or resumed only when
the logical video budget and this predicate hold independently for every
affected pool:

```text
observed_free_bytes >= minimum_free_bytes
                       + active_future_liabilities
                       + candidate_future_liability
                       + safety_margin_bytes
```

Measurements are repeated immediately before resume, staging/import liability
transition, canonical allocation, and recovery of any such effect. Missing
pool/root binding, filesystem identity drift, unknown, overflowed, shrinking,
stale, or internally contradictory measurements fail closed. Consumption by
an unrelated domain is visible as reduced free space but is never recorded as
a MediaInterlock reservation.

This is conservative headroom, not hard capacity isolation. A concurrent
unrelated write remains a residual race. Product documentation and health must
state that only a filesystem quota, partition, or equivalent downstream
facility could create a hard allocation boundary.

## Quiescence

Fence exposes an owner-bound local quiescence request over its existing Unix
control plane. It durably stops new admission, records pause intent, pauses and
observes every ledger-owned active hash, and reports the exact unresolved
count. It never stops a process, service, unrelated transfer, or whole
qBittorrent instance. Recovery observes every possibly consumed pause before
retry. Leaving quiescence rechecks source readiness, capacity, ownership, and
physical free space before any resume; it does not blindly restore prior
activity.

This is a product primitive for safe custody and shutdown. Scheduling and
service stop order remain downstream.

## Stable bundles and hardlinked staging

An asset generation is a sealed bundle containing exactly one selected video
plus its approved contained sidecars. A bounded media-inspection interface
records embedded audio/subtitle streams and sidecar evidence. Deployment policy
may configure required languages, accepted aliases/extensions, settle interval,
and semantic quality requirements. Containment, stable double observation,
digest verification, and last-known-good retention are not configurable.

Publisher performs two equal bounded bundle enumerations separated by the
settle interval. A changed directory entry, inode identity, size, allocation,
metadata, media inspection, or digest keeps the candidate pending. Unknown
files are rejected or explicitly excluded by typed policy; they are never
silently copied into a generation.

A staging video may have multiple hardlinks because Arr can import a completed
qBittorrent payload by hardlink. It is acceptable only for a Fence-owned
acquisition whose exact hash has been durably frozen and observed stopped for
the copy interval. Publisher opens every source beneath the configured staging
root without following links, seals identity and digest, copies bytes into a
private temporary generation, verifies the copy and a second source
observation, fsyncs files and directories, and atomically commits the bundle.

Every canonical file has an inode independent of qBittorrent, Arr, Bazarr,
assisted import, and staging. Product-private links are allowed only within
Publisher-owned canonical roots and cannot expose a writer-owned inode.
Publisher returns the custody receipt only after its durable adoption point;
Fence may release the acquisition hold and restore configured seed behavior
only after that receipt. Crash recovery keeps the source frozen or re-establishes
the exact freeze before copying; it never trusts link count alone.

## Bootstrap and existing-candidate intake

Publisher provides a generic owner-bound bootstrap operation because only
Publisher may initialize its store, asset slots, and canonical generations. A
bootstrap manifest contains source profile, stable asset identity, contained
source path, Arr and catalog identity evidence, provider evidence or explicit
absence, size, allocation, digest, bundle inventory, and expected Jellyfin
binding. Intent is durable before filesystem effects; operation ID plus manifest
digest is idempotent. Drift or ambiguous catalog identity blocks.

Bootstrap does not discover deployment-specific virtual libraries, merge
UserData, repair providers, operate a reverse proxy, remove folders, edit a
database, or decide migration policy. A downstream one-shot prepares and seals
those facts, calls the public bootstrap contract, and owns its own PNR and
recovery.

The same intake state machine may accept an existing Arr-managed staging
candidate without a torrent only through a distinct `assisted` provenance. It
requires a pre-recorded owner intent, one exact Arr entity and import
observation, contained source identity, bundle digest, and publication-space
reservation. It never fabricates a torrent hash or acquisition receipt and
cannot bypass normal bundle/catalog proof. Exact public contract names are
owned by the implementation slice.

## OCI and runtime contract

The wheel continues to contain all three entrypoints. OCI output adds an
explicit Reconciler target beside Fence and Publisher. Every image:

- is built from the same exact source/version and hash-locked build inputs;
- carries standard source, revision, version, and license labels pointing to
  [the public repository](https://github.com/estevaopbs/media-interlock);
- exposes no TCP port and contains no service-manager or deployment manifest;
- supports an arbitrary non-root numeric runtime identity supplied by the
  container runtime and does not depend on a hard-coded UID-owned state path;
- provides a bounded version/config/readiness command appropriate to its
  one-shot or daemon role;
- contains only runtime dependencies and the installed wheel.

Release evidence binds wheel hash, source commit, image manifest digests,
component entrypoint, compatibility profile, and clean-checkout gates. Archive
tar bytes remain transport, not image identity.

## Compatibility and release

At the start of `MI-06`, the latest mutually compatible stable Python,
Jellyfin, Radarr, Sonarr, qBittorrent, Bazarr, Seerr, and Prowlarr releases are
resolved from primary upstream sources and frozen for the entire program.
Adapter tests state exactly what those pins prove, including stable public Arr
download-client IDs and the observation fields required by external adoption.
Older versions receive no implicit support claim.

`MI-08` may produce a local release candidate. Creating a Git tag or GitHub
Release, uploading its wheel, or pushing the three OCI images to GitHub
Container Registry is an external effect and belongs to `MI-09`, which cannot
begin without explicit user authorization.
Downstream adoption requires the immutable version, source commit, and image
digests published by that unit; a local checkout, branch, wheel path, or copied
source is not a valid deployment dependency.

Release metadata also declares the
`shared-qbittorrent-mutation/v1` capability. The OCI rehearsal bind-mounts one
deployment-supplied lock inode, proves cross-process exclusion and crash
release, and demonstrates that a synthetic peer can mutate only its unrelated
hash without MediaInterlock receiving its domain or policy.

## Test strategy

Tests detect distinct product risks rather than mirror files:

- strict source-profile parsing, projection, path translation, and root
  conflicts;
- external-grab correlation, lost events, ambiguity, wrong source/client,
  stopped-state proof, owner-tag adoption, and every crash boundary;
- shared mutation-lease identity, bounded contention, cross-process crash
  release, same-hash first-claim races, and foreign-peer opacity;
- a foreign category/hash snapshot before and after each Fence mutation path;
- logical capacity, per-filesystem `statvfs` headroom, pool aliasing, checked
  arithmetic, changing free space, and explicit no-hard-isolation reporting;
- quiescence, restart, containment, and exact governed-hash recovery;
- hardlinked source freeze, bundle stability, embedded/sidecar policy,
  independent canonical inode, and predecessor retention;
- bootstrap and assisted provenance, idempotency, drift, ambiguous binding, and
  catalog delivery;
- three OCI targets, arbitrary numeric UID, labels, health/config commands,
  clean-checkout reproduction, and immutable release metadata.

The full synthetic rehearsal contains two governed video profiles, at least one
unrelated qBittorrent transfer, and a synthetic peer writer using the same
mounted lease inode. It proves cross-process contention/crash release and that
the unrelated transfer's hash, category, tags, state, path, and counters are
unchanged while both video flows complete. It uses only disposable services and
does not claim downstream or live acceptance.

## Documentation and lifecycle

This work reopens the existing `MEDIAINTERLOCK` program at `MI-06`; it does not
create a cross-repository program. `docs/index.json` routes this specification,
one ordered slice catalog, and one active plan. Each slice uses TDD,
focused-first verification, one stable consolidated review, focused correction
review, current-document convergence, and removal of its active plan.

The specification and catalog remain only while a product unit is pending.
After the immutable release unit converges, permanent product documents are
updated and all work material plus the optional checkpoint are removed. A
downstream repository then owns a separate adoption plan under its own rules.

## Closed decisions

The source-profile model, governed ownership boundary, external observer-first
adoption, logical-plus-physical capacity semantics, lack of hard-isolation
claim, hardlink-to-copy custody, complete bundle, generic bootstrap, three OCI
targets, arbitrary non-root identity, immutable remote release prerequisite,
and downstream responsibility boundary are closed.

Exact class names, envelope field names, store migrations, helper
decomposition, and whether optional adapter credentials are projected into one
or two components are implementation choices unless they weaken these
contracts. A missing public upstream capability, contradictory invariant, or
unavailable behavioral oracle is a `DecisionGap`; ordinary schema evolution,
test decomposition, and defects are not.
