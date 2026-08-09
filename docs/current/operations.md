# Operations

The 0.1.1 corrective release candidate contains one wheel plus Reconciler,
Fence, and Publisher OCI images. This page is not an installation guide and no
live deployment has been tested.

## Processes

The planned entrypoints are:

```text
media-interlock-reconciler
media-interlock-publisher
media-interlock-fence
```

All entrypoints provide `--version` without configuration or network access and
`--config FILE --check-config` for local configuration validation without
opening state or contacting an adapter. Fence and Publisher additionally accept
`--config FILE --status` to query their existing local daemon. These probes do
not supervise, start, stop, or reconfigure another process.

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

The local-only artifact gate is `python scripts/build-artifacts.py --output DIR`.
It requires a clean checkout and emits one wheel plus Reconciler, Fence, and
Publisher OCI archives, individual manifest-digest files, and a canonical
`artifacts.json` binding source revision, version, wheel hash, and image
identities. It never pushes or publishes; OCI archives are local transport.
Downstream consumers pin the public release wheel or these immutable OCI
digests, never a local checkout or an unversioned image tag. The release itself
does not provide deployment manifests or grant live acceptance.

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
behavior from each configured Arr qBittorrent client, distinct source
categories, and a regular singly linked shared mutation-lock file. Fence
accepts a stopped add only after it observes the exact Arr download identity
and corresponding canonical hash, category, reservation tag, and qBittorrent
save path; it persists resume intent before the resume call. Bounded polling
can adopt an external Arr grab only after its post-watermark Queue/History
observation is durably fingerprinted. Read-only capacity probes supply
conservative `statvfs` headroom; they are not filesystem quotas. MediaInterlock validates
the paths and observable upstream settings; deployment manifests, identities,
mounts, ACLs, and credentials enforce the parts outside the process. Both are
required, and negative probes belong to downstream acceptance.

Each Publisher source profile also declares a bounded bundle settle interval,
accepted sidecar extensions, and optional required language aliases and
container evidence. These narrow eligibility only: Publisher always performs
two no-follow bundle observations and independent-inode canonical copies. If a
profile can receive Arr hardlinks, the Publisher must be configured with the
Fence socket; otherwise that candidate remains pending. Bootstrap and assisted
candidate intake use the Publisher's local versioned socket with sealed
owner-bound manifests. A downstream tool may prepare those inputs, but it owns
the one-off selection or migration policy and must not expect a Fence receipt
from either intake path.

## Observability

Human CLI output explains the blocked invariant without printing private media
metadata or secrets. JSON output has a versioned schema and stable machine
status codes. Health distinguishes process liveness, configuration readiness,
adapter readiness, and inhibited work. Metrics are bounded-cardinality and do
not use media paths, titles, torrent hashes, usernames, or operation IDs as
labels. Fence's shared-lease metrics report bounded availability and an opened
device/inode identity only; they do not reveal a lock path or a peer writer.

The current development compatibility profile is Python 3.14.7, Jellyfin
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
