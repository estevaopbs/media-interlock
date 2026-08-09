# Acquisition fence

The fence is the sole writer of acquisition admission state and the configured
qBittorrent control surface. It ensures that downloads begin or resume only
under observed, durable capacity and concurrency constraints.

## Responsibilities

- Pre-admit idempotent Arr release intents against bounded space, inflight,
  source-specific category, priority, and concurrency policy before a download
  ID exists.
- Persist owner-bound intent before external effects and adopt or repeat an
  effect only after observing its exact postcondition.
- Bind each admitted torrent to its real Arr download ID and matching canonical
  qBittorrent hash, source category, reservation tag, and source qBittorrent
  save path before Fence resumes it.
- Poll bounded public Arr History and Queue pages to adopt a post-watermark,
  externally initiated stopped torrent only after its configured download-client
  identity, entity, hash, size, category, and save path agree. The durable
  observation fingerprint is distinct from a Reconciler release fingerprint.
- Observe qBittorrent transfer and seeding state through its adapter without
  treating cached or incomplete responses as authority.
- Maintain reservations after transfer completion, emit a terminal acquisition
  observation, and release custody only after an exact Publisher receipt proves
  that Publisher durably reserved and adopted the payload.
- Report inhibited, ready, degraded, and recovering states over health, metrics,
  CLI, and a versioned Unix socket.
- Use Prowlarr readiness only when configured policy requires that upstream
  capability; it never treats a proxy response as proof of a successful search
  or download.

Prowlarr is a Fence-owned optional readiness capability. Its adapter may report
health plus at least one enabled configured indexer to inhibit new admission,
but cannot rank a
release, initiate a search, or prove a download outcome.

Unknown or ambiguous state closes new admission. Fence unavailability must not
stop playback of an already published generation. Recovery reconciles its own
durable intent with observed qBittorrent effects and never opens admission merely
because a generic timeout elapsed.

Fence's local socket accepts canonical version-1 pre-admission and observed
grab bindings, custody receipts, status, metrics, explicit transfer-observation,
and quiescence requests. It persists selector and observed identity
fingerprints but no release locator. A binding rejects a download ID/hash pair
unless the former canonically lowers to the latter. Terminal observations carry Arr's real
download ID and are persisted before the socket returns them. Status reports
only aggregate reservation counts and bytes; metrics add the bounded
shared-lease probe described below.

Fence readiness requires every configured Arr download client to add work
stopped with its exact category. qBittorrent's global start-paused preference
is not an ownership oracle. Every tag, resume, or pause takes the
deployment-supplied single-inode `shared-qbittorrent-mutation/v1` advisory
lease and reobserves the exact hash; a pre-existing `fence:` owner tag inhibits
a second claim. Its bounded metrics probe reports only lease availability and,
when acquired, the opened device/inode identity; it exposes neither a path nor
any peer domain. An
unavailable Publisher leaves completed payloads reserved and may inhibit later
admissions rather than leaking capacity accounting.

Fence combines its logical video ledger with read-only `statvfs` headroom for
configured capacity pools. Future download, staging, and canonical liabilities
are summed once per pool; unknown, overflowed, or insufficient observations
inhibit admission or resume. This is conservative headroom, not a filesystem
quota or a guarantee against a concurrent unrelated write.

Its local `quiesce` request durably inhibits new admission, pauses only active
ledger-owned hashes, and reports unresolved pause intents. Reopening rechecks
source readiness, ownership, logical capacity, and physical headroom before
any exact resume; it never stops qBittorrent, another process, or an unrelated
transfer.

## Boundaries

Fence does not start or stop the media stack, configure Podman or systemd,
change firewall or reverse-proxy rules, publish media, write another component's
store, or manage backup and disaster recovery. A downstream deployment may use
its readiness to control broader lifecycle, but that policy remains downstream.

Seed behavior is limited to the media acquisition represented by configured
adapters. Cross-component handoff uses versioned intent and observation
contracts, never a coordinator holding two databases.
