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

The shared safety mechanisms are implemented, but the three component verticals
and release packaging are still in development. Nothing in this repository has
been installed in or validated against a live media stack. See [current
state](docs/current/state.md) for the precise boundary.

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
