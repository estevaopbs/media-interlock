# Reconciler

Reconciler periodically evaluates existing Radarr movie and Sonarr episode
files. It asks Arr for a native interactive release decision only when a
configured entity is due and all budget and Fence preconditions hold. It does
not scrape trackers, rank releases, or control qBittorrent directly.

Every completed, syntactically valid Arr response records a generation-specific
checkpoint and consumes the normal search budget. Its next search time is
persisted as `base * multiplier^(completed_attempts - 1)`, capped by policy.
Changing `schedule_policy_revision` makes an old checkpoint eligible without a
state migration. Valid empty Arr responses are completed searches.

Timeouts, response limits, transport errors, HTTP/schema failures, and other
technical failures do not consume completed-search budget or create a completed
checkpoint. They use a separate bounded technical retry. The release endpoint
has its own configurable timeout and response byte cap.

Within semantic priority, episode work is round-robin by Sonarr series ID.
Movies use their own movie ID. An invalidated Fence candidate creates a
replacement event for the exact Arr entity; it outranks ordinary upgrade work
only after its configured video replacement delay and keeps normal budgets and
grab limits.

When `[sources.lidarr]` is configured, music uses a separate durable schedule.
It considers only monitored albums that Lidarr still reports without files,
then asks Lidarr for releases in its native order. The configured music policy
controls its own age gate, completed-search cooldown, retries, budgets, score,
and format requirements. A Fence invalidation rejects that exact sealed release
before the next native candidate is considered. A successful import converges
only after Lidarr stops reporting the album missing.
