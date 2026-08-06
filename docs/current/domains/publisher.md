# Publisher

The publisher is the sole writer of the configured canonical media roots and
its durable publication state. It converts an observed acquisition candidate into a
verified generation, advances the visible library atomically, and retains the
last known-good generation until a newer one is fully committed.

## Responsibilities

- Consume a fence-owned terminal acquisition observation without accepting its
  path as authority.
- Persist a conservative space reservation and custody intent, correlate the
  operation through Radarr or Sonarr public API observations, and return a
  durable custody receipt before Fence may release its reservation.
- Re-derive all filesystem paths from configured roots and stable identifiers;
  external strings never become path authority.
- Reject traversal, symlinks or magic links, unsafe hard links, special files,
  identity drift, and candidates outside configured roots.
- Inspect media using bounded adapters, verify expected identity and content,
  durably record intent, and use crash-safe filesystem primitives before
  committing publication state.
- Retain and garbage-collect generations without deleting the last known-good
  copy merely because staging or an upstream service is unavailable.
- Deliver committed catalog changes through durable, idempotent adapter outboxes
  and confirm observed bindings before releasing related retention holds.

Jellyfin supplies catalog observation and delivery. Bazarr and Seerr are
configured optional adapters whose smallest publisher capabilities remain to be
validated in the publisher slice; they cannot weaken custody or filesystem
commit and cannot invent a successful publication.

## Boundaries

Publisher does not write Jellyfin or other upstream databases directly,
perform one-off catalog or user-state migration, own an in-progress download,
schedule searches, manage stack lifecycle, or provide backup and restore.
Catalog failure retains publication state and reports degraded delivery; it
does not roll the filesystem back to an unverified candidate.

The publisher cannot read the fence or reconciler database. It accepts
versioned observations and intent identifiers and exposes its own status in the
same manner. Safety limits such as path containment, single-writer ownership,
two-phase durable effects, and last-known-good retention are not configurable.
It becomes ready only when canonical and staging roots are disjoint, canonical
roots are writable by its identity, and the deployment has isolated all other
writers. Playback mounts canonical media read-only.
