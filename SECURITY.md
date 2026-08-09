# Security policy

MediaInterlock controls filesystem publication and external service effects.
Treat bypasses of fail-closed admission, path containment, single-writer state,
intent-before-effect ordering, secret redaction, or last-known-good retention
as security-sensitive.

## Supported versions

The supported public line is 0.1.0. Only the latest stable MediaInterlock line
and the upstream versions pinned for its current development cycle are
supported unless release notes say otherwise.

## Reporting a vulnerability

Do not include credentials, personal media metadata, private paths, or live
service responses in a public issue. Once the public repository is hosted, use
its private security-advisory channel. Until then, report privately to the
repository owner rather than opening a public ticket.

## Trust boundary

- Deployment operators provision host security, filesystem permissions,
  service isolation, TLS, backups, snapshots, and disaster recovery.
- MediaInterlock validates configured roots and remote responses but does not
  make an untrusted host or administrator safe.
- Secrets are accepted only through environment variables or files and must
  not be persisted in product state, logs, metrics, artifacts, or receipts.
- Direct edits to Jellyfin, Arr, qBittorrent, or MediaInterlock databases are
  unsupported. Integrations use public upstream APIs and versioned product
  contracts.
