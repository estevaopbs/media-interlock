# Operations

There is no runnable release yet. Fence and Publisher are implemented as local
daemons, but this page is not an installation guide and no live deployment has
been tested.

## Processes

The planned entrypoints are:

```text
media-interlock-reconciler
media-interlock-publisher
media-interlock-fence
```

Reconciler runs to completion under an external scheduler or on demand.
Publisher and fence run independently and expose health, metrics, and versioned
Unix socket endpoints. `media-interlock-publisher --config FILE --status` and
the corresponding Fence command query only their local daemon status. They do
not start, stop, or reconfigure one another.

Each process has a separate least-privilege identity, state directory, runtime
socket, and configured filesystem/network access. Containers receive only the
mounts and sockets their component needs. A deployment may install any
component independently, but a release is accepted only after all three are
integrated as declared in the current release profile.

## Configuration

Validated TOML has one shared runtime section plus optional component and
adapter sections; each process reads only its owned typed projection.
Configuration files contain no secret values. Secret references use `env:` or
`file:` forms, resolve only when an adapter first needs them, and are redacted from
diagnostics.

Startup fails before side effects when configuration is unknown, ambiguous, or
incompatible. A missing optional adapter disables only capabilities that depend
on it; a configured but unhealthy required adapter makes the relevant boundary
unready rather than silently degrading to unsafe behavior.

Operational readiness also requires disjoint staging/canonical roots,
publisher-only canonical write access, read-only playback mounts, paused-on-add
qBittorrent behavior, a dedicated configured qBittorrent category, and
fence-only resume authority. Fence accepts a stopped add only after it observes
its exact hash, category, reservation tag, and staging root; it persists resume
intent before the resume call. MediaInterlock validates
the paths and observable upstream settings; deployment manifests, identities,
mounts, ACLs, and credentials enforce the parts outside the process. Both are
required, and negative probes belong to downstream acceptance.

## Observability

Human CLI output explains the blocked invariant without printing private media
metadata or secrets. JSON output has a versioned schema and stable machine
status codes. Health distinguishes process liveness, configuration readiness,
adapter readiness, and inhibited work. Metrics are bounded-cardinality and do
not use media paths, titles, torrent hashes, usernames, or operation IDs as
labels.

The current development compatibility profile is Python 3.14.6, Jellyfin
10.11.11, Radarr 6.3.0.10514, Sonarr 4.0.19.2979, qBittorrent 5.2.3, Bazarr
1.6.0, Seerr 3.4.1, and Prowlarr 2.5.2.5491. Only a later adapter contract test
qualifies a capability against its corresponding pin.

## Downstream adoption

A deployment repository pins an immutable MediaInterlock version or image
digest and owns its service manager, container definitions, paths, credentials,
reverse proxy, host hardening, backups, and live acceptance. Cross-repository
contract changes are released here before the deployment pin changes.

One-off data correction and migration utilities stay downstream and may be
deleted after their sealed operation. They never become generic adapters merely
because their source informed a product invariant.

An operator remains responsible for backup and recovery. MediaInterlock neither
installs nor requires restic, filesystem snapshots, or a particular upstream
backup facility.
