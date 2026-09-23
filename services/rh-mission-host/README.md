# RH Mission Host

The TypeScript Host runs the real research executive through the official Codex
CLI. Python owns the durable research workspace and mathematical operations.
The Host owns process lifecycle, current Goal identity, scoped delegated grants,
captures and restart reconciliation. A healthy process or successful tool call
does not establish a mathematical result.

## Supported installation

Use Linux with a dedicated unprivileged runtime account. Build the public source
and install it as a root-owned release that the runtime account cannot alter.
The source root, Python bridge, generated tool projection, genesis seed and model
catalog must be regular, root-owned, non-group/world-writable files/directories.
The release path must not resolve through a symlink. Retain the native Codex Linux
sandbox and its network proxy; a missing or incompatible sandbox is an installation
failure, not a reason to run without containment.

The current runtime is deliberately pinned to `@openai/codex@0.153.4`, provider
`openai`, model `gpt-6-astra`, reasoning effort `ultra`. Model access depends on the
operator's own account. There is no silent model or provider fallback.

Provider model metadata and base instructions are not redistributed. Obtain a
normal `models_cache.json` using your own authenticated pinned Codex installation,
then run:

```sh
node services/rh-mission-host/scripts/import-model-catalog.mjs \
  --cache /absolute/path/to/your/models_cache.json \
  --output /absolute/release/services/rh-mission-host/assets/codex-model-catalog.0.153.4.json
```

The importer selects the exact model, validates CLI compatibility and context
metadata, and applies this system's explicit V1/ultra/native result-capacity
policy. It preserves the complete provider `model_messages` record, including
policy blocks, and accepts null or absent optional minimum-version, compaction,
and template-variable metadata. It creates a new file and will not overwrite an existing catalog. Install
that generated file root-owned with the release. Its actual digest is part of
local launch accounting. The generated `assets` contents are ignored by Git;
the authored `test-fixtures` catalog is only for deterministic tests and must not
be used to execute a live model.

The informational launch report counts the exact `model_messages.instructions_template`
once. It does not reconstruct the native prompt: runtime-selected provider blocks,
template-variable rendering, tool framing and output reservations remain explicitly
unmeasured. The [pinned Codex implementation](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/protocol/src/openai_models.rs)
treats a template with null or absent variables as literal text; persistent-mode
instructions are a separate field, not automatically part of that template.

After initializing a fresh workspace with the Python owner, supply all these
environment bindings explicitly:

| Variable | Meaning |
| --- | --- |
| `RH_MISSION_HOST_MISSION_ID` | Exact Mission identity in the initialized workspace. |
| `RH_MISSION_HOST_RELEASE_COMMIT` | Exact public source commit for the installed release. |
| `RH_MISSION_HOST_REPO_ROOT` | Absolute installed, immutable public release root. |
| `RH_MISSION_HOST_WORKSPACE_ROOT` | Absolute Python-owned research workspace. |
| `RH_MISSION_HOST_GOALS_ROOT` | Absolute parent for fresh scratch directories, one per epoch. |
| `RH_MISSION_HOST_RUNTIME_DIR` | Absolute Host state, observation and formal-Attempt directory. |
| `RH_MISSION_RUNTIME_LOCK` | Absolute runtime lock path for this installation. |
| `RH_MISSION_HOST_PYTHON_PATH` | Absolute Python executable with the public packages installed. |
| `RH_MISSION_HOST_CODEX_CLI_PATH` | Absolute pinned Codex executable. |
| `CODEX_HOME` | Dedicated, authenticated Codex home belonging to the operator. |

Source, research workspace, Goal scratch, runtime state and Codex home must be
separate non-overlapping directories. The isolated Codex home must not inherit
another installation's plugins, instructions, configuration or session history.
Generate `.mathematical-research-model-projection.json` with the Python owner
before installing the release; do not hand-author its schemas.

Start the foreground process with `node services/rh-mission-host/dist/main.js`
from the installed release. The executable takes no ordinary free-form arguments.
There is no fleet dispatcher, default remote host or account connection.

## Stop and recovery

`SIGINT`/`SIGTERM` request graceful stop. `SIGUSR2` explicitly force-stops the
current Goal and reconciles its owned execution. These signals target only the
exact process belonging to this installation. Restart with the same environment
and state; do not delete state to make a failed start look fresh.

An empty `checkpoint-stop.pending` in this runtime directory requests a one-shot
stop after the next durable checkpoint, before a successor epoch. An empty
`single-epoch-canary.pending` stops after one terminal epoch. Neither marker is a
research-result claim or an implicit recurring budget.

Stopped-epoch reconciliation is a separate operator entry:
`--reconcile-stopped <exact-epoch-id> <exact-root-thread-id>`. It does not launch a
new research epoch. Uncertain execution is retained and reconciled rather than
automatically retried.

## Scientific and capability boundaries

The nine scientific operations are `orient`, `retrieve`, `record_context`,
`interpret_material`, `record_candidate`, `record_branch`, `synthesize`,
`record_strategy` and `checkpoint`. `usage` is their zero-effect syntax guide.
Historical/research reads, frozen Candidate A1 review and Admission each require
their own exact child grant. Formal Attempts are separately selected operations.

Raw material, interpretation, candidate preservation and canonical Admission
remain distinct. Instruction bodies come from `docs/instructions`; generated
role carriers contain those complete public sources. The Model is not given
private operator credentials or direct access to the owner workspace.

The optional `RH_MISSION_AGENT_COMMUNICATIONS_OUTBOX_DIRECTORY` writes local
credential-free JSON notifications to an explicitly configured directory. No
notification service is included or contacted; it is dormant when unset.
