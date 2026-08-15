# Current state

MediaInterlock 0.1.27 is the immutable public downstream-consumption release.
It publishes one wheel and one OCI image with the `media-interlock` entrypoint.
Fence, Publisher, and Reconciler are internal roles in that one process and
share one namespaced SQLite state database.

This source repository has product tests only. It contains no deployment
configuration, running service, live media-library proof, or authorization to
mutate an external stack. A downstream deployment must pin the public image
digest and perform its own lifecycle and live acceptance.
