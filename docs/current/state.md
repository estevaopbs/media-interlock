# Current state

MediaInterlock 0.1.0 is publicly released from commit
`557a6e657baa77c2811d07bbafb06cb57c085d06` at tag
[`v0.1.0`](https://github.com/estevaopbs/media-interlock/releases/tag/v0.1.0).
The release wheel SHA-256 is
`d258c4cd739c7d8926d0f2c82a5241332e62c20c536322b9e5351ed89ace383a`.
Its immutable OCI references are:

- `ghcr.io/estevaopbs/media-interlock-reconciler:0.1.0` at
  `sha256:d912081bd0792a709f41609290d6f403c8e60cad8ef11c1dfedf607e71a17c03`;
- `ghcr.io/estevaopbs/media-interlock-fence:0.1.0` at
  `sha256:4bdde35e8bf7534f2449dab0740c29d80b5792de639bf61873154ea4359458de`;
- `ghcr.io/estevaopbs/media-interlock-publisher:0.1.0` at
  `sha256:1c37901f26221a8e25d26ce2ad4a48985f70debe03b94d5a574abbd88705393d`.

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

Reconciler has a private durable intent store, typed movie and episode policy,
native ordered Arr release selection, causal Queue/History polling, exact
Unix Fence pre-admission and grab binding, and conservative recovery across
possible release effects.

The release rehearsal ran the production HTTP adapter code against disposable
pinned-shape upstreams, invokes the Reconciler CLI, crosses both daemon Unix
contracts, restarts Fence and Publisher at durable boundaries, and proves exact
Jellyfin catalog and direct-play delivery. The local build produces a
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
- no downstream deployment should consume this candidate until its own pinned,
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
