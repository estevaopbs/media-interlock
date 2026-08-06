# Current state

MediaInterlock is in documented-design, preimplementation state. The repository
contains the development program and documentation validator only. It contains
no product package, daemon, OCI image, supported configuration schema, release,
or installation procedure.

Consequently:

- none of the seven planned upstream adapters is implemented or qualified;
- no compatibility versions have been pinned for an implementation cycle;
- no live, hardware, filesystem, container, or upstream-service acceptance has
  been performed;
- no downstream deployment should consume this repository yet.

## Authority split

The public project will own provider-neutral behavior for the reconciler,
publisher, and fence. Downstream deployment repositories own service topology,
host paths, secrets, lifecycle, reverse proxy, backup, restore, disaster
recovery, and one-off data correction or migration.

An existing downstream implementation is research and extraction input, not a
release candidate. Its one-off catalog/user-state corrections, migration,
backup, reverse proxy, service lifecycle, and live promotion machinery are
explicitly outside this product.

## Versioning and compatibility

The monorepo has one SemVer version for all three components. Development stays
in `0.x` while interfaces and adapters stabilize. Version `1.0.0` requires all
three components, all adapters declared for that release, integrated safety
gates, packaging, and operator documentation.

At the beginning of each implementation cycle, the project pins the latest
stable supported Python and upstream service versions. Those pins remain fixed
for the cycle and define what its adapter tests prove. MediaInterlock does not
promise compatibility with untested older releases.
