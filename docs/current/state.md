# Current state

MediaInterlock 0.1.8 is the immutable public downstream-consumption release.
It adds the separate durable `post_pnr_historical_activation` authority. It
starts only a confirmed historical adoption, retains its Fence ownership and
bytes, and marks it managed so it no longer occupies an admission concurrency
slot. Its terminal receipt remains recoverable through explicit query across
restart and quiescence.
Its annotated tag
[`v0.1.8`](https://github.com/estevaopbs/media-interlock/releases/tag/v0.1.8)
resolves to its immutable source commit. The release publishes one wheel and
three public OCI manifests, each executing Python 3.14.7. Its canonical
artifact manifest binds all identities to one source revision and version.
Version 0.1.7 remains preserved as the preceding immutable release. Its annotated tag
[`v0.1.7`](https://github.com/estevaopbs/media-interlock/releases/tag/v0.1.7)
resolves to its immutable source commit. The release publishes one wheel and
three public OCI manifests, each executing Python 3.14.7. Its post-PNR Fence
adoption path accepts one deployment-authorized existing Arr eligibility and
returns an exact durable receipt only after the stopped, unowned qBittorrent
hash has been tagged and read back under the shared mutation lease. The path is
separate from observer-first polling and never resumes that torrent. The public
GitHub release is immutable and the wheel and manifest identities are verified
again without credentials after publication.

Version 0.1.5 remains preserved at tag
[`v0.1.5`](https://github.com/estevaopbs/media-interlock/releases/tag/v0.1.5)
as the preceding immutable release. It preserves the 0.1.4 functional contract
and corrects the release status incorporated into README and wheel metadata.

Version 0.1.3 added a version-1 Publisher Unix
operation query with durable accepted, pending, catalog-confirmed, conflict,
unavailable, and terminal visible-confirmed results. Its exact terminal receipt
is emitted only after the existing Jellyfin binding and static direct-play gate
and binds the public operation, asset, generation digest, library, item, media
source, and expected catalog path.

Version 0.1.4 introduced
`arr_import_path_prefix` as the Arr-visible namespace and translates only its
canonical relative suffix below each source-specific Publisher staging root.
The release rehearsal uses one shared `/data/library` Arr prefix with distinct
movie and show staging roots, including assisted socket intake, sidecars, lost
response recovery, idempotent retry, and exact terminal receipt. Sonarr
correlation requires the requested episode ID and never substitutes a
numerically matching series ID. Version 0.1.4 remains preserved at tag
[`v0.1.4`](https://github.com/estevaopbs/media-interlock/releases/tag/v0.1.4)
as the preceding immutable release.

Version 0.1.3 remains preserved at tag
[`v0.1.3`](https://github.com/estevaopbs/media-interlock/releases/tag/v0.1.3)
as the preceding immutable release.

Version 0.1.2 remains preserved at tag
[`v0.1.2`](https://github.com/estevaopbs/media-interlock/releases/tag/v0.1.2)
as the preceding immutable release.

Version 0.1.1 remains preserved at tag
[`v0.1.1`](https://github.com/estevaopbs/media-interlock/releases/tag/v0.1.1),
but its three OCI images execute Python 3.14.6 rather than the compatibility
profile's fixed Python 3.14.7. It is therefore not the conforming OCI release.

Version 0.1.0 remains preserved at tag
[`v0.1.0`](https://github.com/estevaopbs/media-interlock/releases/tag/v0.1.0),
but it was published before release immutability was enabled and is not the
immutable downstream-consumption release.

The shared safety slice and Fence, Publisher, and Reconciler verticals have
converged. Fence has strict
Radarr movie and Sonarr episode source profiles, observer-first external Arr
grab adoption, per-source qBittorrent paths, a neutral bounded mutation lease,
physical headroom, and exact local quiescence. It has a private durable
reservation store, version-1 local Unix endpoint, bounded health/metrics,
optional configured-indexer Prowlarr readiness, terminal observation,
conservative custody receipts, and restart reconciliation. Publisher has a
private durable publication store, twice-observed sealed asset bundles, exact
Fence freezes for hardlinked staging copies, independent canonical inodes,
stable slots, asset-local predecessor retention, distinct bootstrap/assisted
provenance, Arr identity correlation, bounded Jellyfin catalog observation,
and exact static direct-play verification before delivery.
Publisher also exposes a per-operation public projection without requiring
access to its private SQLite state; aggregate metrics remain metadata-free.

Reconciler has a private durable intent store, typed movie and episode policy,
native ordered Arr release selection, causal Queue/History polling, exact
Unix Fence pre-admission and grab binding, and conservative recovery across
possible release effects.

The release rehearsal runs the production HTTP adapter code against disposable
pinned-shape upstreams, invokes the Reconciler CLI, crosses both daemon Unix
contracts, restarts Fence and Publisher at durable boundaries, and proves
pending, wrong-binding, response-loss recovery, terminal receipt, and exact
Jellyfin catalog/direct-play delivery. The local build produces a
source-date-controlled wheel and Reconciler, Fence, and Publisher OCI image
manifest digests using a hash-locked build bootstrap. Its canonical artifact
manifest binds the source revision, version, wheel hash, and image identities.
Archive tar timestamps are not a reproducibility identity.

Release proof is limited to those artifacts and disposable checks.
Consequently:

- qBittorrent, Prowlarr, Radarr, Sonarr, Jellyfin, Bazarr, and Seerr are
  contract-tested only against disposable HTTP services at their pinned
  development profiles;
- no live, hardware, filesystem, container, or upstream-service acceptance has
  been performed;
- no downstream deployment should promote this release until its own pinned,
  live acceptance is complete.

## Authority split

The public project will own provider-neutral behavior for the reconciler,
publisher, and fence. Downstream deployment repositories own service topology,
host paths, secrets, lifecycle, reverse proxy, backup, restore, disaster
recovery, and one-off data correction or migration.

An existing downstream implementation is research and extraction input, not a
release candidate. Its one-off catalog/user-state corrections, migration,
backup, reverse proxy, service lifecycle, and live promotion machinery are
explicitly outside this product.

## Versioning and compatibility

The monorepo has one SemVer version for all three components. Development stays
in `0.x` while interfaces and adapters stabilize. Version `1.0.0` requires all
three components, all adapters declared for that release, integrated safety
gates, packaging, and operator documentation.

The current development cycle is pinned to Python 3.14.7, Jellyfin 10.11.11,
Radarr 6.3.0.10514, Sonarr 4.0.19.2979, qBittorrent 5.2.3, Bazarr 1.6.0, Seerr
3.4.1, and Prowlarr 2.5.2.5491. These pins remain fixed for the cycle and
define what future adapter tests prove. MediaInterlock does not promise
compatibility with untested older releases.
