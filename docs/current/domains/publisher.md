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
  custody receipt only after a canonical adoption point permits Fence to
  release its reservation.
- Re-derive all filesystem paths from configured roots and stable identifiers;
  external strings never become path authority.
- Seal exactly one contained video and its selected contained sidecars through
  two equal no-follow observations separated by the source profile's bounded
  settle interval. Source profiles may narrow sidecar extensions and require
  language or container-inspection evidence, including configured aliases;
  containment, double observation, digest verification, and inventory bounds
  are invariant.
- Record immutable payload inspection evidence (container plus any bounded
  audio or subtitle-language evidence supplied by the configured inspector) in
  the sealed bundle. Inspection failure, drift, or malformed evidence keeps
  the candidate pending.
- Reject traversal, symlinks or magic links, special files, identity drift,
  unsupported matching sidecars, and candidates outside configured roots.
- Treat a staging hardlink as evidence requiring a successful owner-bound
  Fence freeze for the exact terminal acquisition. Copy the sealed bundle to
  private, independent canonical inodes, re-observe its sources during copy,
  fsync the complete generation, and atomically expose it before custody can
  be released.
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

Publisher also owns two non-Fence intake provenances. A bootstrap presents a
complete owner-bound manifest containing source, Arr and catalog identities,
provider identity or an explicit provider absence, expected catalog path, and
the full sealed bundle inventory and inspection evidence;
the daemon re-observes the contained source and accepts only an exact manifest
match. An assisted candidate first records an owner intent bound to the same
manifest digest, then completes through the same bundle and generation state
machine only after one exact Arr import observation agrees with its path and
asset identity. Neither path fabricates a torrent hash or produces a Fence
receipt.

Publisher exposes each operation through its version-1 Unix contract without
exposing its private store. `publisher_operation_query` has an empty body and
uses the envelope `operation_id`. Its nonterminal response is
`publisher_operation_status` with exactly one state:

- `accepted`: durable owner intent or custody exists, but no sealed candidate
  has entered publication;
- `pending`: publication has started but exact catalog observation is absent;
- `catalog-confirmed`: the configured library, expected path, item, and media
  source match, but static direct-play bytes and digest are not yet confirmed;
- `conflict`: submitted intake contradicts durable identity;
- `unavailable`: the operation is unknown or cannot be represented safely.

Only full static direct-play verification changes the public result to
`publisher_operation_receipt` with `state=visible-confirmed`. That terminal,
idempotent receipt binds source, upstream and media identities, asset slot,
generation UUID and payload SHA-256, configured library, Jellyfin item and
media-source identities, and expected catalog path. `publisher_assisted_complete`
returns this same projection: a nonterminal processor result is `pending`, never
an intake-success substitute. Re-querying after a crash or lost response returns
the durable projection. A pre-0.1.3 delivered record lacks the new receipt
binding and remains `unavailable` until Publisher repeats exact catalog and
direct-play verification.

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
versioned observations and owner-bound intake contracts and exposes its own
aggregate status plus per-operation projection in the same manner. Its metrics
remain aggregate and never contain operation IDs, paths, titles, or digests.
Safety limits such as path containment, single-writer
ownership, two-phase durable effects, and last-known-good retention are not
configurable.
It becomes ready only when canonical and staging roots are disjoint, canonical
roots are writable by its identity, and the deployment has isolated all other
writers. Playback mounts canonical media read-only.
