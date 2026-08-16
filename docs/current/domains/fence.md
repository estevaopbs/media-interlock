# Fence

Fence is the sole MediaInterlock writer for acquisition reservations and
qBittorrent mutation. It admits native Arr releases under configured capacity
and inflight limits, records ownership before tagging/resuming a matching
torrent, and retains video capacity through publication custody.

Fence observes `radarr` and `sonarr` reservations for video publication and,
when configured, `lidarr` reservations created by its music Reconciler. Every
observation needs the exact qBittorrent hash, source category, configured save
path, and `fence:` reservation tag. Ambiguity, absence, or failed read-back has
no mutation. Existing Lidarr queue items, transfers owned by another actor, and
all untagged music remain outside its authority.

Video health is durable. The first metadata-pending observation starts a
configured deadline. Once metadata exists, only unchanged downloaded bytes
combined with zero availability and peers form a no-progress failure; metadata
or progress clears the failure count. After the configured number of samples,
Fence persists an invalidation, blocklists the exact Arr History row, and asks
qBittorrent to remove only that owned hash. Metadata-pending candidates keep
their arbitrary path; partial candidates let qBittorrent remove its hash-owned
content. Failed blocklist/delete confirmation retains the candidate and prevents
replacement.

Music candidate health is separate. The configured Lidarr policy bounds its
metadata deadline, no-peer/no-progress deadline, candidate count, and whether
an invalid payload is deleted. Fence persists the invalidation before deletion
and exposes it once to the music Reconciler; the Reconciler durably rejects the
sealed release before acknowledgement. Music never enters the Publisher path.
