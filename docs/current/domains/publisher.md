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

Each Arr-derived asset binds to a stable logical slot, an immutable bundle
generation, and an asset-local predecessor. First publication must not replace
an existing slot; later publication atomically exchanges only that asset's
bundle. A notification acknowledgement is submission only. Delivery requires
exactly one catalog item matching the configured library, translated logical
path, type, provider identity, source identity and size, followed by full static
direct-play hash verification. Lost effects and conflicts retain both candidate
and predecessor for later observation; they never cause blind filesystem
rollback.

Jellyfin supplies catalog observation and delivery. Bazarr has an authenticated
status readiness capability and continues to own subtitle work. Seerr has an
authenticated settings readiness capability and continues to own availability
projection from its configured media server. These optional adapters cannot
weaken custody or filesystem commit and cannot invent a successful publication.

## Boundaries

Publisher does not write Jellyfin or other upstream databases directly,
perform one-off catalog or user-state migration, own an in-progress download,
schedule searches, manage stack lifecycle, or provide backup and restore.
Catalog failure retains publication state and reports degraded delivery; it
does not roll the filesystem back after a possibly consumed external effect.

The publisher cannot read the fence or reconciler database. It accepts
versioned observations and intent identifiers and exposes its own status in the
same manner. Safety limits such as path containment, single-writer ownership,
two-phase durable effects, and last-known-good retention are not configurable.
It becomes ready only when canonical and staging roots are disjoint, canonical
roots are writable by its identity, and the deployment has isolated all other
writers. Playback mounts canonical media read-only.
