# Acceptance

Product checks prove source-level contracts; they do not prove a live deployment
or media library. Required product verification for a release is the affected
unit/integration tests, the full test suite, documentation validation, and
`git diff --check`.

The release contract includes one `media-interlock` console entrypoint and one
OCI final image. Tests cover unified state adoption, no component socket or
subprocess in the runtime path, isolated periodic task failures, release
response bounds and technical retry, stable completed-search cooldowns, fair
series scheduling, bounded pre-existing Arr import reconciliation, and exact
video-candidate health invalidation.

Downstream acceptance is separate. It validates a public immutable digest,
one container/PID/unit, its configured mounts and services, and live behavior
only under explicit deployment authorization. It never substitutes a local
checkout, test fixture, or product test for that proof.
