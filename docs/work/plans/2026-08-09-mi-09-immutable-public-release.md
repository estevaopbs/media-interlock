# MI-09 immutable public release plan

**Goal:** Publish the accepted `0.1.0` MediaInterlock release from the exact
MI-08 source commit, then converge the public current documents and program
routing without changing a downstream deployment.

**Architecture:** The immutable release identity is the `v0.1.0` tag on the
accepted MI-08 commit. Its GitHub Release carries the exact prebuilt wheel and
only the three prebuilt OCI images are retagged and pushed to the public
registry. A later documentation-only `main` commit records the verified release
truth; it does not move or replace the tag or artifacts.

**Tech stack:** Git fast-forward and annotated tag, GitHub CLI release API,
Podman local OCI image/registry commands, public GitHub and registry readback,
and existing documentation checker.

## Global constraints

- Use only the already accepted artifact manifest whose source revision is the
  exact MI-08 commit. Do not rebuild, retag a different image, regenerate a
  wheel, replace an artifact, force-push, or overwrite an existing tag/release.
- Verify origin, remote `main` ancestry, absent `v0.1.0` tag/release, GitHub
  authentication, and GHCR authorization before any irreversible action. A
  divergence, occupied identity, or missing permission is a fail-closed external
  gate.
- Publish only this repository's `main`, the `v0.1.0` tag, its wheel, and the
  Reconciler, Fence, and Publisher images. Do not publish to PyPI, contact live
  media services, or modify any downstream repository.
- Do not expose credentials in commands, logs, metadata, artifacts, or docs.

## Task 1: Bind the accepted release inputs

- Confirm clean local source, expected public origin, remote fast-forward
  relation, and the unoccupied SemVer tag/release identity.
- Verify the retained artifact manifest binds `0.1.0`, the selected MI-08
  commit, one wheel SHA-256, and the three local OCI manifest digests. Verify
  all image labels and arbitrary-numeric-UID probes again without rebuilding.
- Record the immutable release metadata text from those exact facts and perform
  a final read-only review of scope and credential safety.

## Task 2: Publish one immutable public identity

- Fast-forward push `main` to the accepted MI-08 commit, create and push the
  annotated `v0.1.0` tag at that exact commit, and create its GitHub Release
  with the exact wheel as the sole binary attachment.
- Retag and push only the accepted Reconciler, Fence, and Publisher OCI images
  to `ghcr.io/estevaopbs` at `0.1.0`; capture remote manifest digests and
  inspect the remote labels. Never replace an existing image tag or digest.
- Independently read GitHub and GHCR back to verify tag target, release asset
  SHA-256, source commit, image manifest digests, labels, and release metadata.

## Task 3: Converge public truth

- Update permanent release/current documents with the verified public identity,
  artifact digests, and explicit limits of proof. Remove this plan, the program
  specification, slice catalog, their index routing, and any stale checkpoint.
- Run the required full test, documentation, compilation, diff, and secret
  gates. Commit and fast-forward push the documentation convergence to `main`.
- Read remote `main`, tag, release, asset, and images again. Report only those
  verified facts; do not claim downstream or live acceptance.
