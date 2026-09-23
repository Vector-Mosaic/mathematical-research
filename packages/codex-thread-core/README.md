# codex-thread-core

Codex CLI `app-server` boundary used by the [Mission Host](../../services/rh-mission-host/README.md).

The boundary binds each Goal to an explicit model/version policy, isolated working
directory and filesystem/network permission profile. It validates native identity,
delegation lineage and child tool grants; it also records observable execution,
handles cancellation, and reconciles interrupted Goals without silently relaunching them.

This package wraps the official Codex CLI. It is not a fork of Codex or a substitute
for its Linux sandbox. The Host supplies the pinned runtime configuration.
