# Current state

MediaInterlock has implemented its shared safety slice and the Fence and
Publisher verticals.
Fence has a private durable reservation store,
version-1 local Unix endpoint, bounded health/metrics, exact qBittorrent
control, optional configured-indexer Prowlarr readiness, terminal observation,
conservative custody receipts, and restart reconciliation. Publisher has a
private durable publication store, per-asset sealed bundles and stable slots,
asset-local predecessor retention, Arr identity correlation, bounded Jellyfin
catalog observation, and exact static direct-play verification before delivery.

Consequently:

- qBittorrent, Prowlarr, Radarr, Sonarr, Jellyfin, Bazarr, and Seerr are
  contract-tested only against disposable HTTP services at their pinned
  development profiles; Reconciler remains pending convergence;
- no live, hardware, filesystem, container, or upstream-service acceptance has
  been performed;
- no downstream deployment should consume this repository yet.

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

The current development cycle is pinned to Python 3.14.6, Jellyfin 10.11.11,
Radarr 6.3.0.10514, Sonarr 4.0.19.2979, qBittorrent 5.2.3, Bazarr 1.6.0, Seerr
3.4.1, and Prowlarr 2.5.2.5491. These pins remain fixed for the cycle and
define what future adapter tests prove. MediaInterlock does not promise
compatibility with untested older releases.
