# MediaInterlock

MediaInterlock is a provider-neutral, fail-closed control system for safely
reconciling, acquiring, and publishing media in self-hosted libraries.

The project is intentionally split into three independently deployable
components:

| Component | Responsibility |
| --- | --- |
| Reconciler | Selects eligible movies and episodes and requests bounded searches through native upstream APIs. |
| Publisher | Verifies acquired files and atomically advances a canonical, last-known-good media library. |
| Fence | Admits and observes downloads under durable capacity, concurrency, and seeding constraints. |

MediaInterlock is not a Jellyfin plugin. Jellyfin, Radarr, Sonarr, qBittorrent,
Bazarr, Seerr, and Prowlarr are adapters around a provider-neutral core and are
active only when configured.

## Project status

MediaInterlock 0.1.23 is the current immutable public
[GitHub release](https://github.com/estevaopbs/media-interlock/releases/tag/v0.1.23).
Version 0.1.22 remains preserved as its immutable predecessor.
Version 0.1.23 bounds Jellyfin catalog observation in smaller pages so rich
`MediaSources` payloads remain below the HTTP response limit without reducing
the 1,000-item observation ceiling. Version 0.1.22 added a distinct configurable Publisher gate for subtitle
languages, so audio-only language evidence cannot satisfy a subtitle
requirement. Version 0.1.21 added a product-owned automatic Reconciler loop. It inventories
existing Radarr and Sonarr files against their native quality profiles, runs
bounded due searches with durable per-file checkpoints, and exposes typed
configuration for geometric cooldowns, terminal searches, score and custom
format filters, search budgets, and grab budgets. Version 0.1.20 exposes each
private immutable generation as regular hardlinks
at the exact relative path reported by Arr, so Jellyfin scans the native movie
or series hierarchy. Version 0.1.19 introduced the canonical relative route but
used file symlinks, which Jellyfin does not catalog. Version 0.1.18 baselines a
newly observed Arr source from its highest public History ID without trying to
correlate historical grabs with the current Queue.
Version 0.1.17 lets Fence adopt an exact external stopped magnet whose Queue
size is still zero by accounting the positive release size sealed in Arr
History. Version 0.1.16 repairs that identity and an unobserved catalog path
binding for already-pending generations after a deployment upgrade. Version
0.1.15 seals Jellyfin provider identity in each published generation as
a read-only NFO sidecar. Version 0.1.14 made Fence durably retry completed acquisitions to Publisher
until the exact custody receipt is accepted. Version 0.1.13 lets an exactly
correlated, pre-admitted stopped magnet fetch
its metadata while accounting the positive Arr release size already reserved
before the grab. Version 0.1.12 gives Reconciler release searches a bounded
90-second adapter window. Version 0.1.11 adds optional qBittorrent bearer API-key authentication while
retaining the session-login contract. Version 0.1.10 correctly scopes
qBittorrent download-client IDs to each Arr API. Version 0.1.9
makes the Fence and Publisher daemons handle process termination
explicitly so OCI runtimes can stop them promptly and release their sockets,
stores, leases, and writer locks cleanly. Version 0.1.8 added explicit
historical activation: only an
already sealed `post_pnr_historical_adoption` may be durably started and
managed by Fence, while retaining its reservation and capacity accounting.
Release 0.1.1 remains preserved but its OCI images use Python 3.14.6 rather than
the fixed 3.14.7 compatibility profile. Version 0.1.3 added per-operation
Publisher status and exact terminal receipts; 0.1.4 translates a shared
Arr-visible import prefix into each source's distinct Publisher staging root.
Version 0.1.5 preserves that functional contract and corrects the release
status incorporated into the public README and wheel metadata. Version 0.1.6
adds explicit post-PNR Fence adoption: one deployment-authorized existing Arr
eligibility is durably tagged, remains stopped, and has a recoverable exact
receipt without exposing Fence's private store. Version 0.1.7 adds the
separate `post_pnr_historical_adoption` contract: an explicitly authorized
historical singleton or Sonarr episode pack is claimed once by its complete
canonical entity set, including when its Arr Queue record is already absent.
The released artifacts have been
exercised only with disposable upstream services, durable local state, Unix
sockets, one wheel, and OCI images for all three components. Nothing in this
repository has been installed in or validated against a live media stack. See
[current state](docs/current/state.md) for the precise boundary.

## Principles

- Unknown or ambiguous state closes new acquisition; it never destroys the
  last known-good publication.
- Each component owns its process, durable state, and side effects.
- Components communicate through versioned contracts, not each other's
  databases.
- Safety invariants are code, while deployment and reconciliation policy are
  validated TOML configuration.
- Backup, disaster recovery, stack lifecycle, and one-off data migrations are
  responsibilities of the deployment environment, not this product.
- Tests cover risks and contracts without mirroring every source file or
  repeating the same behavior at every layer.

Start with the [architecture](docs/current/architecture.md), then load only the
component document relevant to the change. Contributors should read
[CONTRIBUTING.md](CONTRIBUTING.md) and the repository [agent instructions](AGENTS.md).

MediaInterlock is licensed under the [MIT License](LICENSE).
