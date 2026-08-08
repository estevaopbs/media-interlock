# Reconciler

The reconciler is a bounded one-shot job. It decides which existing movie or
episode is eligible for another native upstream search and records the outcome
without becoming a release indexer or quality-ranking engine.

## Responsibilities

- Read normalized entity and publication observations through adapters or
  versioned contracts.
- Evaluate typed TOML rules for media type, age, release dates, cooldown,
  terminal horizon, language and quality constraints, budget, suppression, and
  explicit force requests.
- Observe native interactive Radarr or Sonarr release decisions, preserve their
  ordering, and request only the first approved torrent after Fence
  pre-admission.
- Record attempts, completed checkpoints, deferrals, suppressions, and stable
  reasons for rejection in its own store.
- Emit human and versioned JSON results without titles, paths, credentials, or
  high-cardinality identifiers in logs and metrics.

Movies and episodes are eligibility units. A multi-episode or season result is
accepted only when a configured rule and upstream observation map it
unambiguously to one publication operation; ambiguity defers rather than
guessing.

## Boundaries

The reconciler does not schedule itself, scrape trackers, rank releases,
control qBittorrent, publish files, write publisher or fence state, edit
Jellyfin, or migrate user data. It derives no download locator. It sends an
exact Arr release resource only back to its owning Arr API and uses versioned
Unix contracts for Fence pre-admission and observed-grab binding.

Rules created to repair one deployment's provider metadata, virtual-library
artifacts, user state, or catalog history do not belong here. Those are one-off
downstream correction utilities, not general reconciliation policy.

Technical failure, unknown upstream capability, stale observations, missing
required identifiers, divergent providers, or multiple candidates do not
advance a successful-search checkpoint. A successful native search with zero
results may advance it when the configured policy defines that observation as
complete.
