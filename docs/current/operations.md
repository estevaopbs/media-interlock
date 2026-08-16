# Operations

The supported runtime is one immutable MediaInterlock OCI image with entrypoint
`media-interlock`. Normal execution is:

```text
media-interlock --config /etc/media-interlock/config.toml --daemon
```

`--version`, `--check-config`, and `--status` are short administrative probes.
They do not create a daemon, supervise another process, or make upstream API
calls. `--status` verifies local configuration and durable state readability.

The configuration has one `[media_interlock] state_dir` and typed `[fence]`,
`[publisher]`, and `[reconciler]` sections. Each video reconciliation policy
sets its policy revision, release timeout and response cap, completed-search
cooldown, technical retry, budgets, quality filters, and cutoff requirements.
`[publisher.import_reconciliation]` controls bounded Arr history intake. On a
source's first cursor, `initial_history_lookback_days` limits intake to imports
dated within that many days; older records advance the cursor without becoming
publication candidates. A value of `0` creates a forward-only baseline. Later
polls are incremental by durable history ID.
`[fence.video_candidate_health]` separately controls video metadata/progress
deadlines and replacement backoff. When `[sources.lidarr]` is present,
`[fence.music]` controls only new Lidarr candidate health: seed evidence,
metadata and progress deadlines, per-cycle candidate limit, and invalid-payload
deletion. `[reconciler.music]` has its own age gate, cooldown scale, horizon,
budgets, scoring, and format requirements. These policies neither adopt an
existing Lidarr queue nor select Spotify/Aurral albums.

Deployment owns the Quadlet/systemd lifecycle, paths, mounts, networks,
secrets, backups, and live acceptance. It pins an immutable public image digest
and must not build from or bind mount a product checkout. MediaInterlock does
not install units, run a one-shot migration, or scan/copy a full media library.

The process requires source-specific Arr clients to add torrents stopped.
Video staging/canonical roots must be disjoint; the Lidarr source has only its
configured download root. qBittorrent mutations are
serialized by the configured shared lease and are always revalidated against
the exact Fence tag, category, save path, and hash.
