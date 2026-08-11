# Reconciler

The reconciler supports bounded on-demand passes and a product-owned recurring
loop. It decides which existing movie or episode is eligible for another native
upstream search and records the outcome without becoming a release indexer or
quality-ranking engine.

## Responsibilities

- Inventory existing Radarr movie files and Sonarr episode files and compare
  their quality and custom-format scores with their native quality profiles.
- Evaluate typed TOML rules for media type, age, release dates, cooldown,
  terminal horizon, language and quality constraints, budget, suppression, and
  explicit force requests.
- Observe native interactive Radarr or Sonarr release decisions, preserve their
  ordering, and request only the first approved torrent after Fence
  pre-admission.
- Record generation-specific completed checkpoints, rolling budgets, durable
  search/grab intents, and terminal searches in its own store.
- Emit human and versioned JSON results without titles, paths, credentials, or
  high-cardinality identifiers in logs and metrics.

Movies and episodes are eligibility units. A multi-episode or season result is
accepted only when a configured rule and upstream observation map it
unambiguously to one publication operation; ambiguity defers rather than
guessing.

## Boundaries

The reconciler does not scrape trackers, replace Arr ranking, control
qBittorrent directly, publish files, write publisher or fence state, edit
Jellyfin, or migrate user data. It derives no download locator. It sends an
exact Arr release resource only back to its owning Arr API and uses versioned
Unix contracts for Fence pre-admission and observed-grab binding.

Rules created to repair one deployment's provider metadata, virtual-library
artifacts, user state, or catalog history do not belong here. Those are one-off
downstream correction utilities, not general reconciliation policy.

Technical failure, unknown upstream capability, stale observations, missing
required identifiers, divergent providers, or multiple candidates do not
advance a successful-search checkpoint. A durable pre-POST intent is recovered
through its pre-admission and exact release path; an intent recorded before a
possibly consumed POST is observed first and is never blindly posted again.
The public Arr download ID is retained for downstream correlation, while its
matching canonical lowercase torrent hash is supplied separately to Fence.
A successful native search with zero results may advance it when the configured
policy defines that observation as complete.

Automatic scheduling is per Arr file generation. A replacement file creates a
new generation and therefore a new schedule. The cooldown is
`ceil(base_seconds * multiplier ^ floor(age / age_step))`, optionally capped.
Minimum age, terminal horizon, final-search behavior, attempt limits, search
budgets, grab budget, score thresholds, and required or forbidden custom formats
are separate typed movie and episode parameters. Technical adapter failure
consumes a rolling search budget but does not advance the entity checkpoint.
Once a candidate may have crossed the external-effect boundary, the run consumes
its grab budget even if observation is temporarily unavailable.
