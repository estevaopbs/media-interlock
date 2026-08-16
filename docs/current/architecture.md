# Architecture

MediaInterlock runs as one `media-interlock` process in one OCI image. It owns
one SQLite database at `media_interlock.state_dir/state.sqlite3`. Fence,
Publisher, and Reconciler remain separate internal domains with namespaced
records and explicit service calls, but never use a socket, subprocess, or
component-specific store.

```text
Radarr/Sonarr inventory --> Reconciler --> Fence --> qBittorrent
                                           |          |
                                           +-----> Publisher --> Jellyfin
Lidarr missing albums -----> Reconciler --> Fence --> qBittorrent
```

The runtime starts bounded internal tasks for Fence observation, Publisher
retries, Arr-import reconciliation, and upgrade scheduling. A task failure is
isolated and retried; invalid configuration or unreadable state stops startup
before an external effect. Synchronous adapter I/O is run outside the event
loop.

Fence owns only exact tagged transfers. Video custody remains bound to
Publisher and Jellyfin. A configured Lidarr source has no Publisher path: it
tracks only new candidates selected by the music Reconciler, validates the same
hash/category/save-path/tag identity, and releases capacity after Lidarr
imports or the candidate is invalidated. It never adopts an existing Lidarr
queue, music category, or untagged transfer.

Publisher verifies one Arr-correlated staging bundle, publishes independent
canonical files, and confirms the selected Jellyfin item. Its bounded import
cursor may bootstrap an Arr `downloadFolderImported` history item that predates
the Publisher, but only after its path is translated below the configured
staging root. It never scans a library tree.

Reconciler inventories native Radarr and Sonarr cutoff work. Native Arr remains
the ranker. The scheduler records completed searches separately from technical
failures: completed attempts receive a stable cooldown calculated from their
count and policy revision; transient failures receive a separate bounded retry.
Episode scheduling retains priority but round-robins series scopes.

The independent Lidarr scheduler inventories monitored albums without files.
Its search budgets, release-age cooldown, maximum attempts, format filters, and
candidate limit are separate from video policy. It asks Lidarr for releases in
native order and may reject a release only for its configured score, format, or
seed evidence; it never ranks trackers itself.

An owned video candidate with no metadata past its configured deadline, or with
known metadata but no bytes/prospects, is persisted as invalid before any
effect. The runtime blocklists its exact Arr History row, removes only the
revalidated qBittorrent hash, releases capacity, and writes a replacement event
with video-only exponential backoff. The event is resolved by a valid Arr
response, not by an unbounded diagnostic release query.

The first opening of a unified state directory imports relevant v1 component
records once under an atomic marker. It neither copies media nor removes legacy
databases.
