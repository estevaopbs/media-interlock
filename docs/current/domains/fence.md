# Fence

Fence is the sole MediaInterlock writer for video acquisition reservations and
qBittorrent mutation. It admits native Arr releases under configured capacity
and inflight limits, records ownership before tagging/resuming a matching
torrent, and retains capacity through publication custody.

Fence observes only `radarr` and `sonarr` reservations in its own ledger. An
observation needs the exact qBittorrent hash, source category, configured save
path, and `fence:` reservation tag. Ambiguity, absence, or failed read-back has
no mutation. Lidarr, music categories, and transfers owned by another actor are
outside its authority.

Video health is durable. The first metadata-pending observation starts a
configured deadline. Once metadata exists, only unchanged downloaded bytes
combined with zero availability and peers form a no-progress failure; metadata
or progress clears the failure count. After the configured number of samples,
Fence persists an invalidation, blocklists the exact Arr History row, and asks
qBittorrent to remove only that owned hash. Metadata-pending candidates keep
their arbitrary path; partial candidates let qBittorrent remove its hash-owned
content. Failed blocklist/delete confirmation retains the candidate and prevents
replacement.
