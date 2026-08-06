# Agent routing

Read `docs/index.json` before working. Load the required entry points and only
the documents whose `load_when` conditions match the task. During an active
program, the indexed specification and slice catalog own pending design; one
active implementation plan may own the current slice.

## Product boundary

MediaInterlock owns only the reconciler, transactional publisher, and
acquisition fence. It does not own stack lifecycle, service topology, reverse proxy,
backup or restore, one-off migrations, Jellyfin database edits, or deployment-
specific administration. Do not import those concerns to make extraction from
a downstream deployment easier.

The public repository owns provider-neutral product behavior. A downstream
deployment repository owns local configuration, packaging, service topology,
secrets, live acceptance, and one-off correction utilities. Here, downstream
packaging means deployment manifests and service integration; MediaInterlock
owns its public wheel and OCI images. Never copy product
source back into an overlay; consume a pinned release.

## Development lifecycle

- Git is authoritative for source, commits, and integration state. `main` is
  the protected integration branch; use an ephemeral branch and worktree for
  non-trivial implementation.
- Do not fetch, push, publish packages, create releases, or mutate a live stack
  unless the user explicitly requests that external action.
- Use the indexed specification for pending program design and one bounded plan
  for the active slice. Remove that plan after the slice converges and current
  documents are updated. Remove the program specification and slice catalog
  only after every program unit converges.
- Treat only a missing behavioral oracle, contradictory approved requirements,
  incompatible architectures, or a required weakening of an invariant as a
  `DecisionGap`. Stale paths, decomposition, ordinary defects, test selection,
  and generated-file refresh are implementation work.
- Develop behavior with TDD. Start with the smallest test that can invalidate
  the change, then expand according to affected risk. Do not run every adapter
  or full-system rehearsal after each edit.
- Stabilize a candidate before one consolidated review. Later corrections need
  only focused review unless they change the candidate's architecture or proof
  bindings.
- Process records, agent reports, commits, fixtures, and modeled tests are not
  operational proof. Report only checks actually run and observations actually
  made.

An optional ignored `.agent/checkpoint.json` may help resume interrupted work.
It must be under 4 KiB and contain only the current slice, repository branch and
HEAD, clean/dirty state, a short handoff, and at most five notes. It is derived
from Git and must never contain approvals, reviews, transcripts, evidence,
secrets, or duplicated program history. Discard it when stale.

## Cross-repository changes

A change that affects the downstream deployment contract uses the same program
unit identifier in both repositories but receives one plan per repository.
Integrate and release the MediaInterlock contract first; update the downstream
pin and local acceptance second. A public component test cannot prove the live
overlay, and an overlay rehearsal cannot replace public product gates.

## Documentation and verification

Documentation rules under `docs/` are in `docs/AGENTS.md`. Before committing a
documentation or support-surface change, run:

```bash
python -m unittest discover -s tests -v
python scripts/check-docs.py
git diff --check
```

For product changes, add only the focused and affected integration gates owned
by the active plan. Never expose credentials in configuration examples,
arguments, logs, fixtures, diffs, checkpoints, or commits.
