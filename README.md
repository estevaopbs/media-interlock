# MediaInterlock

MediaInterlock is a provider-neutral, fail-closed controller for video
acquisition/publication and new Lidarr music-candidate health in self-hosted
media libraries. It keeps native Arr ranking authoritative, admits only exact
Fence-owned qBittorrent work, and publishes verified video bundles for Jellyfin.

## Project status

MediaInterlock 0.1.38 is the current immutable public release. It is one OCI
image, one container, one process, and one SQLite state database. Fence,
Publisher, and Reconciler are internal modules that communicate in memory;
there are no component images, daemon sockets, units, or recurring downstream
scripts. A deployment consumes a public digest, never this checkout.

The release also verifies the typed Publisher import-reconciliation policy
during runtime construction, so a configuration accepted by `--check-config`
cannot fail later when the persistent process starts. Its local status probe
uses a read-only SQLite connection, so it remains healthy while the sole
runtime process owns the writer lock. When the runtime starts after a persisted
quiescence, it first recovers the owned work and then reopens it. Its periodic
Fence tick retries durable recovery intents before observing new external work.
It retains bounded Arr-import reconciliation: on a source's first cursor it
considers only the configured initial history lookback, then advances durably
to incremental intake. It does not enumerate a media tree. A completed video
candidate is removed from health tracking; a legacy terminal health record is
discarded safely on restore instead of preventing the sole runtime from starting.
Its bounded initial Arr-import recovery can seal a completed Arr hardlink into
an independent canonical copy; one rejected historical item cannot stall later
imports in the same bounded page. The deterministic UUIDv5 identity of that
historical import is accepted as its canonical generation identity, and the
normal Publisher worker resumes a durable generation intent after restart. It
can also adopt an existing public route only after rechecking it byte-for-byte
against the verified generation; a differing file remains blocked. It also retains explicit
release-response limits and technical retries, generation based exponential
cooldowns, fair series scheduling, and exact video-candidate health recovery.
Publisher intake, recovery, and bounded Arr-history reconciliation share one
in-process work gate, so they cannot race the same durable publication.
For configured new Lidarr requests, it can reject only its own unhealthy
torrent candidate and ask Lidarr for the next native release. It does not adopt
an existing Lidarr queue, select albums for Spotify/Aurral, manage stack
lifecycle, backups, or one-off data migration.

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
