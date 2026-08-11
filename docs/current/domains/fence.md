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
- Permit an exact pre-admitted stopped magnet with pending metadata to start
  only after accounting its positive Arr release-size reservation and applying
  the owner tag; a generic zero-size torrent remains unknown.
- Poll bounded public Arr History and Queue pages to adopt a post-watermark,
  externally initiated stopped torrent only after its configured download-client
  identity, entity, hash, size, category, and save path agree. The durable
  observation fingerprint is distinct from a Reconciler release fingerprint.
- Accept a separately authorized `post_pnr_adoption` only through the local
  version-1 socket. It validates the exact configured Arr client, entity,
  canonical hash, category, and qBittorrent save path against one public
  History/Queue grab, persists its intent, then claims only one stopped,
  unowned hash with one Fence tag and durable read-back. It returns an exact
  terminal receipt without resuming that pre-existing torrent.
- Accept separately authorized `post_pnr_historical_adoption` only through the
  local version-1 socket. Its complete canonical entity set is one reservation
  and one hash tag; it permits an absent Queue only after exact complete public
  History evidence, while a present Queue must agree in full. It never broadens
  observer-first polling or acquires musical or foreign transfers.
- Activate an adopted historical hash only through the identity-free
  `post_pnr_historical_activation` authority. It persists intent before one
  leased exact start/read-back, retains the reservation and bytes, and marks it
  managed only afterward; managed ownership is quiesced exactly but does not
  consume a new-admission inflight slot.
- Observe qBittorrent transfer and seeding state through its adapter without
  treating cached or incomplete responses as authority.
- Maintain reservations after transfer completion, emit a terminal acquisition
  observation, and release custody only after an exact Publisher receipt proves
  that Publisher durably reserved and adopted the payload.
- Reoffer durable terminal acquisitions directly to Publisher until the exact
  custody receipt is accepted, including after a freeze or lost response.
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
grab bindings, the owner-bound terminal freeze request, custody receipts,
status, metrics, explicit transfer-observation, and quiescence requests. A
freeze records intent, uses the shared lease to pause and re-observe only the
exact owned terminal hash, and remains recoverable until Publisher's receipt.
It persists selector and observed identity
fingerprints but no release locator. A binding rejects a download ID/hash pair
unless the former canonically lowers to the latter. Terminal observations carry Arr's real
download ID and are persisted before the socket returns them. Status reports
only aggregate reservation counts and bytes; metrics add the bounded
shared-lease probe described below.

`post_pnr_adoption_query(operation_id)` is the recovery surface for this
explicit authority path. Before the durable tag read-back it returns no
terminal receipt; afterward it returns `post_pnr_adoption_receipt` binding the
operation to source, client, Arr entity, hash, category, save path, and Fence
reservation ID. This public receipt can contain the exact requested identity;
Fence status and metrics remain aggregate-only and never expose paths, hashes,
or operation IDs.

`post_pnr_historical_adoption_query(operation_id)` returns the corresponding
historical receipt only after durable tag read-back. Its receipt carries the
complete canonical entity set; no status, metric, or log projection does.

`post_pnr_historical_activation_query(operation_id)` returns its exact managed
receipt only after active read-back. It derives identity solely from the sealed
historical adoption and remains identical while the owned hash is active,
paused, resumed, or recovered after restart.

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
