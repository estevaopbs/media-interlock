# Publisher

Publisher is the only MediaInterlock role that writes canonical video library
files. It validates an Arr-correlated staging bundle without following links,
copies independent generation files, atomically exposes the current generation,
and verifies the selected Jellyfin catalog item. A failed or ambiguous step
keeps the prior canonical generation.

Normal acquisition reaches Publisher through Fence custody in the shared
runtime. A bounded import reconciler also reads incremental `downloadFolderImported`
history from Radarr or Sonarr. Its cursor and deterministic operation ID are
derived from source and History ID. Before a bootstrap operation, Publisher
requires a single exact Arr identity and a path strictly below the configured
Arr import prefix translated to its staging root. It verifies only that bundle;
it never recursively discovers media files.

The cursor advances only after a complete history response. Network/schema
failure leaves it in place. Replaying a history item or restarting is
idempotent because the bootstrap operation is durable. Existing published work,
outside-root paths, incomplete bundles, and ambiguous Arr identity receive no
publication effect.
