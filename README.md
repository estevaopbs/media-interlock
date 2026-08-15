# MediaInterlock

MediaInterlock is a provider-neutral, fail-closed controller for video
acquisition and publication in self-hosted media libraries. It keeps native
Arr ranking authoritative, admits only exact Fence-owned qBittorrent work, and
publishes verified bundles for Jellyfin.

## Project status

MediaInterlock 0.1.25 is the current immutable public release. It is one OCI
image, one container, one process, and one SQLite state database. Fence,
Publisher, and Reconciler are internal modules that communicate in memory;
there are no component images, daemon sockets, units, or recurring downstream
scripts. A deployment consumes a public digest, never this checkout.

The release adds bounded Arr-import reconciliation for pre-existing staging
imports, explicit release-response limits and technical retries, generation
based exponential cooldowns, fair series scheduling, and exact video-candidate
health recovery. It does not manage Lidarr, music torrents, stack lifecycle,
backups, or one-off data migration.

## Principles

- Unknown or ambiguous evidence closes the affected operation.
- All recurring work runs inside the MediaInterlock process; local deployment
  code is lifecycle or on-demand support only.
- Arr remains the source of release ordering; MediaInterlock narrows only with
  configured policies.
- Candidate mutation requires the exact Fence reservation, qBittorrent hash,
  category, save path, and owner tag.
- Durable state records intent before effects and makes recovery idempotent.

Read the [architecture](docs/current/architecture.md) and
[operations](docs/current/operations.md) for the product contract.
