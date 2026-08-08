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
- Bind each admitted torrent to its qBittorrent hash, Fence-only category,
  reservation tag, and configured staging root before Fence resumes it.
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
grab intents, custody receipts, status, metrics, and explicit
transfer-observation requests. It persists selector and observed identity
fingerprints but no release locator. Terminal observations carry Arr's real
download ID and are persisted before the socket returns them. Status and metrics
report only aggregate reservation counts and bytes.

Fence readiness requires every configured download client to add work stopped,
qBittorrent automatic resume to be disabled, and no competing start/resume
writer. Where upstream APIs expose those settings, Fence verifies them; the
deployment proves the remaining credential and network isolation. An
unavailable Publisher leaves completed payloads reserved and may inhibit later
admissions rather than leaking capacity accounting.

## Boundaries

Fence does not start or stop the media stack, configure Podman or systemd,
change firewall or reverse-proxy rules, publish media, write another component's
store, or manage backup and disaster recovery. A downstream deployment may use
its readiness to control broader lifecycle, but that policy remains downstream.

Seed behavior is limited to the media acquisition represented by configured
adapters. Music-specific Lidarr or Navidrome behavior is not part of the first
release. Cross-component handoff uses versioned intent and observation
contracts, never a coordinator holding two databases.
