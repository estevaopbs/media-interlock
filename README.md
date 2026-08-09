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

MediaInterlock 0.1.3 is the current immutable public
[GitHub release](https://github.com/estevaopbs/media-interlock/releases/tag/v0.1.3).
Release 0.1.2 remains preserved as its immutable predecessor.
Release 0.1.1 remains preserved but its OCI images use Python 3.14.6 rather than
the fixed 3.14.7 compatibility profile. Version 0.1.3 adds per-operation
Publisher status and exact terminal receipts. The released artifacts have been
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
