# Contributing

MediaInterlock is pre-release. Contributions should preserve its three narrow
responsibilities and fail-closed behavior rather than expand it into a complete
media-stack manager.

## Before changing code

1. Read `AGENTS.md` and `docs/index.json`.
2. Load the current architecture plus the affected component page.
3. Confirm that the change belongs to reconciler, publisher, or fence.
4. For non-trivial behavior, work from the active specification and a bounded
   plan in an ephemeral branch/worktree.

Stack installers, systemd or container topology, firewall and reverse-proxy
rules, backup products, and one-off data repair belong in downstream deployment
repositories. A broadly useful upstream API gap should be proposed upstream
before MediaInterlock grows a private substitute.

## Development method

- Pin the latest stable supported upstream versions at the beginning of a
  development cycle. Do not silently widen compatibility inside that cycle.
- Add a failing test for behavior or a contract before implementation.
- Prefer pure, table-driven state-machine tests; use adapter contract tests at
  network boundaries and disposable integration tests only where process or
  filesystem behavior matters.
- Test durable crash boundaries parametrically. Do not duplicate every rule at
  unit, integration, and end-to-end levels without a distinct risk.
- Keep safety invariants non-configurable. Expose only typed, bounded policy in
  TOML; secrets come from environment variables or files.
- Keep component stores private. Cross-component behavior uses versioned Unix
  socket contracts and idempotent operation identifiers.

The documentation support surface currently uses only the Python standard
library:

```bash
python -m unittest discover -s tests -v
python scripts/check-docs.py
git diff --check
```

Product-specific commands will be added only when the corresponding vertical
slice exists.

## Changes and review

Keep commits cohesive and green. Explain the user-visible behavior, the safety
boundary, and the exact verification performed. One consolidated review is
preferred after the candidate is stable; avoid permanent review ledgers,
agent attestations, and generated evidence about the development process.
