# Current state

MediaInterlock 0.1.35 is the immutable public downstream-consumption release.
On a source without a durable Publisher import cursor, it applies the typed
initial history lookback before accepting Arr imports, advances over older
history records, and then resumes incremental history-ID intake. It publishes
one wheel and one OCI image with the `media-interlock` entrypoint. Fence,
Publisher, and Reconciler are internal roles in that one process and share one
namespaced SQLite state database. Completed candidates leave the health state;
the restore path discards the known legacy terminal candidate projection after
validating its shape, so it cannot block the process from starting. The bounded
initial import recovery can copy a twice-verified completed Arr hardlink into
an independent generation; one rejected historical item does not block the
cursor from reaching later imports. Its deterministic UUIDv5 history operation
is accepted as that generation's identity. The normal Publisher worker resumes
a durable generation intent after restart, rather than leaving it pending.
An older public route can be adopted only after its content exactly matches the
verified generation; a different route remains blocked.

This source repository has product tests only. It contains no deployment
configuration, running service, live media-library proof, or authorization to
mutate an external stack. A downstream deployment must pin the public image
digest and perform its own lifecycle and live acceptance.
