# Documentation routing

These rules apply under `docs/`; repository-wide lifecycle and safety rules
remain in the root `AGENTS.md`.

## Authorities

- Every Markdown file must appear exactly once in `docs/index.json`, with one
  owner and a concrete `load_when` condition.
- Give each current fact one page and one owner. Link to that authority instead
  of copying its text.
- Permanent documents describe only the current product and must not link to
  specifications, slice catalogs, plans, or other work material.
- Git history preserves past decisions and states. Do not create archives,
  completed-plan ledgers, decision-log catalogs, compatibility readers, or
  replacement historical summaries.
- When a slice converges, update its permanent owners and remove only its active
  plan and checkpoint. Keep the program specification and slice catalog while
  any program unit remains pending. At final program convergence, update all
  permanent owners and then remove the specification, catalog, their index
  routing, and the checkpoint.

## Hard budget

The permanent Markdown surface is capped at 12 files and 160 KiB total. Active
work material is capped at one specification (64 KiB), one slice catalog
(24 KiB), and one plan (32 KiB). These are ceilings, not targets. Prefer
deleting duplication or merging ownership before increasing the surface.
The 12 permanent slots are deliberately allocated. Future configuration and
upgrade guidance belongs in the existing architecture and operations owners;
if a genuinely separate authority is required, consolidate or remove an
existing page first.

All public documentation is written in English. Examples use synthetic names,
paths, identifiers, and secrets. Local deployment facts belong downstream.
Local links use inline Markdown syntax with an explicit destination.
Percent-encode parentheses, use angle brackets for destinations containing
spaces, and give external links an explicit URI scheme. Reference-style links
and raw HTML links are intentionally unsupported; external links use only
HTTP, HTTPS, or `mailto`. The validator checks local files but not heading
fragments.

## Verification

For every documentation change, run:

```bash
python scripts/check-docs.py
python -m unittest discover -s tests -v
git diff --check
```

Add product tests only if the changed claim depends on product behavior. A
document, fixture, agent report, or successful docs check is not runtime or live
acceptance evidence.
